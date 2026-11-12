#!/usr/bin/env bash
# Prova completa senza Proxmox:
#  1. costruisce dist/ovpn-panel-proxmox.sh
#  2. lo lancia in un "nodo Proxmox" simulato (Debian 12 + systemd, pct/pveam finti)
#  3. collega un client OpenVPN vero e prova pannello e API
#  4. riavvia il container e controlla che i servizi ripartano da soli
# Alla fine il pannello resta su https://localhost:9443 (docker compose -f tests/docker-compose.yml down -v per chiudere).
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash su Windows: non convertire i percorsi del container
C="docker compose -f tests/docker-compose.yml"
mkdir -p tests/.cache

echo "==> Build"
python tools/build.py

echo "==> Nodo Proxmox simulato"
$C down -v >/dev/null 2>&1 || true
$C up -d --build pve
for _ in $(seq 60); do
  $C exec -T pve systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
  sleep 1
done

echo "==> Script Proxmox"
$C exec -T pve bash /dist/ovpn-panel-proxmox.sh --yes --public-host pve

echo "==> Controlli sul nodo"
$C exec -T pve sh -c 'grep -q "lxc.mount.entry: /dev/net" /etc/pve/lxc/100.conf' && echo "  OK   tun abilitato nella configurazione del container"
$C exec -T pve sh -c 'grep -q "pveam download local debian-12-standard_12.7-1_amd64.tar.zst" /var/log/pve-sim.log' && echo "  OK   scaricato il template Debian 12 piu' recente"
$C exec -T pve sh -c 'grep -q "rootfs local-lvm:4" /var/log/pve-sim.log' && echo "  OK   disco su local-lvm"
$C exec -T pve cat /etc/ovpn-panel/tls.crt > tests/.cache/tls.crt
PW="$($C exec -T pve sed -n 's/^password=//p' /root/ovpn-panel-credentials.txt | tr -d '\r')"

echo "==> Test end-to-end"
$C run --rm --build -e ADMIN_PASSWORD="$PW" e2e

echo "==> Riavvio del container"
$C restart pve
for _ in $(seq 60); do
  $C exec -T pve systemctl is-active -q openvpn-server@server ovpn-panel ovpn-panel-nat 2>/dev/null && break
  sleep 1
done
$C exec -T pve systemctl is-active openvpn-server@server ovpn-panel ovpn-panel-nat
$C run --rm -e ADMIN_PASSWORD="$PW" -e SMOKE=1 e2e

echo
echo "Pannello di prova: https://localhost:9443  (admin / $PW)"
