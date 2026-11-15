#!/usr/bin/env bash
# Installs OpenVPN + ovpn-panel on Debian 12 (designed for a Proxmox LXC container).
# Normally run by proxmox/create-ct.sh; it also works by hand inside an existing container or VM:
#   PUBLIC_HOST=home.ddns.net bash install.sh
# Running it again upgrades the application without touching the PKI, users or settings.
#
# Optional environment: PUBLIC_HOST, OVPN_PORT, OVPN_PROTO, OVPN_SUBNET, PANEL_PORT, ADMIN_USER,
#   PANEL_ALLOW (comma-separated networks that may reach the panel; default: the local network),
#   SANDBOX=0 (skip the systemd sandboxing, for troubleshooting).
set -euo pipefail
# "pct exec" does not pass a full PATH: without /usr/local/bin the ovpn-panel command is not found
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PUBLIC_HOST="${PUBLIC_HOST:-}"
OVPN_PORT="${OVPN_PORT:-1194}"
OVPN_PROTO="${OVPN_PROTO:-udp}"
OVPN_SUBNET="${OVPN_SUBNET:-10.9.0.0/24}"
PANEL_PORT="${PANEL_PORT:-8443}"
ADMIN_USER="${ADMIN_USER:-admin}"
PANEL_ALLOW="${PANEL_ALLOW:-}"
SANDBOX="${SANDBOX:-1}"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/ovpn-panel
CONF_DIR=/etc/ovpn-panel
DATA_DIR=/var/lib/ovpn-panel
EASYRSA_DIR=/etc/openvpn/easy-rsa
OVPN_DIR=/etc/openvpn/server
CREDS=/root/ovpn-panel-credentials.txt
PANEL_USER=ovpn-panel

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
cfg() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$CONF_DIR/config.json" "$1"; }

[[ $EUID -eq 0 ]] || die "run as root"
[[ -c /dev/net/tun ]] || die "/dev/net/tun is not available: the container needs access to the tun device (see docs/INSTALL.md)"
[[ $OVPN_PROTO == udp || $OVPN_PROTO == tcp ]] || die "OVPN_PROTO must be udp or tcp"
[[ -z $PUBLIC_HOST || $PUBLIC_HOST =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] \
  || die "PUBLIC_HOST must be a host name or an IPv4 address"
[[ $OVPN_PORT =~ ^[0-9]{1,5}$ && $PANEL_PORT =~ ^[0-9]{1,5}$ ]] || die "ports must be numbers"
[[ $OVPN_SUBNET =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "OVPN_SUBNET must look like 10.9.0.0/24"
[[ $ADMIN_USER =~ ^[a-z0-9][a-z0-9_.-]{1,31}$ ]] || die "ADMIN_USER: lowercase letters, digits, _ . -"

LAN_IP="$(hostname -I | awk '{print $1}')"
[[ -n $LAN_IP ]] || die "the machine has no IPv4 address"
PUBLIC_HOST="${PUBLIC_HOST:-$LAN_IP}"

step "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  openvpn easy-rsa iptables iproute2 openssl python3-venv ca-certificates

step "IP forwarding"
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-ovpn-panel.conf
sysctl -q -p /etc/sysctl.d/99-ovpn-panel.conf || true
[[ $(cat /proc/sys/net/ipv4/ip_forward) == 1 ]] \
  || warn "IP forwarding is off: VPN clients will reach this server but nothing behind it"

step "Service account"
getent group "$PANEL_USER" >/dev/null || groupadd --system "$PANEL_USER"
id "$PANEL_USER" >/dev/null 2>&1 \
  || useradd --system --gid "$PANEL_USER" --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$PANEL_USER"

step "OpenVPN PKI"
export EASYRSA_BATCH=1 EASYRSA_CA_EXPIRE=3650 EASYRSA_CERT_EXPIRE=3650 EASYRSA_CRL_DAYS=365 \
       EASYRSA_ALGO=ec EASYRSA_CURVE=prime256v1
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
chmod 700 "$EASYRSA_DIR"   # the CA key lives here: only root, never the panel
install -d -m 755 "$OVPN_DIR" "$OVPN_DIR/ccd"
cp "$EASYRSA_DIR/pki/ca.crt" "$EASYRSA_DIR/pki/issued/server.crt" "$OVPN_DIR/"
install -m 600 "$EASYRSA_DIR/pki/private/server.key" "$OVPN_DIR/server.key"
# do not overwrite a newer CRL written by the panel after a revocation
[[ -f $OVPN_DIR/crl.pem ]] || install -m 644 "$EASYRSA_DIR/pki/crl.pem" "$OVPN_DIR/crl.pem"
[[ -f $OVPN_DIR/tc.key ]] || openvpn --genkey secret "$OVPN_DIR/tc.key"
chmod 600 "$OVPN_DIR/tc.key"

step "Application"
install -d -m 755 "$APP_DIR"
rm -rf "$APP_DIR/ovpnpanel"
cp -r "$SRC_DIR/ovpnpanel" "$APP_DIR/"
find "$APP_DIR/ovpnpanel" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR/ovpnpanel"
chmod -R go-w,a+rX "$APP_DIR/ovpnpanel"
# a clean virtualenv every time, with the exact versions (and hashes) of requirements.lock
rm -rf "$APP_DIR/venv"
python3 -m venv "$APP_DIR/venv"
cp "$SRC_DIR/requirements.lock" "$APP_DIR/requirements.lock"
"$APP_DIR/venv/bin/pip" install -q --disable-pip-version-check --no-cache-dir --require-hashes \
  -r "$APP_DIR/requirements.lock"
install -d -m 755 /usr/local/lib/ovpn-panel
install -m 755 "$SRC_DIR/firewall.sh" /usr/local/lib/ovpn-panel/firewall.sh

cat > /usr/local/bin/ovpn-panel <<'EOF'
#!/bin/sh
# Commands that only touch the panel's database run as the unprivileged panel user;
# the others (certificates, backups, firewall, TLS) need root.
export PYTHONDONTWRITEBYTECODE=1
cd /opt/ovpn-panel || exit 1
PY=/opt/ovpn-panel/venv/bin/python
case "${1:-}" in
  create-admin|reset-password|unlock|audit)
    if [ "$(id -u)" = 0 ]; then
      exec runuser -u ovpn-panel -- "$PY" -m ovpnpanel.manage "$@"
    fi
    exec "$PY" -m ovpnpanel.manage "$@" ;;
  *)
    exec "$PY" -m ovpnpanel.helper.admin "$@" ;;
esac
EOF
chmod 755 /usr/local/bin/ovpn-panel

step "Configuration"
install -d -m 750 -o root -g "$PANEL_USER" "$CONF_DIR"
install -d -m 700 -o "$PANEL_USER" -g "$PANEL_USER" "$DATA_DIR"
chown -R "$PANEL_USER:$PANEL_USER" "$DATA_DIR"   # an older version created it as root
extra=()
[[ $PUBLIC_HOST == "$LAN_IP" ]] || extra+=(--extra-host "$PUBLIC_HOST")
ovpn-panel configure --public-host "$PUBLIC_HOST" --port "$OVPN_PORT" --proto "$OVPN_PROTO" \
  --subnet "$OVPN_SUBNET" --panel-port "$PANEL_PORT" --panel-host "$LAN_IP" \
  --extra-host "$(hostname)" "${extra[@]}" ${PANEL_ALLOW:+--allow "$PANEL_ALLOW"}
# from here on, what is stored wins over the environment (an upgrade keeps the original choices)
OVPN_PORT="$(cfg port)"; OVPN_PROTO="$(cfg proto)"; PANEL_PORT="$(cfg panel_port)"; PUBLIC_HOST="$(cfg public_host)"
ovpn-panel write-server-conf

step "Panel HTTPS certificate"
ovpn-panel init-tls

step "Firewall"
ovpn-panel apply-firewall --no-reload

step "Services"
# the previous version used a plain NAT unit; the firewall unit replaces it
if [[ -f /etc/systemd/system/ovpn-panel-nat.service ]]; then
  systemctl disable --now ovpn-panel-nat 2>/dev/null || true
  rm -f /etc/systemd/system/ovpn-panel-nat.service
fi

cat > /etc/systemd/system/ovpn-panel-firewall.service <<EOF
[Unit]
Description=ovpn-panel firewall rules (NAT, client isolation, panel access)
After=network-online.target
Wants=network-online.target
Before=ovpn-panel.service openvpn-server@server.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/ovpn-panel/firewall.sh start
ExecStop=/usr/local/lib/ovpn-panel/firewall.sh stop

[Install]
WantedBy=multi-user.target
EOF

# in LXC the Debian unit's LimitNPROC=10 prevents openvpn from starting
install -d /etc/systemd/system/openvpn-server@server.service.d
cat > /etc/systemd/system/openvpn-server@server.service.d/ovpn-panel.conf <<'EOF'
[Service]
LimitNPROC=infinity
EOF

cat > /etc/systemd/system/ovpn-panel-helper.service <<EOF
[Unit]
Description=ovpn-panel privileged helper (root; serves the panel over a unix socket)
After=network-online.target openvpn-server@server.service
Before=ovpn-panel.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_DIR/venv/bin/python -m ovpnpanel.helper
RuntimeDirectory=ovpn-panel
RuntimeDirectoryMode=0750
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

panel_caps=""
if (( PANEL_PORT < 1024 )); then
  panel_caps=$'CapabilityBoundingSet=CAP_NET_BIND_SERVICE\nAmbientCapabilities=CAP_NET_BIND_SERVICE'
else
  panel_caps=$'CapabilityBoundingSet=\nAmbientCapabilities='
fi

cat > /etc/systemd/system/ovpn-panel.service <<EOF
[Unit]
Description=ovpn-panel (OpenVPN dashboard, unprivileged)
After=network-online.target ovpn-panel-helper.service ovpn-panel-firewall.service
Wants=ovpn-panel-helper.service ovpn-panel-firewall.service

[Service]
User=$PANEL_USER
Group=$PANEL_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_DIR/venv/bin/uvicorn ovpnpanel.main:app --host 0.0.0.0 --port $PANEL_PORT \\
  --ssl-keyfile $CONF_DIR/tls.key --ssl-certfile $CONF_DIR/tls.crt --no-access-log --no-server-header
Restart=on-failure
RestartSec=2
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/ovpn-panel-maintenance.service <<'EOF'
[Unit]
Description=ovpn-panel maintenance (revocation list and panel certificate renewal)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ovpn-panel maintenance
EOF
cat > /etc/systemd/system/ovpn-panel-maintenance.timer <<'EOF'
[Unit]
Description=Daily ovpn-panel maintenance

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

# sandboxing lives in drop-ins so it can be removed for troubleshooting (see docs/INSTALL.md)
for unit in ovpn-panel ovpn-panel-helper; do install -d "/etc/systemd/system/$unit.service.d"; done
if [[ $SANDBOX == 0 ]]; then
  rm -f /etc/systemd/system/ovpn-panel.service.d/10-sandbox.conf /etc/systemd/system/ovpn-panel-helper.service.d/10-sandbox.conf
  warn "systemd sandboxing disabled (SANDBOX=0)"
else
  cat > /etc/systemd/system/ovpn-panel.service.d/10-sandbox.conf <<EOF
[Service]
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
InaccessiblePaths=-/etc/openvpn
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
SystemCallArchitectures=native
$panel_caps
EOF
  cat > /etc/systemd/system/ovpn-panel-helper.service.d/10-sandbox.conf <<'EOF'
[Service]
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/etc/openvpn /run
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
EOF
fi

systemctl daemon-reload
systemctl enable -q ovpn-panel-firewall openvpn-server@server ovpn-panel-helper ovpn-panel ovpn-panel-maintenance.timer
systemctl restart ovpn-panel-firewall openvpn-server@server ovpn-panel-helper

for _ in $(seq 30); do
  [[ -S /run/openvpn-mgmt.sock && -S /run/ovpn-panel/helper.sock ]] && break
  sleep 1
done
[[ -S /run/openvpn-mgmt.sock ]] || die "OpenVPN did not start: journalctl -u openvpn-server@server"
[[ -S /run/ovpn-panel/helper.sock ]] || die "the helper did not start: journalctl -u ovpn-panel-helper"
systemctl restart ovpn-panel
systemctl start ovpn-panel-maintenance.timer

step "Admin account"
if [[ ! -f $CREDS ]]; then
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16)}"
  OVPN_PANEL_PASSWORD="$ADMIN_PASSWORD" ovpn-panel create-admin "$ADMIN_USER"
  (umask 077; cat > "$CREDS" <<EOF
url=https://$LAN_IP:$PANEL_PORT/
username=$ADMIN_USER
password=$ADMIN_PASSWORD
EOF
  )
else
  echo "Credentials already in $CREDS (upgrade)"
fi

up=0
for _ in $(seq 30); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PANEL_PORT") 2>/dev/null; then up=1; break; fi
  sleep 1
done
[[ $up == 1 ]] || die "the panel did not start: journalctl -u ovpn-panel (try SANDBOX=0 if systemd sandboxing is not available here)"

VERSION="$(cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -c 'from ovpnpanel import __version__; print(__version__)')"
FP="$(ovpn-panel pki-status | sed -n 's/^Panel CA SHA-256: //p')"
cat <<EOF

==========================================================
 OpenVPN Panel $VERSION is installed
   Panel       : https://$LAN_IP:$PANEL_PORT/   (reachable from your local network only)
   Endpoint    : $PUBLIC_HOST:$OVPN_PORT/$OVPN_PROTO
   Credentials : $CREDS

 HTTPS uses a private CA. To avoid the browser warning, install
 https://$LAN_IP:$PANEL_PORT/panel-ca.crt as a trusted root, after checking its SHA-256 fingerprint:
   $FP
==========================================================
EOF
