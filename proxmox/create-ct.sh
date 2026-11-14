#!/usr/bin/env bash
# Creates a Debian 12 LXC container on Proxmox VE with OpenVPN + ovpn-panel, fully configured.
# Run it as root in the Proxmox node shell (Datacenter > node > Shell). See docs/INSTALL.md.
set -euo pipefail

# Where the application is downloaded from when this script runs on its own (e.g. through curl).
REPO_URL="${OVPN_PANEL_REPO_URL:-https://github.com/OWNER/ovpn-panel}"
REPO_REF="${OVPN_PANEL_REF:-main}"

CTID="${CTID:-}"
CT_HOSTNAME="${CT_HOSTNAME:-ovpn-panel}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
STORAGE="${STORAGE:-}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"
GW="${GW:-}"
DISK="${DISK:-4}"
MEMORY="${MEMORY:-512}"
CORES="${CORES:-1}"
OVPN_PORT="${OVPN_PORT:-1194}"
OVPN_PROTO="${OVPN_PROTO:-udp}"
PANEL_PORT="${PANEL_PORT:-8443}"
SSH_KEY="${SSH_KEY:-}"
YES="${YES:-0}"
UPDATE_CTID=""

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Creates a Debian 12 LXC container on Proxmox VE with OpenVPN + ovpn-panel.

Usage: create-ct.sh [options]
Every option can also be given as an environment variable (e.g. CTID=120).

  --ctid N              container ID                          (default: next free ID)
  --hostname NAME       container hostname                    (default: ovpn-panel)
  --public-host HOST    DDNS name or public IP for clients    (default: container IP, set it later in the panel)
  --storage NAME        storage for the root disk             (default: local-lvm, else the first available)
  --template-storage S  storage for the Debian template       (default: local)
  --bridge NAME         network bridge                        (default: vmbr0)
  --ip CIDR|dhcp        IPv4 address                          (default: dhcp)
  --gw IP               gateway, required with a static --ip
  --disk GB             root disk size                        (default: 4)
  --memory MB           RAM                                   (default: 512)
  --cores N             CPU cores                             (default: 1)
  --port N              OpenVPN port                          (default: 1194)
  --proto udp|tcp       OpenVPN protocol                      (default: udp)
  --panel-port N        panel HTTPS port                      (default: 8443)
  --ssh-key FILE        public key for root SSH access
  --repo URL            repository to download the app from   (default: OVPN_PANEL_REPO_URL)
  --update ID           upgrade the panel in an existing container instead of creating one
                        (users, certificates and settings are kept)
  -y, --yes             do not ask for confirmation
  -h, --help            show this help
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) CT_HOSTNAME="$2"; shift 2 ;;
    --public-host) PUBLIC_HOST="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --template-storage) TEMPLATE_STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gw) GW="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --port) OVPN_PORT="$2"; shift 2 ;;
    --proto) OVPN_PROTO="$2"; shift 2 ;;
    --panel-port) PANEL_PORT="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --update) UPDATE_CTID="$2"; shift 2 ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------- checks
[[ $EUID -eq 0 ]] || die "run as root in the Proxmox node shell"
command -v pct >/dev/null && command -v pveam >/dev/null || die "pct/pveam not found: run this script on a Proxmox VE node"
[[ $OVPN_PROTO == udp || $OVPN_PROTO == tcp ]] || die "--proto must be udp or tcp"
[[ $IP == dhcp || $IP =~ ^[0-9.]+/[0-9]+$ ]] || die "--ip must be dhcp or a CIDR (e.g. 192.168.1.50/24)"
[[ $IP == dhcp || -n $GW ]] || die "a static --ip also needs --gw"
[[ -z $SSH_KEY || -f $SSH_KEY ]] || die "SSH key not found: $SSH_KEY"

if [[ -n $UPDATE_CTID ]]; then
  [[ -e /etc/pve/lxc/$UPDATE_CTID.conf ]] || die "container $UPDATE_CTID does not exist"
  pct status "$UPDATE_CTID" | grep -q running || die "container $UPDATE_CTID is not running (pct start $UPDATE_CTID)"
else
  CTID="${CTID:-$(pvesh get /cluster/nextid)}"
  [[ ! -e /etc/pve/lxc/$CTID.conf ]] || die "container $CTID already exists"

  if [[ -z $STORAGE ]]; then
    storages="$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1}')"
    if grep -qx local-lvm <<<"$storages"; then STORAGE=local-lvm; else STORAGE="$(head -n1 <<<"$storages")"; fi
    [[ -n $STORAGE ]] || die "no active storage for containers: use --storage"
  fi

  if [[ -z $PUBLIC_HOST && $YES != 1 && -t 0 ]]; then
    read -rp "DDNS name or public IP that clients use to reach the VPN (Enter = set it later in the panel): " PUBLIC_HOST
  fi
fi

# ---------------------------------------------------------------- application source
# 1. dist/ovpn-panel-proxmox.sh carries the app after a __PAYLOAD_BELOW__ marker
# 2. run from a checkout of the repository: use ../app
# 3. run on its own (curl): download the repository archive
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SCRIPT_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"
if [[ -f $SCRIPT_PATH ]] && grep -q '^__PAYLOAD_BELOW__$' "$SCRIPT_PATH"; then
  sed '1,/^__PAYLOAD_BELOW__$/d' "$SCRIPT_PATH" | base64 -d > "$WORK/app.tgz" || die "the embedded package is corrupted"
elif [[ -f $SCRIPT_PATH && -f $(dirname "$SCRIPT_PATH")/../app/install.sh ]]; then
  tar czf "$WORK/app.tgz" -C "$(dirname "$SCRIPT_PATH")/.." app
else
  step "Downloading ovpn-panel ($REPO_URL, $REPO_REF)"
  mkdir "$WORK/src"
  # GitHub and Gitea/Forgejo use different archive paths
  for url in "$REPO_URL/archive/refs/heads/$REPO_REF.tar.gz" "$REPO_URL/archive/$REPO_REF.tar.gz"; do
    curl -fsSL "$url" 2>/dev/null | tar xz -C "$WORK/src" 2>/dev/null && break
  done
  app_dir="$(find "$WORK/src" -mindepth 2 -maxdepth 2 -type d -name app | head -n1)"
  [[ -n $app_dir && -f $app_dir/install.sh ]] || die "could not download the application from $REPO_URL (use --repo)"
  tar czf "$WORK/app.tgz" -C "$(dirname "$app_dir")" app
fi

install_app() {  # $1 = container ID; extra env vars are passed to install.sh
  local id="$1"; shift
  pct push "$id" "$WORK/app.tgz" /root/ovpn-panel.tgz
  pct exec "$id" -- sh -c 'rm -rf /root/ovpn-panel-src && mkdir -p /root/ovpn-panel-src && tar xzf /root/ovpn-panel.tgz -C /root/ovpn-panel-src && rm /root/ovpn-panel.tgz'
  pct exec "$id" -- env "$@" bash /root/ovpn-panel-src/app/install.sh
}

if [[ -n $UPDATE_CTID ]]; then
  step "Upgrading ovpn-panel in container $UPDATE_CTID"
  install_app "$UPDATE_CTID"
  printf '\n%s\n' "$(bold "Done. Users, certificates and settings were kept.")"
  exit 0
fi

# ---------------------------------------------------------------- summary
bold "This container will be created:"
cat <<EOF
  ID           $CTID ($CT_HOSTNAME)
  Resources    $CORES core(s), $MEMORY MB RAM, $DISK GB on $STORAGE
  Network      $BRIDGE, IP $IP${GW:+, gateway $GW}
  OpenVPN      port $OVPN_PORT/$OVPN_PROTO, endpoint ${PUBLIC_HOST:-<container IP, set it later in the panel>}
  Panel        port $PANEL_PORT (HTTPS)
EOF
if [[ $YES != 1 ]]; then
  read -rp "Continue? [y/N] " ok
  [[ $ok =~ ^[yY]$ ]] || { echo "Aborted."; exit 0; }
fi

# ---------------------------------------------------------------- template
step "Debian 12 template"
pveam update >/dev/null 2>&1 || echo "  (could not refresh the template list, using the cached one)"
TEMPLATE="$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E '^debian-12-standard_.*_amd64\.tar\.(zst|gz|xz)$' | sort -V | tail -n1)"
[[ -n $TEMPLATE ]] || die "no Debian 12 template found in 'pveam available'"
if pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  echo "  $TEMPLATE already downloaded"
else
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

# ---------------------------------------------------------------- container
step "Creating container $CTID"
net="name=eth0,bridge=$BRIDGE,ip=$IP${GW:+,gw=$GW}"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_HOSTNAME" --ostype debian \
  --cores "$CORES" --memory "$MEMORY" --swap 512 \
  --rootfs "$STORAGE:$DISK" --net0 "$net" \
  --unprivileged 1 --features nesting=1 --onboot 1 \
  --tags ovpn-panel --description "OpenVPN + ovpn-panel" \
  ${SSH_KEY:+--ssh-public-keys "$SSH_KEY"}

# OpenVPN needs /dev/net/tun inside the container
cat >> "/etc/pve/lxc/$CTID.conf" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net dev/net none bind,create=dir
EOF

# from here on, a failed install keeps the container around for inspection
on_fail() {
  rm -rf "$WORK"
  printf '\n\033[31mInstallation stopped.\033[0m Container %s was kept for debugging:\n' "$CTID"
  printf '  pct enter %s              # to inspect it\n  pct destroy %s --purge   # to remove it\n' "$CTID" "$CTID"
}
trap on_fail EXIT

step "Starting the container and waiting for the network"
pct start "$CTID"
for i in $(seq 60); do
  if pct exec "$CTID" -- sh -c 'ip -4 route show default | grep -q . && getent hosts deb.debian.org >/dev/null' 2>/dev/null; then
    break
  fi
  [[ $i -lt 60 ]] || die "the container has no Internet access (check DHCP/DNS)"
  sleep 2
done
CT_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo "  Container IP: $CT_IP"

step "Installing OpenVPN and the panel"
install_app "$CTID" \
  PUBLIC_HOST="$PUBLIC_HOST" OVPN_PORT="$OVPN_PORT" OVPN_PROTO="$OVPN_PROTO" PANEL_PORT="$PANEL_PORT"

CREDS="$(pct exec "$CTID" -- cat /root/ovpn-panel-credentials.txt)"
ADMIN_USER="$(sed -n 's/^username=//p' <<<"$CREDS")"
ADMIN_PASSWORD="$(sed -n 's/^password=//p' <<<"$CREDS")"
trap 'rm -rf "$WORK"' EXIT

cat <<EOF

$(bold "Done. Container $CTID is ready.")

  Panel        https://$CT_IP:$PANEL_PORT/
  Username     $ADMIN_USER
  Password     $ADMIN_PASSWORD
               (also stored in the container: pct exec $CTID -- cat /root/ovpn-panel-credentials.txt)

  On your router, forward $OVPN_PORT/$OVPN_PROTO to $CT_IP.
  The panel does not need to be exposed to the Internet: reaching it from your LAN is enough.
$( [[ -n $PUBLIC_HOST ]] || echo "
  No public endpoint set: open Settings in the panel and set it before creating users." )
EOF
