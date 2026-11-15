#!/usr/bin/env bash
# Full test without Proxmox, in a Docker "node" (Debian 12 + systemd, fake pct/pveam):
#  1. installs the PREVIOUS release (UPGRADE_FROM, default tag v0.1.0), checks it, then upgrades it in place
#  2. checks the real system: which user runs what, file permissions, sandbox, firewall, helper socket
#  3. runs the end-to-end tests with real OpenVPN clients (two of them, to test client isolation)
#  4. backup -> destroy the PKI and the database -> restore -> the old certificates must still work
#  5. login lockout, whole-container restart, services coming back by themselves
# The test panel stays up at https://localhost:9443 (docker compose -f tests/docker-compose.yml down -v to stop it).
# UPGRADE_FROM="" skips the upgrade scenario and installs the current version directly.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite container paths
C="docker compose -f tests/docker-compose.yml"
UPGRADE_FROM="${UPGRADE_FROM-v0.1.0}"
BACKUP_PASS="a long test passphrase"
mkdir -p tests/.cache dist
fails=0
PW=""

ok() { echo "  OK   $*"; }
bad() { echo "  FAIL $*"; fails=$((fails + 1)); }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
checkno() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$what"; else ok "$what"; fi; }
equals() { local what="$1" want="$2" got="$3"; if [[ $got == "$want" ]]; then ok "$what"; else bad "$what (wanted '$want', got '$got')"; fi; }
pve() { $C exec -T pve "$@"; }
e2e() { local phase="$1"; shift; $C run --rm --build -T -e PHASE="$phase" -e ADMIN_PASSWORD="$PW" "$@" e2e || fails=$((fails + 1)); }
wait_node() {
  for _ in $(seq 90); do
    $C exec -T pve systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && return
    sleep 1
  done
}
wait_services() {
  for _ in $(seq 90); do
    $C exec -T pve systemctl is-active -q openvpn-server@server ovpn-panel-helper ovpn-panel ovpn-panel-firewall 2>/dev/null && return
    sleep 1
  done
}
refresh_ca() { pve cat /etc/ovpn-panel/ca.crt > tests/.cache/panel-ca.crt; }
read_pw() { PW="$(pve sed -n 's/^password=//p' /root/ovpn-panel-credentials.txt | tr -d '\r')"; }

echo "==> Build"
python tools/build.py
HAVE_OLD=0
if [[ -n $UPGRADE_FROM ]] && git rev-parse -q --verify "$UPGRADE_FROM^{commit}" >/dev/null; then
  rm -rf tests/.cache/old-src dist/old
  mkdir -p tests/.cache/old-src dist/old
  git archive "$UPGRADE_FROM" | tar -x -C tests/.cache/old-src
  (cd tests/.cache/old-src && python tools/build.py >/dev/null)
  cp tests/.cache/old-src/dist/ovpn-panel-proxmox.sh dist/old/
  HAVE_OLD=1
  echo "    previous release: $UPGRADE_FROM"
fi

echo "==> Simulated Proxmox node"
$C down -v >/dev/null 2>&1 || true
$C up -d --build pve
wait_node

if [[ $HAVE_OLD == 1 ]]; then
  echo "==> Installing the previous release"
  pve bash /dist/old/ovpn-panel-proxmox.sh --yes --public-host pve
  read_pw
  e2e old
  echo "==> Upgrading in place with the current release"
  pve bash /dist/ovpn-panel-proxmox.sh --update 100
  refresh_ca
  wait_services
  e2e upgraded
else
  echo "==> Installing the current release"
  pve bash /dist/ovpn-panel-proxmox.sh --yes --public-host pve
  read_pw
  refresh_ca
fi

echo "==> Node checks (Proxmox script)"
check "tun enabled in the container config" pve grep -q "lxc.mount.entry: /dev/net" /etc/pve/lxc/100.conf
check "latest Debian 12 template downloaded" pve grep -q "pveam download local debian-12-standard_12.7-1_amd64.tar.zst" /var/log/pve-sim.log
check "the container is unprivileged" pve grep -q -- "--unprivileged 1" /var/log/pve-sim.log
check "...with nesting, which the systemd sandbox needs" pve grep -q -- "--features nesting=1" /var/log/pve-sim.log

echo "==> System checks: privileges"
PANEL_PID="$(pve systemctl show -p MainPID --value ovpn-panel | tr -d '\r')"
HELPER_PID="$(pve systemctl show -p MainPID --value ovpn-panel-helper | tr -d '\r')"
equals "the panel runs as the unprivileged user" "ovpn-panel" "$(pve ps -o user= -p "$PANEL_PID" | tr -d ' \r')"
equals "the helper is the only root process of the service" "root" "$(pve ps -o user= -p "$HELPER_PID" | tr -d ' \r')"
check "the panel has no capabilities at all" pve grep -qE '^CapEff:[[:space:]]+0+$' "/proc/$PANEL_PID/status"
equals "no new privileges" "yes" "$(pve systemctl show -p NoNewPrivileges --value ovpn-panel | tr -d '\r')"
equals "read-only filesystem for the panel" "strict" "$(pve systemctl show -p ProtectSystem --value ovpn-panel | tr -d '\r')"
checkno "the panel user cannot read the OpenVPN CA key" pve runuser -u ovpn-panel -- cat /etc/openvpn/easy-rsa/pki/private/ca.key
checkno "...nor the OpenVPN server key" pve runuser -u ovpn-panel -- cat /etc/openvpn/server/server.key
checkno "...nor the tls-crypt key" pve runuser -u ovpn-panel -- cat /etc/openvpn/server/tc.key
checkno "...nor the panel CA key" pve runuser -u ovpn-panel -- cat /etc/ovpn-panel/ca/ca.key
checkno "...and cannot rewrite server.conf" pve runuser -u ovpn-panel -- sh -c 'echo "script-security 2" >> /etc/openvpn/server/server.conf'
check "it can read its own HTTPS key" pve runuser -u ovpn-panel -- cat /etc/ovpn-panel/tls.key
checkno "inside its sandbox /etc/openvpn does not even exist" pve nsenter -t "$PANEL_PID" -m ls /etc/openvpn/easy-rsa
checkno "inside its sandbox /etc is read-only" pve nsenter -t "$PANEL_PID" -m touch /etc/ovpn-panel/probe
check "inside its sandbox its data directory is writable" pve nsenter -t "$PANEL_PID" -m sh -c 'touch /var/lib/ovpn-panel/.probe && rm /var/lib/ovpn-panel/.probe'
equals "the helper socket is root:ovpn-panel 0660" "root:ovpn-panel 660" "$(pve stat -c '%U:%G %a' /run/ovpn-panel/helper.sock | tr -d '\r')"
check "the panel user can talk to the helper" pve runuser -u ovpn-panel -- python3 -c "import socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/ovpn-panel/helper.sock')"
checkno "another local user cannot" pve runuser -u nobody -- python3 -c "import socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/ovpn-panel/helper.sock')"
equals "the PKI directory is root-only" "700" "$(pve stat -c %a /etc/openvpn/easy-rsa | tr -d '\r')"
equals "the server key is root-only" "600" "$(pve stat -c %a /etc/openvpn/server/server.key | tr -d '\r')"
equals "the tls-crypt key is root-only" "600" "$(pve stat -c %a /etc/openvpn/server/tc.key | tr -d '\r')"
equals "the panel CA key is root-only" "600" "$(pve stat -c %a /etc/ovpn-panel/ca/ca.key | tr -d '\r')"
equals "the panel HTTPS key is root:ovpn-panel 0640" "root:ovpn-panel 640" "$(pve stat -c '%U:%G %a' /etc/ovpn-panel/tls.key | tr -d '\r')"
equals "the panel owns its data" "ovpn-panel" "$(pve stat -c %U /var/lib/ovpn-panel/panel.db | tr -d '\r')"
equals "...and nobody else can read it" "700" "$(pve stat -c %a /var/lib/ovpn-panel | tr -d '\r')"
checkno "the old NAT unit is gone" pve test -f /etc/systemd/system/ovpn-panel-nat.service

echo "==> System checks: firewall"
check "client isolation rule loaded" pve sh -c "iptables -S OVPNP_FWD | grep -q -- '-i tun+ -o tun+ -j DROP'"
check "NAT rule loaded" pve sh -c "iptables -t nat -S OVPNP_NAT | grep -q MASQUERADE"
check "panel port restricted to the local network, then dropped" pve sh -c "iptables -S OVPNP_IN | grep -- '--dport 8443' | tail -1 | grep -q DROP"
checkno "no leftover NAT rule from the previous version" pve sh -c "iptables -t nat -S POSTROUTING | grep -q -- '-s 10.9.0.0/24 -o'"

echo "==> End-to-end tests"
e2e main

echo "==> Maintenance job and command line"
check "the maintenance job runs" pve systemctl start ovpn-panel-maintenance.service
equals "...and succeeded" "success" "$(pve systemctl show -p Result --value ovpn-panel-maintenance.service | tr -d '\r')"
equals "pki-status lists every certificate" "6" "$(pve ovpn-panel pki-status | grep -c . | tr -d '\r')"
check "the audit command line works" pve ovpn-panel audit 5
checkno "activation tokens never reach the journal" pve sh -c "journalctl -u ovpn-panel --no-pager | grep -E '/(dl|p)/[A-Za-z0-9_-]{20,}'"
check "audit events are mirrored to the journal" pve sh -c "journalctl -u ovpn-panel --no-pager | grep -q '\"action\": \"user.create\"'"

echo "==> Disaster recovery"
e2e dr_pre
pve sh -c "echo '$BACKUP_PASS' > /root/pass && echo wrong-passphrase-here > /root/wrongpass && chmod 600 /root/pass /root/wrongpass"
pve rm -f /root/dr.bak
check "a backup can be taken" pve ovpn-panel backup --out /root/dr.bak --passphrase-file /root/pass
checkno "a backup is never overwritten" pve ovpn-panel backup --out /root/dr.bak --passphrase-file /root/pass
equals "the backup is private" "600" "$(pve stat -c %a /root/dr.bak | tr -d '\r')"
checkno "the backup does not contain the CA key in clear" pve sh -c "grep -a -q 'PRIVATE KEY' /root/dr.bak"
pve sh -c "systemctl stop ovpn-panel openvpn-server@server && rm -rf /etc/openvpn/easy-rsa/pki /etc/openvpn/server /var/lib/ovpn-panel /etc/ovpn-panel/ca /etc/ovpn-panel/tls.key /etc/ovpn-panel/tls.crt /etc/ovpn-panel/ca.crt"
echo "    (PKI, database and TLS material destroyed)"
checkno "a wrong passphrase restores nothing" pve ovpn-panel restore /root/dr.bak --passphrase-file /root/wrongpass --yes
check "the real passphrase restores everything" pve ovpn-panel restore /root/dr.bak --passphrase-file /root/pass --yes
wait_services
refresh_ca
e2e dr_post
equals "the data directory is private again after the restore" "ovpn-panel 700" "$(pve stat -c '%U %a' /var/lib/ovpn-panel | tr -d '\r')"

echo "==> Login lockout and restart"
e2e lockout
$C restart pve
wait_node
wait_services
e2e locked
check "the administrator can clear a lockout" pve ovpn-panel unlock
e2e smoke
check "the firewall rules came back with the container" pve sh -c "iptables -S OVPNP_FWD | grep -q 'tun+'"

echo
if [[ $fails -eq 0 ]]; then
  echo "ALL TESTS PASSED"
else
  echo "$fails CHECK(S) FAILED"
fi
echo "Test panel: https://localhost:9443  (admin / $PW)"
exit $((fails > 0))
