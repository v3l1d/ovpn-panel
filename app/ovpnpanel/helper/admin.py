"""Root-only administration. Run through the `ovpn-panel` wrapper, e.g. `ovpn-panel backup --out F`."""
import argparse
import getpass
import grp
import ipaddress
import json
import os
import pwd
import re
import subprocess
import sys

from .. import db, settings, validate
from ..config import CONFIG, CONFIG_PATH
from ..config import load as load_config
from . import backup, ops, pki, serverconf, tlsca
from .common import atomic_write, cert_end, days_left, log, pki_lock, run

FIREWALL_ENV = "firewall.env"


def die(message: str):
    sys.exit(f"error: {message}")


def _systemctl(*args) -> bool:
    try:
        run(["systemctl", *args], timeout=90)
        return True
    except Exception as e:  # noqa: BLE001 - best effort: the service may not exist (yet)
        print(f"  (systemctl {' '.join(args)} failed: {getattr(e, 'stderr', e)})", file=sys.stderr)
        return False


def _load_config_file() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _save_config_file(cfg: dict):
    atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2) + "\n", 0o640)
    try:
        os.chown(CONFIG_PATH, 0, grp.getgrnam(CONFIG.panel_user).gr_gid)
    except KeyError:
        pass


# ---------------------------------------------------------------- configure

def cmd_configure(a):
    """Install-time settings. Values that already exist win, so upgrading never changes them."""
    cfg = _load_config_file()
    try:
        wanted = {
            "public_host": validate.host(a.public_host) if a.public_host else None,
            "port": validate.int_range(a.port, "port", 1, 65535) if a.port else None,
            "proto": a.proto if a.proto in ("udp", "tcp") else None,
            "subnet": str(ipaddress.IPv4Network(a.subnet)) if a.subnet else None,
            "panel_port": validate.int_range(a.panel_port, "panel port", 1, 65535) if a.panel_port else None,
            "default_panel_host": validate.host(a.panel_host) if a.panel_host else None,
        }
        hosts = list(cfg.get("panel_hosts", []))
        for h in [a.panel_host] + list(a.extra_host or []):
            if h:
                h = validate.host(h)
                if h not in hosts:
                    hosts.append(h)
        allow = validate.ipv4_networks(a.allow, "allowed networks") if a.allow else None
    except (validate.ValidationError, ValueError) as e:
        die(str(e))
    for key, value in wanted.items():
        if value is not None and key not in cfg:
            cfg[key] = value
    cfg["panel_hosts"] = hosts
    if allow and "panel_allowed_cidrs" not in cfg:
        cfg["panel_allowed_cidrs"] = allow
    _save_config_file(cfg)
    print(f"configuration written to {CONFIG_PATH}")


def cmd_write_server_conf(_a):
    """server.conf from the settings stored in the panel's database (defaults on a fresh install)."""
    s = settings.defaults()
    if os.path.exists(CONFIG.db_path):
        with db.connect() as conn:
            s = settings.load(conn)
    serverconf.write(settings.validate_server(settings.server_params(s)))
    print("server.conf written")


# ---------------------------------------------------------------- TLS

def cmd_init_tls(a):
    changed = tlsca.ensure_leaf(tlsca.wanted_hosts(), force=a.force)
    print("panel certificate " + ("issued" if changed else "is up to date"))
    print("panel CA SHA-256 fingerprint:", tlsca.fingerprint())
    if changed and a.restart:
        _systemctl("restart", "ovpn-panel")


def cmd_add_host(a):
    try:
        host = validate.host(a.name)
    except validate.ValidationError:
        die("not a valid host name or address")
    cfg = _load_config_file()
    hosts = list(cfg.get("panel_hosts", []))
    if host not in hosts:
        hosts.append(host)
        cfg["panel_hosts"] = hosts
        _save_config_file(cfg)
    CONFIG.panel_hosts = hosts
    tlsca.ensure_leaf(tlsca.wanted_hosts(), force=True)
    _systemctl("restart", "ovpn-panel")
    print(f"the panel now answers to {host} and its certificate covers it")


# ---------------------------------------------------------------- firewall

def _network_info():
    route = run(["ip", "-4", "route", "show", "default"]).stdout.split()
    if "dev" not in route:
        die("no default route: cannot work out the network interface")
    dev = route[route.index("dev") + 1]
    addrs = run(["ip", "-4", "-o", "addr", "show", "dev", dev, "scope", "global"]).stdout.split()
    if "inet" not in addrs:
        die(f"no IPv4 address on {dev}")
    return dev, ipaddress.ip_interface(addrs[addrs.index("inet") + 1]).network


def cmd_apply_firewall(a):
    """Renders firewall.env (the rules themselves live in firewall.sh) and reloads the rules."""
    dev, lan = _network_info()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", dev):
        die("unexpected interface name")
    allow = [str(n) for n in CONFIG.panel_allowed_cidrs] or [str(lan)]
    allow = validate.ipv4_networks(allow, "allowed networks")
    env = (f"SUBNET={ipaddress.IPv4Network(CONFIG.subnet)}\nWAN_IF={dev}\n"
           f"PANEL_PORT={validate.int_range(int(CONFIG.panel_port), 'panel port', 1, 65535)}\n"
           f'PANEL_ALLOW="{" ".join(allow)}"\n')
    atomic_write(os.path.join(CONFIG.tls_dir, FIREWALL_ENV), env, 0o644)
    print(f"panel port {CONFIG.panel_port} reachable from: {', '.join(allow)}")
    if not a.no_reload:
        _systemctl("restart", "ovpn-panel-firewall")


def cmd_allow_panel(a):
    try:
        cidrs = validate.ipv4_networks(a.cidr, "networks")
    except validate.ValidationError as e:
        die(str(e))
    cfg = _load_config_file()
    cfg["panel_allowed_cidrs"] = cidrs
    _save_config_file(cfg)
    CONFIG.panel_allowed_cidrs = cidrs
    cmd_apply_firewall(argparse.Namespace(no_reload=False))


# ---------------------------------------------------------------- maintenance

def cmd_maintenance(_a):
    """Daily job: keeps the CRL and the panel certificate from expiring, warns about the rest."""
    did = []
    with pki_lock():
        try:
            if days_left(pki.crl_end()) < 90:
                pki.regen_crl()
                did.append("revocation list refreshed")
        except Exception:  # noqa: BLE001 - a broken CRL must not stop the other checks
            log.exception("could not check the revocation list")
    try:
        if tlsca.ensure_leaf(tlsca.wanted_hosts(), min_days=60):
            did.append("panel certificate renewed")
            _systemctl("restart", "ovpn-panel")
    except Exception:  # noqa: BLE001
        log.exception("could not renew the panel certificate")
    for item in ops.pki_status()["items"]:
        if item["days_left"] is not None and item["days_left"] < 180 and item["key"] in ("ca", "server"):
            print(f"WARNING: {item['label']} expires in {item['days_left']} days", file=sys.stderr)
    print("; ".join(did) if did else "nothing to do")


def cmd_pki_status(_a):
    status = ops.pki_status()
    for i in status["items"]:
        left = "?" if i["days_left"] is None else f"{i['days_left']} days"
        print(f"{i['label']:<30} {i['not_after'] or '-':<28} {left}")
    print("Panel CA SHA-256:", status["panel_ca_fingerprint"])


# ---------------------------------------------------------------- backup / restore

def _passphrase(a, confirm: bool) -> str:
    if a.passphrase_file:
        with open(a.passphrase_file) as f:
            p = f.readline().rstrip("\n")
    elif os.environ.get("OVPN_PANEL_BACKUP_PASSPHRASE"):
        p = os.environ["OVPN_PANEL_BACKUP_PASSPHRASE"]
    else:
        p = getpass.getpass("Backup passphrase: ")
        if confirm and p != getpass.getpass("Repeat passphrase: "):
            die("the passphrases do not match")
    try:
        return backup.check_passphrase(p)
    except backup.BackupError as e:
        die(str(e))


def cmd_backup(a):
    passphrase = _passphrase(a, confirm=True)
    with pki_lock():
        blob = backup.encrypt(backup.make_archive(), passphrase)
    try:
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        die(f"{a.out} already exists")
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    print(f"backup written to {a.out} ({len(blob) // 1024} KB, encrypted)")
    print("Keep the passphrase somewhere else: without it the backup cannot be read.")


def _fix_permissions():
    try:
        uid = pwd.getpwnam(CONFIG.panel_user).pw_uid
        gid = grp.getgrnam(CONFIG.panel_user).gr_gid
    except KeyError:
        return
    data = os.path.dirname(CONFIG.db_path)
    os.chmod(data, 0o700)
    for root, dirs, files in os.walk(data):
        for n in dirs + files:
            os.chown(os.path.join(root, n), uid, gid)
    os.chown(data, uid, gid)
    for path in (tlsca.leaf_key(), CONFIG_PATH):
        if os.path.exists(path):
            tlsca.share_with_panel(path, 0o640)
    os.chmod(os.path.join(CONFIG.tls_dir, "ca"), 0o700)


def cmd_restore(a):
    if not a.yes and input("This replaces the PKI, the database and the configuration. Type 'restore' to continue: ") != "restore":
        die("aborted")
    with open(a.file, "rb") as f:
        blob = f.read()
    try:
        plain = backup.decrypt(blob, _passphrase(a, confirm=False))
        for unit in ("ovpn-panel", "openvpn-server@server"):
            _systemctl("stop", unit)
        with pki_lock():
            restored = backup.restore_archive(plain)
    except backup.BackupError as e:
        die(str(e))
    _fix_permissions()
    CONFIG.__dict__.update(load_config().__dict__)  # the restored config.json may differ
    cmd_apply_firewall(argparse.Namespace(no_reload=False))
    for unit in ("ovpn-panel-helper", "openvpn-server@server", "ovpn-panel"):
        _systemctl("restart", unit)
    print(f"restored {len(restored)} items; services restarted")


# ---------------------------------------------------------------- entry point

def main():
    p = argparse.ArgumentParser(prog="ovpn-panel")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("configure", help="write install-time settings (existing values are kept)")
    for name in ("public-host", "port", "proto", "subnet", "panel-port", "panel-host"):
        c.add_argument(f"--{name}")
    c.add_argument("--extra-host", action="append")
    c.add_argument("--allow")
    c.set_defaults(fn=cmd_configure)

    sub.add_parser("write-server-conf", help="regenerate server.conf from the saved settings").set_defaults(fn=cmd_write_server_conf)

    t = sub.add_parser("init-tls", help="create the panel CA and certificate if needed")
    t.add_argument("--force", action="store_true")
    t.add_argument("--restart", action="store_true")
    t.set_defaults(fn=cmd_init_tls)

    h = sub.add_parser("add-host", help="let the panel answer to another host name (and reissue its certificate)")
    h.add_argument("name")
    h.set_defaults(fn=cmd_add_host)

    f = sub.add_parser("apply-firewall", help="re-read the configuration and reload the firewall rules")
    f.add_argument("--no-reload", action="store_true")
    f.set_defaults(fn=cmd_apply_firewall)

    a = sub.add_parser("allow-panel", help="networks allowed to reach the panel port (default: the local network)")
    a.add_argument("cidr", help="comma-separated, e.g. 192.168.1.0/24,10.0.0.0/8")
    a.set_defaults(fn=cmd_allow_panel)

    sub.add_parser("maintenance", help="renew what is about to expire").set_defaults(fn=cmd_maintenance)
    sub.add_parser("pki-status", help="show certificate expiry dates").set_defaults(fn=cmd_pki_status)

    b = sub.add_parser("backup", help="encrypted backup of the PKI, database and configuration")
    b.add_argument("--out", required=True)
    b.add_argument("--passphrase-file")
    b.set_defaults(fn=cmd_backup)

    r = sub.add_parser("restore", help="restore a backup (stops the services while doing it)")
    r.add_argument("file")
    r.add_argument("--passphrase-file")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_restore)

    args = p.parse_args()
    if os.geteuid() != 0:
        die("this command must run as root")
    args.fn(args)


if __name__ == "__main__":
    main()
