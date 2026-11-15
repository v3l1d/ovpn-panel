#!/bin/sh
# Firewall rules for ovpn-panel, loaded by the ovpn-panel-firewall service.
#
#   NAT          VPN clients are masqueraded behind the container's address
#   FORWARD      VPN clients cannot reach each other through the kernel. Without this rule the
#                kernel would route client A's packets back out to client B, whatever the
#                client-to-client setting says (that option only governs OpenVPN's own forwarding).
#   INPUT        the panel port answers only to the listed networks (default: the local network),
#                so it is not reachable from the VPN clients or from the Internet
#
# Variables come from /etc/ovpn-panel/firewall.env, written by `ovpn-panel apply-firewall`.
set -eu

. /etc/ovpn-panel/firewall.env
IPT=iptables

flush() {
  while $IPT -t nat -D POSTROUTING -j OVPNP_NAT 2>/dev/null; do :; done
  $IPT -t nat -F OVPNP_NAT 2>/dev/null || true
  $IPT -t nat -X OVPNP_NAT 2>/dev/null || true
  for pair in "INPUT OVPNP_IN" "FORWARD OVPNP_FWD"; do
    # shellcheck disable=SC2086
    set -- $pair
    while $IPT -D "$1" -j "$2" 2>/dev/null; do :; done
    $IPT -F "$2" 2>/dev/null || true
    $IPT -X "$2" 2>/dev/null || true
  done
}

case "${1:-}" in
  start)
    flush

    $IPT -t nat -N OVPNP_NAT
    $IPT -t nat -A OVPNP_NAT -s "$SUBNET" -o "$WAN_IF" -j MASQUERADE
    $IPT -t nat -A POSTROUTING -j OVPNP_NAT

    $IPT -N OVPNP_IN
    $IPT -A OVPNP_IN -i lo -j RETURN
    for cidr in $PANEL_ALLOW; do
      $IPT -A OVPNP_IN -p tcp --dport "$PANEL_PORT" -s "$cidr" -j RETURN
    done
    $IPT -A OVPNP_IN -p tcp --dport "$PANEL_PORT" -j DROP
    $IPT -I INPUT 1 -j OVPNP_IN

    $IPT -N OVPNP_FWD
    $IPT -A OVPNP_FWD -i tun+ -o tun+ -j DROP
    $IPT -I FORWARD 1 -j OVPNP_FWD
    ;;
  stop)
    flush
    ;;
  *)
    echo "usage: $0 start|stop" >&2
    exit 2
    ;;
esac
