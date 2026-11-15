# Installation guide

- [Before you start](#before-you-start)
- [Install on Proxmox VE](#install-on-proxmox-ve)
- [Installer options](#installer-options)
- [After the installation](#after-the-installation)
- [Connecting a device](#connecting-a-device)
- [Exposing the panel (optional)](#exposing-the-panel-optional)
- [Install on an existing Debian 12 system](#install-on-an-existing-debian-12-system)
- [Upgrading](#upgrading)
- [Backup and restore](#backup-and-restore)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [Files and services](#files-and-services)

## Before you start

You need:

- **A Proxmox VE node, version 7 or later**, with Internet access. The container downloads Debian packages and a few
  Python packages during the install.
- **A public IPv4 address**, and the ability to forward one UDP port on your router to the container.
- **A DDNS name** (recommended) if your public IP changes, e.g. from DuckDNS, No-IP or your router's
  built-in DDNS client. Profiles contain the endpoint: with a name, an IP change does not break them.
- **A LAN subnet that is not too common.** If your home LAN is `192.168.1.0/24` or `192.168.0.0/24`,
  many networks your users connect from (hotels, friends, phone hotspots) use the same range. Devices
  on those networks cannot reach your LAN through the VPN, because their own network wins. Internet
  access through the VPN still works. See [Troubleshooting](#connected-but-some-lan-devices-or-services-are-unreachable).

The default container uses 1 core, 512 MB RAM and 4 GB of disk, which is plenty for dozens of users.

## Install on Proxmox VE

Open the **Shell** of your node (*Datacenter → your node → Shell*) and run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/v3l1d/ovpn-panel/main/proxmox/create-ct.sh)"
```

The script will:

1. Ask for the public endpoint (DDNS name or public IP). Press Enter to set it later from the panel.
2. Show a summary (ID, resources, storage, network) and ask for confirmation.
3. Download the latest Debian 12 template, if it is not there yet.
4. Create an **unprivileged** container that starts at boot and has access to `/dev/net/tun`.
5. Install OpenVPN, create the PKI (EC keys, `tls-crypt`), enable NAT, and install the panel as systemd services.
6. Print the panel address, the admin user name and a generated password.

The password is also stored in the container:

```bash
pct exec <ID> -- cat /root/ovpn-panel-credentials.txt
```

If anything fails, the container is kept so you can inspect it with `pct enter <ID>`. The script prints the
command to remove it.

### Offline install, or without GitHub access

Build a single self-contained file from a checkout of the repository, on any machine with Python 3:

```bash
python tools/build.py      # -> dist/ovpn-panel-proxmox.sh
```

Copy it to the node, e.g. `scp dist/ovpn-panel-proxmox.sh root@proxmox:`, and run
`bash ovpn-panel-proxmox.sh`. It accepts the same options.

You can also clone the repository on the node and run `bash proxmox/create-ct.sh`. It uses the `app/`
folder next to it.

## Installer options

Every option can also be passed as an environment variable, e.g. `CTID=120 bash create-ct.sh`.

| Option | Default | Description |
|---|---|---|
| `--public-host HOST` | asked, or the container IP | DDNS name or public IP that clients connect to |
| `--ctid N` | next free ID | container ID |
| `--hostname NAME` | `ovpn-panel` | container hostname |
| `--storage NAME` | `local-lvm`, else the first available | storage for the root disk |
| `--template-storage NAME` | `local` | where the Debian template is stored |
| `--bridge NAME` | `vmbr0` | network bridge |
| `--ip CIDR` / `--gw IP` | DHCP | static address, e.g. `--ip 192.168.1.50/24 --gw 192.168.1.1` |
| `--disk GB` / `--memory MB` / `--cores N` | 4 / 512 / 1 | resources |
| `--port N` / `--proto udp\|tcp` | 1194 / udp | OpenVPN port and protocol |
| `--panel-port N` | 8443 | panel HTTPS port |
| `--ssh-key FILE` | none | public key for root SSH access to the container |
| `--repo URL` | this repository | where to download the app from (forks, self-hosted Git) |
| `--update ID` | | upgrade an existing container, see [Upgrading](#upgrading) |
| `--yes` | | do not ask for confirmation |

A **static IP** is recommended. The port forward on your router points to the container, so its address
should not change. Either use `--ip` or reserve the address in your DHCP server.

Fully non-interactive example:

```bash
bash create-ct.sh --yes --public-host vpn.example.net --ip 192.168.1.50/24 --gw 192.168.1.1
```

## After the installation

1. **Forward the OpenVPN port** on your router to the container IP: 1194/udp unless you changed it.
   If your router forwards a different external port to it, enter that external port as *Public port* in
   *Settings*.
2. **Open the panel** at `https://<container-ip>:8443` and sign in. The browser warns about the
   self-signed certificate the first time; this is expected.
3. **Check *Settings → Public address*.** The endpoint must be your DDNS name or public IP. The overview
   shows a warning while it is not set.
4. **Choose the routing** in *Settings → Routing and DNS*:
   - **All traffic**: browsing and LAN access go through the VPN. Useful on public Wi-Fi.
   - **Only some networks**: e.g. `192.168.1.0/24`. Only your LAN goes through the VPN; everything else
     uses the user's own connection.
   - **DNS for clients**: set it to your router or local DNS server (e.g. `192.168.1.1`) if users must
     reach devices by name, e.g. `nas.lan`.
5. **Change the admin password** in *Settings → Admin password* if you like.

## Connecting a device

1. In the panel, go to *Overview → New user*, enter a name and click **Create and show QR**.
2. On the phone, install **OpenVPN Connect** (App Store / Google Play).
3. Scan the QR code with the camera. It opens the activation page.
4. Download the profile, open it with OpenVPN Connect, then tap **Import** and **Connect**.

The activation page is served by the panel, so the phone must be able to reach the panel. That is the case
on your home Wi-Fi. If the user is elsewhere, either:

- use the **Download .ovpn** button and send the file over a private channel, or
- [expose the panel](#exposing-the-panel-optional).

Activation links expire after 72 hours. You can create a new one at any time with the QR button on the
*Users* page.

On a computer, import the downloaded `.ovpn` file into OpenVPN Connect, or into any OpenVPN 2.5+ client.

## Exposing the panel (optional)

You only need this if users must open activation links from outside your network. **Do not simply
forward port 8443.** Put the panel behind a reverse proxy that has a valid certificate (e.g. Let's
Encrypt for your DDNS name).

Example with [Caddy](https://caddyserver.com/) on another machine or container:

```
vpn.example.net {
    reverse_proxy https://192.168.1.50:8443 {
        transport http {
            tls_insecure_skip_verify   # the panel's own certificate is self-signed
        }
    }
}
```

Then set *Settings → Public panel URL* to `https://vpn.example.net`, so that QR codes and links use it.

Nginx Proxy Manager, Traefik or any other proxy works the same way: the backend scheme is `https` and the
backend certificate is self-signed.

## Install on an existing Debian 12 system

The container installer is a plain script. It works on any Debian 12 system with systemd: VM, bare metal,
or an LXC container you created yourself.

```bash
git clone https://github.com/v3l1d/ovpn-panel.git
cd ovpn-panel
sudo PUBLIC_HOST=vpn.example.net bash app/install.sh
```

Optional variables:

| Variable | Default | Description |
|---|---|---|
| `PUBLIC_HOST` | the machine's IP | DDNS name or public IP |
| `OVPN_PORT` | `1194` | OpenVPN port |
| `OVPN_PROTO` | `udp` | `udp` or `tcp` |
| `OVPN_SUBNET` | `10.9.0.0/24` | VPN client network; must not overlap your LAN |
| `PANEL_PORT` | `8443` | panel HTTPS port |

In your own LXC container, OpenVPN needs the tun device. On the Proxmox host, add these lines to
`/etc/pve/lxc/<ID>.conf` and restart the container:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net dev/net none bind,create=dir
```

## Upgrading

From the Proxmox node shell:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/v3l1d/ovpn-panel/main/proxmox/create-ct.sh)" -- --update <ID>
```

On a manual install, pull the repository and run `sudo bash app/install.sh` again.

Upgrades keep users, certificates, settings, the admin account and the ports chosen at install time.
Connected clients drop for a few seconds while OpenVPN restarts.

## Backup and restore

The simplest backup is a regular Proxmox backup of the container (*Datacenter → Backup*), which includes
everything.

To back up only the data, save these paths:

| Path | Contents |
|---|---|
| `/etc/openvpn/easy-rsa/pki` | certificate authority and all client certificates |
| `/etc/openvpn/server` | server keys, `tls-crypt` key, CRL, disabled users |
| `/var/lib/ovpn-panel/panel.db` | users, settings, admin accounts |
| `/etc/ovpn-panel` | install-time configuration and the panel's TLS certificate |

```bash
pct exec <ID> -- tar czf - /etc/openvpn /var/lib/ovpn-panel /etc/ovpn-panel > ovpn-panel-backup.tgz
```

To restore:
1. Install a new container with the same options.
2. Stop the services: `systemctl stop ovpn-panel openvpn-server@server`.
3. Extract the archive over `/` and start the services again.

**The PKI is the important part.** Without it, every user needs a new profile.

## Uninstalling

```bash
pct stop <ID> && pct destroy <ID> --purge
```

Then remove the port forward from your router.

## Troubleshooting

### `/dev/net/tun is not available`

The container cannot use the tun device. The Proxmox installer configures it automatically. For a
container you created yourself, see [Install on an existing Debian 12 system](#install-on-an-existing-debian-12-system).

### OpenVPN does not start

```bash
pct exec <ID> -- journalctl -u openvpn-server@server -n 50 --no-pager
```

In LXC, the Debian unit's `LimitNPROC=10` prevents OpenVPN from starting. The installer adds an override
in `/etc/systemd/system/openvpn-server@server.service.d/ovpn-panel.conf`; check that it exists.

### The client never connects

Check these in order:
- **Port forward:** the OpenVPN port must be forwarded to the container IP, with the right protocol (UDP by default).
- **Endpoint and port:** *Settings → Public address* must hold your current public IP or DDNS name. If you
  change it, download the profile again: existing profiles keep the old endpoint.
- **Testing from inside your network:** connecting from your own LAN to your public IP requires NAT
  loopback on the router. Test from mobile data instead.
- **Server log:** *Server → Log* in the panel. If nothing appears there when the client tries to connect,
  the packets are not reaching the container.

### Connected, but no Internet and no LAN access

The tunnel is up, but the container does not forward traffic. Check:

```bash
pct exec <ID> -- sysctl net.ipv4.ip_forward                  # must be 1
pct exec <ID> -- iptables -t nat -S POSTROUTING             # must contain a MASQUERADE rule for 10.9.0.0/24
pct exec <ID> -- systemctl status ovpn-panel-nat
```

### Connected, but some LAN devices or services are unreachable

Things to check:

- **Same subnet on both ends.** If the client's local network uses the same range as your LAN (e.g. both
  `192.168.1.0/24`), traffic for that range stays on the client's local network.
  - **Fix:** renumber your LAN to a less common range, e.g. `192.168.57.0/24`.
- **Names instead of addresses.** Clients use the DNS servers set in *Settings → Routing and DNS*
  (`1.1.1.1` and `9.9.9.9` by default), which do not know your local names.
  - **Check:** try the IP address and port directly, e.g. `http://192.168.1.20:8096`.
  - **Fix:** set the DNS to your router or local DNS server.
- **Public domain for a home service.** A domain that resolves to your public IP, e.g. a Jellyfin or
  Nextcloud domain, needs NAT loopback on the router when used from inside the VPN.
  - **Fix:** use the local address, or have your local DNS resolve that name to the internal IP.
- **Apps that discover servers automatically.** Apps like the Jellyfin client find servers through
  broadcast, which does not cross the VPN. Add the server by address in the app.
- **Firewall on the target.** VPN traffic reaches your LAN from the container's IP, not from `10.9.0.x`.
  - **Check:** whether the Proxmox firewall or the service's own allow list blocks that address.

### Forgot the admin password

```bash
pct enter <ID>
ovpn-panel reset-password admin
```

### Where are the logs?

- OpenVPN: *Server → Log* in the panel, or `journalctl -u openvpn-server@server`
- Panel: `journalctl -u ovpn-panel`

## Files and services

| Service | Purpose |
|---|---|
| `openvpn-server@server` | OpenVPN server |
| `ovpn-panel` | web panel (uvicorn, HTTPS) |
| `ovpn-panel-nat` | NAT rule for the VPN network |

| Path | Purpose |
|---|---|
| `/opt/ovpn-panel` | application and its Python virtualenv |
| `/etc/ovpn-panel/config.json` | install-time settings (ports, protocol, subnet) |
| `/etc/ovpn-panel/tls.{crt,key}` | panel certificate |
| `/var/lib/ovpn-panel/panel.db` | database |
| `/etc/openvpn/server/server.conf` | generated by the panel, do not edit by hand |
| `/run/openvpn-mgmt.sock` | OpenVPN management socket (root only) |
| `/root/ovpn-panel-credentials.txt` | initial admin credentials |
