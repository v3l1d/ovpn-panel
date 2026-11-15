"""Small utilities shared by the privileged modules."""
import contextlib
import datetime
import logging
import os
import subprocess

log = logging.getLogger("ovpn-panel-helper")

RUN_DIR = "/run/ovpn-panel"
LOCK_PATH = RUN_DIR + "/pki.lock"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class OpError(Exception):
    """code: invalid | not_found | failed"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@contextlib.contextmanager
def pki_lock():
    """Serializes certificate and server.conf changes across the daemon and the maintenance job."""
    import fcntl  # not available on Windows; imported lazily so the other helpers stay importable

    os.makedirs(RUN_DIR, mode=0o750, exist_ok=True)
    with open(LOCK_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run(cmd, env=None, cwd=None, input=None, timeout=120):
    """Runs a command with a clean environment; raises CalledProcessError / TimeoutExpired."""
    e = {"PATH": SAFE_PATH, "HOME": "/tmp", "LC_ALL": "C"}
    if env:
        e.update(env)
    return subprocess.run(cmd, env=e, cwd=cwd, input=input, capture_output=True, text=True,
                          timeout=timeout, check=True)


def atomic_write(path: str, data, mode: int = 0o644):
    """Writes through a temporary file so that readers never see a truncated file."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.chmod(tmp, mode)  # the umask may have narrowed it
    os.replace(tmp, path)


def parse_openssl_date(text: str) -> datetime.datetime:
    """'notAfter=Nov  5 12:00:00 2028 GMT' (or just the date) -> aware UTC datetime."""
    value = text.strip().split("=", 1)[-1]
    return datetime.datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)


def cert_end(path: str, kind: str = "x509"):
    """notAfter of a certificate (or nextUpdate of a CRL) as an aware datetime."""
    flag = "-enddate" if kind == "x509" else "-nextupdate"
    return parse_openssl_date(run(["openssl", kind, "-in", path, "-noout", flag]).stdout)


def days_left(when: datetime.datetime) -> int:
    return int((when - datetime.datetime.now(datetime.timezone.utc)).total_seconds() // 86400)
