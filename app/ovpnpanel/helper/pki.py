"""easy-rsa operations. Callers hold the pki lock for anything that changes state."""
import os
import re
import subprocess

from .. import validate
from ..config import CONFIG
from .common import OpError, atomic_write, cert_end, log, run

_SUBJECT_CN = re.compile(r"/CN=([^/]+)")


def pki_dir() -> str:
    return os.path.join(CONFIG.easyrsa_dir, "pki")


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def easyrsa(*args, days: int = 825):
    env = {"EASYRSA_PKI": pki_dir(), "EASYRSA_BATCH": "1", "EASYRSA_CRL_DAYS": str(int(CONFIG.crl_days)),
           "EASYRSA_CERT_EXPIRE": str(int(days)), "EASYRSA_ALGO": "ec", "EASYRSA_CURVE": "prime256v1"}
    try:
        return run(["./easyrsa", *args], cwd=CONFIG.easyrsa_dir, env=env)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("easyrsa %s failed: %s", " ".join(args), getattr(e, "stderr", e))
        raise OpError("failed", "certificate operation failed")


def index() -> dict:
    """CN -> status ('V' valid, 'R' revoked, 'E' expired), from easy-rsa's database."""
    out = {}
    try:
        with open(os.path.join(pki_dir(), "index.txt")) as f:
            for line in f:
                fields = line.rstrip("\n").split("\t")
                if len(fields) >= 6:
                    m = _SUBJECT_CN.search(fields[5])
                    if m:
                        out[m.group(1)] = fields[0]
    except FileNotFoundError:
        pass
    return out


def _paths(cn: str):
    p = pki_dir()
    return (f"{p}/issued/{cn}.crt", f"{p}/private/{cn}.key", f"{p}/reqs/{cn}.req")


def _cleanup(cn: str):
    """Removes the leftovers of a failed issue. Only for names that never got a valid certificate."""
    if index().get(cn) == "V":
        return
    for path in _paths(cn):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def issue(cn: str, days: int) -> str:
    validate.cn(cn)
    validate.int_range(days, "days", 1, 3650)
    if cn in index():
        raise OpError("invalid", "certificate name already used")
    try:
        easyrsa("build-client-full", cn, "nopass", days=days)
    except OpError:
        _cleanup(cn)
        raise
    return cert_end(_paths(cn)[0]).isoformat()


def info(cn: str) -> dict:
    status = index().get(cn)
    if status is None:
        raise OpError("not_found", "certificate not found")
    not_after = None
    if status == "V":
        try:
            not_after = cert_end(_paths(cn)[0]).isoformat()
        except (subprocess.CalledProcessError, OSError, ValueError):
            pass
    return {"status": status, "not_after": not_after}


def material(cn: str) -> dict:
    """Everything a client profile needs. Only for certificates that are valid (not revoked)."""
    if index().get(cn) != "V":
        raise OpError("not_found", "certificate not found or revoked")
    crt, key, _ = _paths(cn)
    return {
        "ca": _read(os.path.join(pki_dir(), "ca.crt")).strip(),
        "cert": _read(crt).strip(),
        "key": _read(key).strip(),
        "tls_crypt": _read(os.path.join(CONFIG.server_dir, "tc.key")).strip(),
    }


def set_disabled(cn: str, disabled: bool):
    """Blocks/unblocks a client through client-config-dir (applies from its next connection)."""
    ccd = os.path.join(CONFIG.server_dir, "ccd")
    os.makedirs(ccd, mode=0o755, exist_ok=True)
    path = os.path.join(ccd, cn)
    if disabled:
        atomic_write(path, "disable\n", 0o644)
    elif os.path.exists(path):
        os.remove(path)


def regen_crl():
    easyrsa("gen-crl")
    # openvpn rereads the CRL on every connection and runs as "nobody": it must be world-readable,
    # and replaced atomically, otherwise a client connecting meanwhile would see a truncated file
    atomic_write(os.path.join(CONFIG.server_dir, "crl.pem"),
                 _read(os.path.join(pki_dir(), "crl.pem")), 0o644)


def crl_end():
    return cert_end(os.path.join(CONFIG.server_dir, "crl.pem"), kind="crl")


def revoke(cn: str):
    """Idempotent: revoking a revoked or unknown certificate only refreshes the CRL."""
    status = index().get(cn)
    if status == "V":
        easyrsa("revoke", cn)
    elif status is None:
        log.warning("revoke: unknown certificate %s", cn)
    regen_crl()
    set_disabled(cn, False)
