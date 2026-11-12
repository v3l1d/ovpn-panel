#!/usr/bin/env bash
# Crea su Proxmox VE un container LXC Debian 12 con OpenVPN + ovpn-panel, gia' configurato.
# Da lanciare sulla shell del nodo Proxmox (Datacenter > nodo > Shell), come root:
#
#   bash ovpn-panel-proxmox.sh                         # interattivo
#   bash ovpn-panel-proxmox.sh --yes --public-host casa.ddns.net
#
# Opzioni (anche come variabili d'ambiente, es. CTID=120):
#   --ctid N            ID del container            (default: primo libero)
#   --hostname NOME     hostname                    (default: ovpn-panel)
#   --public-host H     DDNS o IP pubblico per i client (default: IP del container, da cambiare poi)
#   --storage S         storage per il disco        (default: local-lvm o il primo disponibile)
#   --template-storage S storage dei template       (default: local)
#   --bridge B          bridge di rete              (default: vmbr0)
#   --ip CIDR|dhcp      indirizzo IPv4              (default: dhcp)
#   --gw IP             gateway, con --ip statico
#   --disk GB --memory MB --cores N                 (default: 4 / 512 / 1)
#   --port N --proto udp|tcp                        porta OpenVPN (default: 1194/udp)
#   --panel-port N      porta del pannello          (default: 8443)
#   --ssh-key FILE      chiave pubblica per l'accesso root via SSH
#   --yes               non chiedere conferme
set -euo pipefail

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

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mErrore: %s\033[0m\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0; }

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
    -y|--yes) YES=1; shift ;;
    -h|--help) usage ;;
    *) die "opzione sconosciuta: $1 (vedi --help)" ;;
  esac
done

# ---------------------------------------------------------------- controlli
[[ $EUID -eq 0 ]] || die "esegui come root sulla shell del nodo Proxmox"
command -v pct >/dev/null && command -v pveam >/dev/null || die "pct/pveam non trovati: questo script va lanciato su un nodo Proxmox VE"
[[ $OVPN_PROTO == udp || $OVPN_PROTO == tcp ]] || die "--proto deve essere udp o tcp"
[[ $IP == dhcp || $IP =~ ^[0-9.]+/[0-9]+$ ]] || die "--ip deve essere dhcp o un CIDR (es. 192.168.1.50/24)"
[[ $IP == dhcp || -n $GW ]] || die "con --ip statico serve anche --gw"
[[ -z $SSH_KEY || -f $SSH_KEY ]] || die "chiave SSH non trovata: $SSH_KEY"

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
[[ ! -e /etc/pve/lxc/$CTID.conf ]] || die "il container $CTID esiste gia'"

if [[ -z $STORAGE ]]; then
  storages="$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1}')"
  if grep -qx local-lvm <<<"$storages"; then STORAGE=local-lvm; else STORAGE="$(head -n1 <<<"$storages")"; fi
  [[ -n $STORAGE ]] || die "nessuno storage attivo per i container: indica --storage"
fi

if [[ -z $PUBLIC_HOST && $YES != 1 && -t 0 ]]; then
  read -rp "DDNS o IP pubblico con cui i client raggiungono la VPN (invio = decidi dopo dal pannello): " PUBLIC_HOST
fi

# ---------------------------------------------------------------- sorgente dell'applicazione
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SCRIPT_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"
if grep -q '^__PAYLOAD_BELOW__$' "$SCRIPT_PATH" 2>/dev/null; then
  sed '1,/^__PAYLOAD_BELOW__$/d' "$SCRIPT_PATH" | base64 -d > "$WORK/app.tgz" || die "pacchetto incorporato danneggiato"
elif [[ -f $(dirname "$SCRIPT_PATH")/../app/install.sh ]]; then
  tar czf "$WORK/app.tgz" -C "$(dirname "$SCRIPT_PATH")/.." app
else
  die "applicazione non trovata: usa dist/ovpn-panel-proxmox.sh oppure lancia lo script dalla cartella del repo"
fi

# ---------------------------------------------------------------- riepilogo
bold "Verra' creato questo container:"
cat <<EOF
  ID           $CTID ($CT_HOSTNAME)
  Risorse      $CORES core, $MEMORY MB RAM, $DISK GB su $STORAGE
  Rete         $BRIDGE, IP $IP${GW:+, gateway $GW}
  OpenVPN      porta $OVPN_PORT/$OVPN_PROTO, endpoint ${PUBLIC_HOST:-<IP del container, da impostare nel pannello>}
  Pannello     porta $PANEL_PORT (HTTPS)
EOF
if [[ $YES != 1 ]]; then
  read -rp "Procedo? [s/N] " ok
  [[ $ok =~ ^[sSyY]$ ]] || { echo "Annullato."; exit 0; }
fi

# ---------------------------------------------------------------- template
step "Template Debian 12"
pveam update >/dev/null 2>&1 || echo "  (aggiornamento lista template non riuscito, uso quella in cache)"
TEMPLATE="$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E '^debian-12-standard_.*_amd64\.tar\.(zst|gz|xz)$' | sort -V | tail -n1)"
[[ -n $TEMPLATE ]] || die "template Debian 12 non trovato in 'pveam available'"
if pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  echo "  $TEMPLATE gia' presente"
else
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

# ---------------------------------------------------------------- container
step "Creazione del container $CTID"
net="name=eth0,bridge=$BRIDGE,ip=$IP${GW:+,gw=$GW}"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_HOSTNAME" --ostype debian \
  --cores "$CORES" --memory "$MEMORY" --swap 512 \
  --rootfs "$STORAGE:$DISK" --net0 "$net" \
  --unprivileged 1 --features nesting=1 --onboot 1 \
  --tags ovpn-panel --description "OpenVPN + ovpn-panel" \
  ${SSH_KEY:+--ssh-public-keys "$SSH_KEY"}

# OpenVPN ha bisogno di /dev/net/tun dentro il container
cat >> "/etc/pve/lxc/$CTID.conf" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net dev/net none bind,create=dir
EOF

# da qui in poi, in caso di errore il container resta per poterlo esaminare
on_fail() {
  rm -rf "$WORK"
  printf '\n\033[31mInstallazione interrotta.\033[0m Il container %s e'"'"' rimasto per il debug:\n' "$CTID"
  printf '  pct enter %s              # per esaminarlo\n  pct destroy %s --purge   # per eliminarlo\n' "$CTID" "$CTID"
}
trap on_fail EXIT

step "Avvio e attesa della rete"
pct start "$CTID"
for i in $(seq 60); do
  if pct exec "$CTID" -- sh -c 'ip -4 route show default | grep -q . && getent hosts deb.debian.org >/dev/null' 2>/dev/null; then
    break
  fi
  [[ $i -lt 60 ]] || die "il container non ha accesso a Internet (DHCP/DNS?)"
  sleep 2
done
CT_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo "  IP del container: $CT_IP"

step "Installazione di OpenVPN e del pannello"
pct push "$CTID" "$WORK/app.tgz" /root/ovpn-panel.tgz
pct exec "$CTID" -- sh -c 'rm -rf /root/ovpn-panel-src && mkdir -p /root/ovpn-panel-src && tar xzf /root/ovpn-panel.tgz -C /root/ovpn-panel-src && rm /root/ovpn-panel.tgz'
pct exec "$CTID" -- env \
  PUBLIC_HOST="$PUBLIC_HOST" OVPN_PORT="$OVPN_PORT" OVPN_PROTO="$OVPN_PROTO" PANEL_PORT="$PANEL_PORT" \
  bash /root/ovpn-panel-src/app/install.sh

CREDS="$(pct exec "$CTID" -- cat /root/ovpn-panel-credentials.txt)"
ADMIN_USER="$(sed -n 's/^username=//p' <<<"$CREDS")"
ADMIN_PASSWORD="$(sed -n 's/^password=//p' <<<"$CREDS")"
trap 'rm -rf "$WORK"' EXIT

cat <<EOF

$(bold "Fatto. Container $CTID pronto.")

  Pannello     https://$CT_IP:$PANEL_PORT/
  Utente       $ADMIN_USER
  Password     $ADMIN_PASSWORD
               (salvata anche nel container: pct exec $CTID -- cat /root/ovpn-panel-credentials.txt)

  Sul router inoltra $OVPN_PORT/$OVPN_PROTO verso $CT_IP.
  Il pannello non serve esporlo su Internet: basta raggiungerlo dalla LAN.
  Se vuoi che gli utenti aprano i QR da fuori casa, inoltra anche $PANEL_PORT/tcp.
$( [[ -n $PUBLIC_HOST ]] || echo "
  Endpoint pubblico non impostato: aprilo dal pannello in Impostazioni prima di creare utenti." )
EOF
