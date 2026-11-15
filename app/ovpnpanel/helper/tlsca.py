"""Private CA for the panel's HTTPS certificate.

A local CA signs the panel certificate, so that a browser which trusts the CA once never shows a
warning again, and a new certificate (new host name, renewal) needs no new click-through.
The CA key stays in a root-only directory; the panel process only ever sees the certificate.
"""
import grp
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile

from .. import validate
from ..config import CONFIG
from .common import OpError, cert_end, days_left, log, run

LEAF_DAYS = 825
CA_DAYS = 3650


def _dir() -> str:
    return CONFIG.tls_dir


def _ca_dir() -> str:
    return os.path.join(_dir(), "ca")


def ca_cert() -> str:
    return os.path.join(_dir(), "ca.crt")


def leaf_cert() -> str:
    return os.path.join(_dir(), "tls.crt")


def leaf_key() -> str:
    return os.path.join(_dir(), "tls.key")


def _panel_gid():
    try:
        return grp.getgrnam(CONFIG.panel_user).gr_gid
    except KeyError:
        return None


def share_with_panel(path: str, mode: int = 0o640):
    """root:<panel group>, readable by the panel only."""
    os.chmod(path, mode)
    gid = _panel_gid()
    if gid is not None:
        os.chown(path, 0, gid)


def ensure_ca() -> bool:
    """Creates the CA if missing. Returns True if it was created."""
    key = os.path.join(_ca_dir(), "ca.key")
    crt = os.path.join(_ca_dir(), "ca.crt")
    if os.path.exists(key) and os.path.exists(crt):
        if not os.path.exists(ca_cert()):
            shutil.copyfile(crt, ca_cert())
            os.chmod(ca_cert(), 0o644)
        return False
    os.makedirs(_ca_dir(), mode=0o700, exist_ok=True)
    os.chmod(_ca_dir(), 0o700)
    run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-days", str(CA_DAYS), "-sha256", "-subj", "/CN=ovpn-panel local CA",
         "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-keyout", key, "-out", crt])
    os.chmod(key, 0o600)
    shutil.copyfile(crt, ca_cert())
    os.chmod(ca_cert(), 0o644)
    return True


def wanted_hosts(extra=()) -> list:
    """Names and addresses the panel certificate must cover."""
    hosts = list(CONFIG.panel_hosts) + [CONFIG.default_panel_host, CONFIG.public_host, socket.gethostname(),
                                        "localhost", "127.0.0.1"] + list(extra)
    try:
        hosts += run(["hostname", "-I"]).stdout.split()
    except (subprocess.CalledProcessError, OSError):
        pass
    out = []
    for h in hosts:
        try:
            h = validate.host(h)
        except validate.ValidationError:
            continue
        if h not in out:
            out.append(h)
    return out


def _san(hosts) -> str:
    parts = []
    for h in hosts:
        try:
            ipaddress.ip_address(h)
            parts.append(f"IP:{h}")
        except ValueError:
            parts.append(f"DNS:{h}")
    return ",".join(parts)


def _covered(hosts) -> bool:
    try:
        text = run(["openssl", "x509", "-in", leaf_cert(), "-noout", "-ext", "subjectAltName"]).stdout
    except (subprocess.CalledProcessError, OSError):
        return False
    have = {m.lower() for m in re.findall(r"(?:DNS|IP Address):([^,\s]+)", text)}
    return all(h.lower() in have for h in hosts)


def leaf_problem(hosts, min_days: int):
    """Why the current panel certificate must be replaced, or None."""
    if not (os.path.exists(leaf_cert()) and os.path.exists(leaf_key())):
        return "missing"
    try:
        run(["openssl", "verify", "-CAfile", ca_cert(), leaf_cert()])
    except (subprocess.CalledProcessError, OSError):
        return "not signed by the panel CA"
    if days_left(cert_end(leaf_cert())) < min_days:
        return "expiring"
    if not _covered(hosts):
        return "does not cover every host name"
    return None


def issue_leaf(hosts):
    if not hosts:
        raise OpError("invalid", "no host names for the certificate")
    with tempfile.TemporaryDirectory(dir=_ca_dir()) as tmp:
        key, csr, crt, ext = (os.path.join(tmp, n) for n in ("k", "r", "c", "e"))
        run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-subj", f"/CN={hosts[0]}", "-keyout", key, "-out", csr])
        with open(ext, "w") as f:
            f.write("basicConstraints=critical,CA:FALSE\n"
                    "keyUsage=critical,digitalSignature\n"
                    "extendedKeyUsage=serverAuth\n"
                    "subjectKeyIdentifier=hash\n"
                    "authorityKeyIdentifier=keyid\n"
                    f"subjectAltName={_san(hosts)}\n")
        run(["openssl", "x509", "-req", "-in", csr, "-CA", os.path.join(_ca_dir(), "ca.crt"),
             "-CAkey", os.path.join(_ca_dir(), "ca.key"), "-CAcreateserial", "-days", str(LEAF_DAYS),
             "-sha256", "-extfile", ext, "-out", crt])
        shutil.copyfile(key, leaf_key() + ".new")
        shutil.copyfile(crt, leaf_cert() + ".new")
    share_with_panel(leaf_key() + ".new", 0o640)
    os.chmod(leaf_cert() + ".new", 0o644)
    os.replace(leaf_key() + ".new", leaf_key())
    os.replace(leaf_cert() + ".new", leaf_cert())


def ensure_leaf(hosts, force: bool = False, min_days: int = 60) -> bool:
    """Issues the panel certificate if it is missing, expiring or does not cover the hosts."""
    ensure_ca()
    problem = "forced" if force else leaf_problem(hosts, min_days)
    if problem is None:
        return False
    log.info("issuing a new panel certificate (%s) for %s", problem, ", ".join(hosts))
    issue_leaf(hosts)
    return True


def fingerprint() -> str:
    out = run(["openssl", "x509", "-in", ca_cert(), "-noout", "-fingerprint", "-sha256"]).stdout
    return out.strip().split("=", 1)[-1]
