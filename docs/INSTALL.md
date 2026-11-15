# Installation guide

- [Before you start](#before-you-start)
- [Install on Proxmox VE](#install-on-proxmox-ve)
- [Installer options](#installer-options)
- [After the installation](#after-the-installation)
- [Connecting a device](#connecting-a-device)
- [Who can reach the panel](#who-can-reach-the-panel)
- [Exposing the panel (optional)](#exposing-the-panel-optional)
- [Install on an existing Debian 12 system](#install-on-an-existing-debian-12-system)
- [Upgrading](#upgrading)
- [Backup and restore](#backup-and-restore)
- [Command line reference](#command-line-reference)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [Files and services](#files-and-services)

See also [SECURITY.md](SECURITY.md) for how the installation is protected and what it does not cover.

## Before you start

You need:

- **A Proxmox VE node, version 7 or later**, with Internet access. The container downloads Debian packages and
  a few Python packages during the install.
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
bash -c "$(curl -fsSL https://raw.githubusercontent.com/v3l1d/ovpn-panel/v1.0.0/proxmox/create-ct.sh)"
```

The address contains a **release tag** (`v1.0.0`), not `main`: you run exactly what was released. If you
would rather read the script before running it:

```bash
curl -fsSLo create-ct.sh https://raw.githubusercontent.com/v3l1d/ovpn-panel/v1.0.0/proxmox/create-ct.sh
less create-ct.sh
bash create-ct.sh
```

The script will:

1. Ask for the public endpoint (DDNS name or public IP). Press Enter to set it later from the panel.
2. Show a summary (ID, resources, storage, network) and ask for confirmation.
3. Download the application and **verify its integrity** against the hash written in the script.
   It refuses to continue if the files differ from the release.
4. Download the latest Debian 12 template, if it is not there yet.
5. Create an **unprivileged** container that starts at boot, with `nesting` (needed by the systemd
   sandbox) and access to `/dev/net/tun`.
6. Install OpenVPN, create the PKI (EC keys, `tls-crypt`), the panel's private CA and HTTPS certificate,
   the firewall rules, and the services: OpenVPN, the unprivileged panel, the privileged helper, and a daily
   maintenance job.
7. Print the panel address, the admin user name and a generated password.

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
`bash ovpn-panel-proxmox.sh`. It accepts the same options. (The container still needs Internet access for the
Debian and Python packages.)

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
| `--panel-allow CIDRS` | the container's local network | networks allowed to reach the panel, comma-separated |
| `--ssh-key FILE` | none | public key for root SSH access to the container |
| `--repo URL` | this repository | where to download the app from (forks, self-hosted Git) |
| `--ref REF` | the release this script belongs to | tag or branch to download; **any other value skips the integrity check** |
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
   *Settings*. **Do not forward the panel port.**
2. **Install the panel's CA** (optional but recommended). The panel uses a private CA, so a browser warns
   the first time. Download `https://<container-ip>:8443/panel-ca.crt` (accept the warning once), compare its
   SHA-256 fingerprint with the one the installer printed (also shown in *Server → Certificates and HTTPS*),
   and install it as a trusted root certificate on the computers you administer from. The warning is gone for
   good, including after certificate renewals.
3. **Open the panel** at `https://<container-ip>:8443` and sign in.
4. **Check *Settings → Public address*.** The endpoint must be your DDNS name or public IP. The overview
   shows a warning while it is not set.
5. **Choose the routing** in *Settings → Routing and DNS*:
   - **All traffic**: browsing and LAN access go through the VPN. Useful on public Wi-Fi.
   - **Only some networks**: e.g. `192.168.1.0/24`. Only your LAN goes through the VPN; everything else
     uses the user's own connection.
   - **DNS for clients**: set it to your router or local DNS server (e.g. `192.168.1.1`) if users must
     reach devices by name, e.g. `nas.lan`.
6. **Review *Settings → Certificates and activation links***: certificate validity (2 years by default), how long
   an activation link lasts and how many downloads it allows.
7. **Take a first backup**: see [Backup and restore](#backup-and-restore).

## Connecting a device

1. In the panel, go to *Overview → New user*, enter a name and click **Create and show QR**.
2. On the phone, install **OpenVPN Connect** (App Store / Google Play).
3. Scan the QR code with the camera. It opens the activation page.
4. Download the profile, open it with OpenVPN Connect, then tap **Import** and **Connect**.

The activation page is served by the panel, so the phone must be able to reach the panel. The panel is only
reachable from your local network, so this works on your home Wi-Fi. If the user is elsewhere, either:

- use the **Download .ovpn** button and send the file over a private channel, or
- [expose the activation pages](#exposing-the-panel-optional) through a reverse proxy.

Activation links expire after 24 hours or 5 downloads (configurable). A new link cancels the previous one.
Create one at any time with the QR button on the *Users* page.

On a computer, import the downloaded `.ovpn` file into OpenVPN Connect, or into any OpenVPN 2.5+ client.

Each certificate expires (2 years by default). The *Users* page shows the date and flags certificates that
are close to expiring; use the renew button to replace one.

## Who can reach the panel

The container's firewall lets **only your local network** reach the panel port. VPN clients and the
Internet are refused, so a stolen phone with a valid profile cannot even load the login page.

To allow other networks (for example an admin VLAN), run inside the container:

```bash
pct exec <ID> -- ovpn-panel allow-panel 192.168.1.0/24,10.20.0.0/16
```

To also allow the VPN's own network (for example to administer from a phone over the VPN), add it to the list:
`ovpn-panel allow-panel 192.168.1.0/24,10.9.0.0/24`. Every VPN user can then open the login page, though not log
in without the password.

If the panel answers to a name other than the container's address or your public host name, add it so that its
certificate covers it (the panel refuses unknown host names):

```bash
pct exec <ID> -- ovpn-panel add-host panel.home.example
```

## Exposing the panel (optional)

You only need this if users must open activation links from outside your network. **Do not forward port
8443**: it would expose the login page and the whole admin API. Put a reverse proxy with a valid certificate
(e.g. Let's Encrypt for your DDNS name) in front, and let it pass **only the activation pages**:

Example with [Caddy](https://caddyserver.com/) on another machine or container:

```
vpn.example.net {
    @activation path /p/* /dl/*
    handle @activation {
        reverse_proxy https://192.168.1.50:8443 {
            transport http {
                tls_trust_pool file /path/to/panel-ca.crt   # the CA downloaded from the panel
                tls_server_name 192.168.1.50
            }
        }
    }
    respond 404
}
```

Then:
1. add the proxy's address as an allowed network: `ovpn-panel allow-panel 192.168.1.0/24,<proxy address>/32`;
2. add the name: `ovpn-panel add-host vpn.example.net` (the panel refuses unknown host names);
3. set *Settings → Public panel URL* to `https://vpn.example.net`, so QR codes and links use it;
4. if the proxy is on another machine, list it in `trusted_proxies` in `/etc/ovpn-panel/config.json`, so that the
   login rate limit sees real client addresses, then `systemctl restart ovpn-panel`.

With this setup the login page and the API are not reachable from the Internet at all.

## Install on an existing Debian 12 system

The container installer is a plain script. It works on any Debian 12 system with systemd: VM, bare metal,
or an LXC container you created yourself.

```bash
git clone --branch v1.0.0 https://github.com/v3l1d/ovpn-panel.git
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
| `PANEL_ALLOW` | the local network | networks allowed to reach the panel, comma-separated |
| `SANDBOX` | `1` | `0` skips the systemd sandboxing (troubleshooting only) |

In your own LXC container, OpenVPN needs the tun device and the sandbox needs nesting. On the Proxmox host, add
these lines to `/etc/pve/lxc/<ID>.conf`, enable *Options → Features → Nesting*, and restart the container:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net dev/net none bind,create=dir
```

## Upgrading

From the Proxmox node shell, with the script of the new release:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/v3l1d/ovpn-panel/v1.0.0/proxmox/create-ct.sh)" -- --update <ID>
```

On a manual install, check out the new tag and run `sudo bash app/install.sh` again.

Upgrades keep users, certificates, settings, the admin account and the ports chosen at install time. Connected
clients drop for a few seconds while OpenVPN restarts. **Take a backup first.**

**Upgrading from a version before 1.0** also moves the panel to the unprivileged user, adds the helper and the
firewall rules, replaces the self-signed certificate with one from a private CA (the browser warns again until you
install the new CA) and takes ownership of the data directory. Existing client certificates keep working; they
keep their original expiry date (the Users page shows it). The upgrade is exercised by the integration tests: they
install the previous release, create users, upgrade in place, and check that a profile issued before the upgrade
still connects.

## Backup and restore

The simplest backup is a regular Proxmox backup of the container (*Datacenter → Backup*), which includes
everything. For an encrypted backup of just the data, run inside the container:

```bash
pct exec <ID> -- ovpn-panel backup --out /root/ovpn-panel.bak
pct pull <ID> /root/ovpn-panel.bak ./ovpn-panel.bak
```

It asks for a passphrase of at least 12 characters (or use `--passphrase-file`). The backup contains the PKI
including the CA key, the database, and the configuration, encrypted with AES-256-GCM. Keep it and its passphrase
away from the container.

To restore (for example into a fresh container created by the installer):

```bash
pct push <ID> ./ovpn-panel.bak /root/ovpn-panel.bak
pct exec <ID> -- ovpn-panel restore /root/ovpn-panel.bak
```

The restore stops the services, replaces the PKI, database and configuration, fixes permissions and restarts
everything. Certificates issued before the backup keep working; certificates revoked before it stay revoked.

**Test your restore** on a spare container before you need it.

## Command line reference

Run inside the container (`pct enter <ID>`, or `pct exec <ID> -- ovpn-panel ...`).

| Command | Purpose |
|---|---|
| `ovpn-panel create-admin NAME` | add an administrator |
| `ovpn-panel reset-password NAME` | set a new password (signs that admin out everywhere) |
| `ovpn-panel unlock` | clear login lockouts |
| `ovpn-panel audit [N]` | show the last N audit events |
| `ovpn-panel pki-status` | expiry dates of every certificate, and the panel CA fingerprint |
| `ovpn-panel backup --out FILE` / `restore FILE` | encrypted backup and restore |
| `ovpn-panel allow-panel CIDRS` | networks that may reach the panel |
| `ovpn-panel add-host NAME` | let the panel answer to another name (reissues its certificate) |
| `ovpn-panel init-tls [--force]` | create or renew the panel's CA and certificate |
| `ovpn-panel apply-firewall` | reload the firewall rules from the configuration |
| `ovpn-panel maintenance` | what the daily job does: refresh the CRL, renew the panel certificate |

## Uninstalling

```bash
pct stop <ID> && pct destroy <ID> --purge
```

Then remove the port forward from your router.

## Troubleshooting

### `/dev/net/tun is not available`

The container cannot use the tun device. The Proxmox installer configures it automatically. For a
container you created yourself, see [Install on an existing Debian 12 system](#install-on-an-existing-debian-12-system).

### The panel does not start ("203/EXEC", "226/NAMESPACE" or similar)

The systemd sandbox needs a container with nesting enabled. Check:

```bash
pct config <ID> | grep features        # should show nesting=1
pct exec <ID> -- journalctl -u ovpn-panel -u ovpn-panel-helper -n 30 --no-pager
```

To confirm the sandbox is the cause, re-run the installer with `SANDBOX=0` (see
[Install on an existing Debian 12 system](#install-on-an-existing-debian-12-system)). The privilege separation
stays in place; only the extra systemd hardening is removed.

### "Invalid host header" or a certificate warning on a name

The panel only answers to the names it knows, and its certificate covers exactly those. Add the one you use:
`ovpn-panel add-host NAME`.

### I cannot reach the panel from my own PC

The firewall allows only the container's local network. If you are on another network (or on the VPN), allow it:
`ovpn-panel allow-panel 192.168.1.0/24,<your network>`.

### OpenVPN does not start

```bash
pct exec <ID> -- journalctl -u openvpn-server@server -n 50 --no-pager
```

In LXC, the Debian unit's `LimitNPROC=10` prevents OpenVPN from starting. The installer adds an override
in `/etc/systemd/system/openvpn-server@server.service.d/ovpn-panel.conf`; check that it exists.

If OpenVPN fails after you change routing or DNS in the panel, the panel restores the previous `server.conf` and
tells you; nothing is left half-applied.

### "The privileged helper is not running"

```bash
pct exec <ID> -- systemctl status ovpn-panel-helper
pct exec <ID> -- journalctl -u ovpn-panel-helper -n 30 --no-pager
```

### The client never connects

Check these in order:
- **Port forward:** the OpenVPN port must be forwarded to the container IP, with the right protocol (UDP by default).
- **Endpoint and port:** *Settings → Public address* must hold your current public IP or DDNS name. If you
  change it, download the profile again: existing profiles keep the old endpoint.
- **Testing from inside your network:** connecting from your own LAN to your public IP requires NAT
  loopback on the router. Test from mobile data instead.
- **Certificate expired or revoked:** the Users page shows the expiry; renew it if needed.
- **Server log:** *Server → Log* in the panel. If nothing appears there when the client tries to connect,
  the packets are not reaching the container.

### Connected, but no Internet and no LAN access

The tunnel is up, but the container does not forward traffic. Check:

```bash
pct exec <ID> -- sysctl net.ipv4.ip_forward                  # must be 1
pct exec <ID> -- iptables -t nat -S OVPNP_NAT                # must contain a MASQUERADE rule for 10.9.0.0/24
pct exec <ID> -- systemctl status ovpn-panel-firewall
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
- **VPN clients cannot see each other.** This is intended: the container's firewall blocks client-to-client
  traffic through the kernel. The *Clients can reach each other* setting turns it on.

### Forgot the admin password

```bash
pct exec <ID> -- ovpn-panel reset-password admin
```

### Locked out after failed logins

```bash
pct exec <ID> -- ovpn-panel unlock
```

### Where are the logs?

- OpenVPN: *Server → Log* in the panel, or `journalctl -u openvpn-server@server`
- Panel and audit trail: `journalctl -u ovpn-panel`, or the *Audit log* page
- Helper: `journalctl -u ovpn-panel-helper`

## Files and services

| Service | Runs as | Purpose |
|---|---|---|
| `openvpn-server@server` | root, then `nobody` | OpenVPN server |
| `ovpn-panel` | `ovpn-panel` (unprivileged, sandboxed) | web panel (uvicorn, HTTPS) |
| `ovpn-panel-helper` | root | privileged operations for the panel, over a unix socket |
| `ovpn-panel-firewall` | root (one-shot) | NAT, client isolation, panel access rules |
| `ovpn-panel-maintenance.timer` | root | daily: refresh the CRL, renew the panel certificate |

| Path | Purpose |
|---|---|
| `/opt/ovpn-panel` | application and its Python virtualenv (root-owned, read-only for the panel) |
| `/etc/ovpn-panel/config.json` | install-time settings (ports, protocol, subnet, hosts) |
| `/etc/ovpn-panel/ca/` | the panel's private CA (root only) |
| `/etc/ovpn-panel/tls.{crt,key}`, `ca.crt` | panel certificate and the published CA certificate |
| `/var/lib/ovpn-panel/panel.db` | database (owned by the panel user, mode 0700) |
| `/etc/openvpn/easy-rsa/` | the PKI, including the CA key (root only) |
| `/etc/openvpn/server/` | server certificate and keys, CRL, `server.conf` (generated by the helper, do not edit) |
| `/run/ovpn-panel/helper.sock` | the helper's socket |
| `/root/ovpn-panel-credentials.txt` | initial admin credentials |
