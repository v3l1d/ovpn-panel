# Fake "Proxmox node" to test dist/ovpn-panel-proxmox.sh without a real Proxmox.
# Debian 12 with systemd, like the LXC container the script creates; pct/pveam/pvesh/pvesm
# are simulated and "pct exec" runs commands right here, so install.sh really runs.
FROM debian:12

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus iproute2 procps ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl mask getty@tty1.service console-getty.service systemd-logind.service

COPY mocks/ /usr/local/sbin/
RUN chmod 755 /usr/local/sbin/* && mkdir -p /etc/pve/lxc

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
