# "Nodo Proxmox" finto per provare dist/ovpn-panel-proxmox.sh senza un Proxmox vero.
# Debian 12 con systemd, come il container LXC creato dallo script; pct/pveam/pvesh/pvesm
# sono simulati e "pct exec" esegue i comandi qui dentro, quindi install.sh gira davvero.
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
