# Security

This page describes how ovpn-panel is built to limit the damage of a mistake or a compromise, what each
protection costs you, and what it deliberately does not cover.

To report a vulnerability, please open a private security advisory on the GitHub repository rather than a
public issue.

- [Threat model](#threat-model)
- [Privilege separation](#privilege-separation)
- [Network exposure](#network-exposure)
- [HTTPS and host names](#https-and-host-names)
- [Certificates and the PKI](#certificates-and-the-pki)
- [Logins, sessions and the audit log](#logins-sessions-and-the-audit-log)
- [Activation links](#activation-links)
- [Supply chain](#supply-chain)
- [Backups and disaster recovery](#backups-and-disaster-recovery)
- [Known limitations](#known-limitations)
- [How this is tested](#how-this-is-tested)

## Threat model

The assets worth protecting, in order:

1. **The OpenVPN CA key.** Whoever holds it can issue a certificate for anyone and join your network.
2. **The users' private keys**, which are in every profile.
3. **Your home or lab network**, which every VPN client can reach.

The attackers considered:

- **Someone on the Internet.** The only port exposed is the OpenVPN one.
- **A VPN user, or a stolen device that is already authorized.** This is the realistic case. Being behind a
  VPN lowers the exposure of the panel but does not remove the risk of one compromised client, so the panel
  is isolated from the VPN network too.
- **A bug in the panel.** The panel is the largest piece of code and the one that parses untrusted input.
  Everything is arranged so that a bug there does not give access to the CA key or to root.

Not considered: someone with root on the container or the Proxmox host (they own everything), and
attacks on OpenVPN itself.

## Privilege separation

```
 browser ──HTTPS──►  ovpn-panel  (user ovpn-panel, no capabilities, read-only filesystem)
                          │  unix socket, root:ovpn-panel 0660, peer credentials checked
                          ▼
                    ovpn-panel-helper  (root; 13 operations, every argument validated)
                          │
        easy-rsa / server.conf / OpenVPN management socket / systemctl / journal
```

- **The panel runs as an unprivileged system user**, with systemd sandboxing: no new privileges, no
  capabilities, a read-only filesystem apart from its own data directory, `/etc/openvpn` invisible, no
  namespaces, only the socket families it needs.
- **Everything that needs root goes through the helper**, a small daemon on a unix socket. It exposes a fixed
  list of operations (the list is pinned by a test: adding one is a conscious decision). The socket is
  `root:ovpn-panel 0660`, and the helper also checks the peer's credentials, so other local users are dropped
  without an answer.
- **The helper does not trust the panel.** It validates every argument again (certificate names must match a
  strict pattern, numbers must be in range, networks must parse) and it builds `server.conf` itself from typed
  values. Without this, a compromised panel could add a directive such as `script-security 2` and `up`
  to `server.conf` and run commands as root. The generated file only ever contains known directives, and a
  test checks that.
- **The CA key never leaves root.** The panel cannot read it, and it is not exposed by any operation. The
  helper does return a client's own private key to build a profile, which is unavoidable because the profile
  contains it.
- **What the panel can still do if it is compromised:** read the users' profiles (through the helper), issue
  and revoke certificates, change the routing. That is the full extent of the panel's authority, and each of
  those actions is recorded in the audit log.
- Install-time settings (ports, subnet, the restart command) live in a file the panel can read but not write.

### Troubleshooting the sandbox

The sandboxing needs a container with `nesting=1`, which the installer sets. If a service refuses to start in
your environment, reinstall with `SANDBOX=0 bash install.sh` to remove the sandboxing drop-ins
(`/etc/systemd/system/ovpn-panel*.service.d/10-sandbox.conf`). The privilege separation (separate users,
the helper) stays.

## Network exposure

The container's firewall (`/usr/local/lib/ovpn-panel/firewall.sh`, loaded by `ovpn-panel-firewall.service`)
enforces three rules:

| Rule | Why |
|---|---|
| The panel port answers only to the local network (and the networks you add) | The dashboard is not reachable from the Internet, **nor from VPN clients** |
| VPN clients cannot reach each other through the kernel | Without this rule, the kernel routes client A's packets back out to client B whatever the "client-to-client" setting says; that setting only controls OpenVPN's own forwarding |
| VPN traffic is masqueraded | So clients can reach the network behind the container |

Change who can reach the panel with `ovpn-panel allow-panel 192.168.1.0/24,10.20.0.0/16`.

Do not forward the panel port on your router. If users must open activation links from outside, put the panel
behind a reverse proxy that exposes only `/p/*` and `/dl/*`: see
[Exposing the panel](INSTALL.md#exposing-the-panel-optional). The Proxmox firewall can be added on top
(Datacenter or container firewall, allow `1194/udp` in and `8443/tcp` only from your LAN); the installer does
not enable it because turning on the datacenter firewall can lock you out of the Proxmox host.

A VPN client **can** reach everything on your LAN, that is the point of a VPN. If some users should only
reach one service, give them a split-tunnel profile for that network and add firewall rules on the target.

## HTTPS and host names

- The panel only speaks HTTPS; there is no HTTP port to downgrade to. TLS 1.2 or later.
- The certificate is signed by a **private CA** created at install time, and covers the container's address,
  its host name, your public host name, and any name you add with `ovpn-panel add-host`. Install the CA
  (`/panel-ca.crt`, fingerprint printed by the installer and shown in the panel) once on your devices and the
  browser warning goes away for good. The CA key stays in a root-only directory.
- The panel certificate is renewed automatically by a daily job, together with the CRL.
- The panel **only answers to host names it knows** (the `Host` header allow-list). This blocks DNS rebinding
  and forged host headers.
- **Activation links are never built from the request.** They use the configured panel URL, or the install-time
  address, so a request with a forged `Host` or `X-Forwarded-Host` cannot make the panel hand out links that point
  to an attacker.
- Pages are served with a strict **Content-Security-Policy** that allows exactly the one inline script and
  style of each page, by hash. Injected script cannot run even if some output escaping were ever missed.
  Other headers: `nosniff`, `X-Frame-Options: DENY`, no referrer, `no-store`, same-origin isolation.
- `X-Forwarded-For` is only believed when the request comes from a proxy listed in `trusted_proxies`.

## Certificates and the PKI

- Client certificates are EC (P-256), valid for **2 years by default**, configurable from 1 day to 10 years in
  Settings. The panel shows each expiry and flags those under 30 days; renew from the Users page.
- Revocation is by CRL, replaced atomically; the CRL lasts a year and is refreshed daily by the maintenance
  job before it can expire (an expired CRL would lock every client out).
- **Renewing is atomic.** The helper issues the new certificate and revokes the old one as one step, under a
  lock. If anything fails in between, the new certificate is revoked again and the user is left exactly as
  before. Revoking is idempotent, so a failed delete can simply be retried.
- A failed settings change is rolled back: if OpenVPN does not start with the new `server.conf`, the previous
  file is restored and OpenVPN restarted.
- Profiles pin the server's name (`verify-x509-name`) and require TLS 1.2+. The data channel uses AES-GCM or
  ChaCha20-Poly1305, and the control channel is hidden behind `tls-crypt`.
- The OpenVPN CA and server certificates last 10 years. The Server page shows both expiry dates and warns well
  before they matter.

## Logins, sessions and the audit log

- Admin passwords are hashed with scrypt. Sessions are random 256-bit tokens, stored only as hashes, and expire
  after 8 hours. They are invalidated by logout, by a password change (all other sessions) and by
  `ovpn-panel reset-password`. At most 10 sessions per admin are kept.
- **Login rate limit, persisted in the database** so a restart does not reset it: an address is locked out for
  10 minutes after 5 failures, and a user name after 20 failures from anywhere. The administrator can clear a
  lockout with `ovpn-panel unlock`.
- An unknown user name costs the same hashing work as a wrong password, so response times do not reveal which
  names exist.
- The panel uses a bearer token in a header, not a cookie, so there is no CSRF.
- **Audit log** (Audit log page, `ovpn-panel audit`, and the system journal): logins and failures, password
  changes, user creation, deletion, renewal, enabling and disabling, disconnects, settings changes (with the
  new values), restarts, profile downloads, and every activation-link download. Entries are kept for a year.
- The panel's access log is turned off, so activation tokens in URLs never reach the journal.

## Activation links

A link gives access to a user's private key, so it is treated like a password:

- 256-bit random token, valid for **24 hours** and **5 downloads** by default (both configurable);
- creating a new link cancels the previous one; disabling, renewing or deleting the user cancels them too;
- looking at the page does not use a download; each profile download is audited;
- the page sends no referrer and loads nothing from other sites.

## Supply chain

- **Releases are versioned and pinned.** The one-line installer in the README fetches the script from a tag
  (e.g. `v1.0.0`), never from `main`.
- **Integrity is verified.** The script carries a SHA-256 of the application files of that release. After
  downloading, it hashes the files itself and refuses to continue on a mismatch. The hash covers the content
  of each file, not the archive, so it does not depend on how GitHub packs the download.
  Any `--ref` other than the release skips the check and says so.
- **Dependencies are locked with hashes** (`app/requirements.lock`), installed with `pip --require-hashes` into
  a fresh virtualenv, so a dependency cannot change between releases, and a tampered package is refused.
  The set is small: FastAPI, uvicorn, segno and cryptography with their dependencies.
- To avoid trusting a one-line `curl | bash`, download the script, read it, then run it.

## Backups and disaster recovery

```bash
ovpn-panel backup --out /root/ovpn-panel.bak          # asks for a passphrase (12+ characters)
ovpn-panel restore /root/ovpn-panel.bak
```

- The backup holds the PKI (including the CA key), the OpenVPN server files, the database, and the panel's
  configuration and TLS material. It is encrypted with AES-256-GCM using a key derived from the passphrase with
  scrypt; a wrong passphrase or any modification makes the whole file unreadable.
- The file is created with mode 0600 and never overwrites an existing file. **Store it, and the passphrase,
  away from the container.** Anyone with both can impersonate the server.
- A restore validates every entry first (no absolute paths, no `..`, no links, nothing outside the expected
  directories), replaces whole directories rather than mixing old and new files, and fixes permissions.
- The restore is exercised by the integration tests: they destroy the PKI and the database, restore, and check
  that a certificate issued before the backup still connects and one revoked before it is still refused.

## Known limitations

- **IPv6 is not routed through the tunnel.** With "All traffic", a client that has IPv6 connectivity will use
  it directly for IPv6 sites, outside the VPN. Disable IPv6 on clients that need full protection. The server
  pushes `block-outside-dns` for Windows DNS leaks, but cannot block IPv6 on other clients.
- **A connected VPN client can reach your LAN.** That is what the VPN is for; restrict it with a split-tunnel
  profile and firewall rules on the services.
- **One CA, one server certificate, 10 years.** Plan to rebuild the PKI before it expires; the panel warns.
- **The panel certificate is trusted only after you install the private CA.** Until then a browser warns.
  Check the fingerprint when you install it.
- **A backup passphrase that is lost cannot be recovered.**
- **Profiles created on the server contain the user's private key**, which therefore exists in the panel's view
  for the duration of the request. Activation links are the mitigation.

## How this is tested

- 240 unit and security tests (`bash tests/run-unit.sh`, or plain `pytest`): authentication, session expiry,
  rate limiting across restarts, the host allow-list, CSP hashes, **every route requiring a login unless it is on
  a fixed list of public ones**, rollback of renewals and deletions, the helper protocol and its validation,
  `server.conf` contents, hostile backup archives, certificate lifetime.
- An integration run on a real system (`bash tests/run.sh`): installs the previous release, upgrades it in place,
  checks which user runs what and which files each user can read, the sandbox from inside its mount namespace,
  the firewall with real OpenVPN clients (including two clients that must not reach each other), certificate
  lifetime, links, sessions, audit, backup, destruction and restore of the PKI, and a lockout that survives a
  restart.
