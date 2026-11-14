#!/usr/bin/env bash
# Full test without Proxmox:
#  1. builds dist/ovpn-panel-proxmox.sh
#  2. runs it on a simulated "Proxmox node" (Debian 12 + systemd, fake pct/pveam)
#  3. connects a real OpenVPN client and exercises the panel and the API
#  4. restarts the container and checks that the services come back by themselves
# The test panel stays up at https://localhost:9443 (docker compose -f tests/docker-compose.yml down -v to stop it).
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite container paths
C="docker compose -f tests/docker-compose.yml"
mkdir -p tests/.cache

echo "==> Build"
python tools/build.py

echo "==> Simulated Proxmox node"
$C down -v >/dev/null 2>&1 || true
$C up -d --build pve
for _ in $(seq 60); do
  $C exec -T pve systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
  sleep 1
done

echo "==> Proxmox script"
$C exec -T pve bash /dist/ovpn-panel-proxmox.sh --yes --public-host pve

echo "==> Node checks"
$C exec -T pve sh -c 'grep -q "lxc.mount.entry: /dev/net" /etc/pve/lxc/100.conf' && echo "  OK   tun enabled in the container config"
$C exec -T pve sh -c 'grep -q "pveam download local debian-12-standard_12.7-1_amd64.tar.zst" /var/log/pve-sim.log' && echo "  OK   latest Debian 12 template downloaded"
$C exec -T pve sh -c 'grep -q "rootfs local-lvm:4" /var/log/pve-sim.log' && echo "  OK   root disk on local-lvm"
$C exec -T pve cat /etc/ovpn-panel/tls.crt > tests/.cache/tls.crt
PW="$($C exec -T pve sed -n 's/^password=//p' /root/ovpn-panel-credentials.txt | tr -d '\r')"

echo "==> End-to-end tests"
$C run --rm --build -e ADMIN_PASSWORD="$PW" e2e

echo "==> Container restart"
$C restart pve
for _ in $(seq 60); do
  $C exec -T pve systemctl is-active -q openvpn-server@server ovpn-panel ovpn-panel-nat 2>/dev/null && break
  sleep 1
done
$C exec -T pve systemctl is-active openvpn-server@server ovpn-panel ovpn-panel-nat
$C run --rm -e ADMIN_PASSWORD="$PW" -e SMOKE=1 e2e

echo
echo "Test panel: https://localhost:9443  (admin / $PW)"
