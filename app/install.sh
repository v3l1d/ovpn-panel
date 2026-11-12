#!/usr/bin/env bash
# Installa OpenVPN + ovpn-panel su Debian 12 (pensato per un container LXC Proxmox).
# Di solito lo lancia proxmox/create-ct.sh; si puo' usare anche a mano dentro un container gia' esistente:
#   PUBLIC_HOST=casa.ddns.net bash install.sh
# Rilanciarlo aggiorna l'applicazione senza toccare PKI, utenti e impostazioni.
set -euo pipefail
# "pct exec" non passa un PATH completo: senza /usr/local/bin il comando ovpn-panel non si trova
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
die() { printf '\033[31mErrore: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "esegui come root"
[[ -c /dev/net/tun ]] || die "/dev/net/tun non disponibile: il container deve avere accesso a tun (vedi README)"
[[ $OVPN_PROTO == udp || $OVPN_PROTO == tcp ]] || die "OVPN_PROTO deve essere udp o tcp"

LAN_IP="$(hostname -I | awk '{print $1}')"
PUBLIC_HOST="${PUBLIC_HOST:-$LAN_IP}"

step "Pacchetti"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  openvpn easy-rsa iptables iproute2 openssl python3-venv ca-certificates

step "IP forwarding"
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-ovpn-panel.conf
sysctl -q -p /etc/sysctl.d/99-ovpn-panel.conf || true

step "PKI OpenVPN"
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
# non sovrascrivere una CRL piu' recente scritta dal pannello dopo una revoca
[[ -f $OVPN_DIR/crl.pem ]] || install -m 644 "$EASYRSA_DIR/pki/crl.pem" "$OVPN_DIR/crl.pem"
[[ -f $OVPN_DIR/tc.key ]] || openvpn --genkey secret "$OVPN_DIR/tc.key"

step "Applicazione"
install -d -m 700 "$CONF_DIR"
if [[ ! -f $CONF_DIR/config.json ]]; then
  cat > "$CONF_DIR/config.json" <<EOF
{
  "public_host": "$PUBLIC_HOST",
  "port": $OVPN_PORT,
  "proto": "$OVPN_PROTO",
  "subnet": "$OVPN_SUBNET"
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

step "Certificato TLS del pannello"
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

step "Servizi"
WAN_IF="$(ip -4 route show default | awk '{print $5; exit}')"
cat > /etc/systemd/system/ovpn-panel-nat.service <<EOF
[Unit]
Description=NAT per la rete OpenVPN
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

# in LXC il LimitNPROC=10 dell'unita' Debian impedisce l'avvio di openvpn
install -d /etc/systemd/system/openvpn-server@server.service.d
cat > /etc/systemd/system/openvpn-server@server.service.d/ovpn-panel.conf <<'EOF'
[Service]
LimitNPROC=infinity
EOF

cat > /etc/systemd/system/ovpn-panel.service <<EOF
[Unit]
Description=ovpn-panel (dashboard OpenVPN)
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
[[ -S /run/openvpn-mgmt.sock ]] || die "OpenVPN non si e' avviato: journalctl -u openvpn-server@server"

step "Amministratore"
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
  echo "Credenziali gia' presenti in $CREDS (installazione aggiornata)"
fi

for _ in $(seq 30); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PANEL_PORT") 2>/dev/null && break
  sleep 1
done

cat <<EOF

==========================================================
 OpenVPN Panel installato
   Pannello : https://$LAN_IP:$PANEL_PORT/
   Endpoint : $PUBLIC_HOST:$OVPN_PORT/$OVPN_PROTO
   Credenziali in $CREDS
==========================================================
EOF
