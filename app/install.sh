#!/usr/bin/env bash
# Installs OpenVPN + ovpn-panel on Debian 12 (designed for a Proxmox LXC container).
# Normally run by proxmox/create-ct.sh; it also works by hand inside an existing container or VM:
#   PUBLIC_HOST=home.ddns.net bash install.sh
# Running it again upgrades the application without touching the PKI, users or settings.
set -euo pipefail
# "pct exec" does not pass a full PATH: without /usr/local/bin the ovpn-panel command is not found
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PUBLIC_HOST="${PUBLIC_HOST:-}"
OVPN_PORT="${OVPN_PORT:-1194}"
OVPN_PROTO="${OVPN_PROTO:-udp}"
OVPN_SUBNET="${OVPN_SUBNET:-10.9.0.0/24}"
PANEL_PORT="${PANEL_PORT:-8443}"
ADMIN_USER="${ADMIN_USER:-admin}"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/ovpn-panel
CONF_DIR=/etc/ovpn-panel
EASYRSA_DIR=/etc/openvpn/easy-rsa
OVPN_DIR=/etc/openvpn/server
CREDS=/root/ovpn-panel-credentials.txt

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
[[ -c /dev/net/tun ]] || die "/dev/net/tun is not available: the container needs access to the tun device (see docs/INSTALL.md)"
[[ $OVPN_PROTO == udp || $OVPN_PROTO == tcp ]] || die "OVPN_PROTO must be udp or tcp"

LAN_IP="$(hostname -I | awk '{print $1}')"
PUBLIC_HOST="${PUBLIC_HOST:-$LAN_IP}"

step "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  openvpn easy-rsa iptables iproute2 openssl python3-venv ca-certificates

if [[ -f $CONF_DIR/config.json ]]; then
  # upgrade: keep the ports and network chosen at install time
  eval "$(python3 - "$CONF_DIR/config.json" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))
for var, key in (("PUBLIC_HOST", "public_host"), ("OVPN_PORT", "port"), ("OVPN_PROTO", "proto"),
                 ("OVPN_SUBNET", "subnet"), ("PANEL_PORT", "panel_port")):
    if key in c:
        print(f"{var}={shlex.quote(str(c[key]))}")
PY
)"
fi

step "IP forwarding"
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-ovpn-panel.conf
sysctl -q -p /etc/sysctl.d/99-ovpn-panel.conf || true
[[ $(cat /proc/sys/net/ipv4/ip_forward) == 1 ]] \
  || warn "IP forwarding is off: VPN clients will reach this server but nothing behind it"

step "OpenVPN PKI"
export EASYRSA_BATCH=1 EASYRSA_CRL_DAYS=3650 EASYRSA_CERT_EXPIRE=3650 EASYRSA_ALGO=ec EASYRSA_CURVE=prime256v1
if [[ ! -d $EASYRSA_DIR/pki ]]; then
  rm -rf "$EASYRSA_DIR"
  cp -r /usr/share/easy-rsa "$EASYRSA_DIR"
  (
    cd "$EASYRSA_DIR"
    ./easyrsa init-pki
    EASYRSA_REQ_CN="ovpn-panel-ca" ./easyrsa build-ca nopass
    ./easyrsa build-server-full server nopass
    ./easyrsa gen-crl
  ) >/dev/null 2>&1
fi
install -d -m 755 "$OVPN_DIR" "$OVPN_DIR/ccd"
cp "$EASYRSA_DIR/pki/ca.crt" "$EASYRSA_DIR/pki/issued/server.crt" "$OVPN_DIR/"
install -m 600 "$EASYRSA_DIR/pki/private/server.key" "$OVPN_DIR/server.key"
# do not overwrite a newer CRL written by the panel after a revocation
[[ -f $OVPN_DIR/crl.pem ]] || install -m 644 "$EASYRSA_DIR/pki/crl.pem" "$OVPN_DIR/crl.pem"
[[ -f $OVPN_DIR/tc.key ]] || openvpn --genkey secret "$OVPN_DIR/tc.key"

step "Application"
install -d -m 700 "$CONF_DIR"
if [[ ! -f $CONF_DIR/config.json ]]; then
  cat > "$CONF_DIR/config.json" <<EOF
{
  "public_host": "$PUBLIC_HOST",
  "port": $OVPN_PORT,
  "proto": "$OVPN_PROTO",
  "subnet": "$OVPN_SUBNET",
  "panel_port": $PANEL_PORT
}
EOF
fi
install -d "$APP_DIR"
rm -rf "$APP_DIR/ovpnpanel"
cp -r "$SRC_DIR/ovpnpanel" "$APP_DIR/"
[[ -d $APP_DIR/venv ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --disable-pip-version-check -r "$SRC_DIR/requirements.txt"
cat > /usr/local/bin/ovpn-panel <<EOF
#!/bin/sh
cd $APP_DIR && exec $APP_DIR/venv/bin/python -m ovpnpanel.manage "\$@"
EOF
chmod 755 /usr/local/bin/ovpn-panel
/usr/local/bin/ovpn-panel init

step "Panel TLS certificate"
if [[ ! -f $CONF_DIR/tls.crt ]]; then
  san="IP:$LAN_IP"
  if [[ $PUBLIC_HOST =~ ^[0-9.]+$ ]]; then
    [[ $PUBLIC_HOST == "$LAN_IP" ]] || san="$san,IP:$PUBLIC_HOST"
  else
    san="$san,DNS:$PUBLIC_HOST"
  fi
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=$PUBLIC_HOST" -addext "subjectAltName=$san" \
    -keyout "$CONF_DIR/tls.key" -out "$CONF_DIR/tls.crt" 2>/dev/null
  chmod 600 "$CONF_DIR/tls.key"
fi

step "Services"
WAN_IF="$(ip -4 route show default | awk '{print $5; exit}')"
cat > /etc/systemd/system/ovpn-panel-nat.service <<EOF
[Unit]
Description=NAT for the OpenVPN network
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/iptables -t nat -A POSTROUTING -s $OVPN_SUBNET -o $WAN_IF -j MASQUERADE
ExecStop=/usr/sbin/iptables -t nat -D POSTROUTING -s $OVPN_SUBNET -o $WAN_IF -j MASQUERADE

[Install]
WantedBy=multi-user.target
EOF

# in LXC the Debian unit's LimitNPROC=10 prevents openvpn from starting
install -d /etc/systemd/system/openvpn-server@server.service.d
cat > /etc/systemd/system/openvpn-server@server.service.d/ovpn-panel.conf <<'EOF'
[Service]
LimitNPROC=infinity
EOF

cat > /etc/systemd/system/ovpn-panel.service <<EOF
[Unit]
Description=ovpn-panel (OpenVPN dashboard)
After=network-online.target openvpn-server@server.service

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/uvicorn ovpnpanel.main:app --host 0.0.0.0 --port $PANEL_PORT \\
  --ssl-keyfile $CONF_DIR/tls.key --ssl-certfile $CONF_DIR/tls.crt
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q ovpn-panel-nat openvpn-server@server ovpn-panel
systemctl restart ovpn-panel-nat openvpn-server@server ovpn-panel

for _ in $(seq 30); do
  [[ -S /run/openvpn-mgmt.sock ]] && break
  sleep 1
done
[[ -S /run/openvpn-mgmt.sock ]] || die "OpenVPN did not start: journalctl -u openvpn-server@server"

step "Admin account"
if [[ ! -f $CREDS ]]; then
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16)}"
  OVPN_PANEL_PASSWORD="$ADMIN_PASSWORD" /usr/local/bin/ovpn-panel create-admin "$ADMIN_USER"
  (umask 077; cat > "$CREDS" <<EOF
url=https://$LAN_IP:$PANEL_PORT/
username=$ADMIN_USER
password=$ADMIN_PASSWORD
EOF
  )
else
  echo "Credentials already in $CREDS (upgrade)"
fi

for _ in $(seq 30); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PANEL_PORT") 2>/dev/null && break
  sleep 1
done

cat <<EOF

==========================================================
 OpenVPN Panel is installed
   Panel       : https://$LAN_IP:$PANEL_PORT/
   Endpoint    : $PUBLIC_HOST:$OVPN_PORT/$OVPN_PROTO
   Credentials : $CREDS
==========================================================
EOF
