"""Encrypted backup and restore of everything that is hard to recreate.

File format: MAGIC | log2(scrypt N) | salt(16) | nonce(12) | AES-256-GCM(tar.gz), with the header
authenticated. A wrong passphrase or any modification makes decryption fail as a whole.
"""
import io
import os
import posixpath
import shutil
import sqlite3
import tarfile
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..config import CONFIG, CONFIG_PATH
from .common import atomic_write

MAGIC = b"OVPNPBK1"
N_LOG2 = 16
MIN_PASSPHRASE = 12
MAX_UNPACKED = 256 * 1024 * 1024


class BackupError(Exception):
    pass


def check_passphrase(p: str) -> str:
    if len(p) < MIN_PASSPHRASE:
        raise BackupError(f"the passphrase must be at least {MIN_PASSPHRASE} characters long")
    return p


def sources() -> list:
    """Absolute paths that make up a backup: the PKI, the OpenVPN server files, the panel's
    configuration and TLS material (including its CA key) and the database."""
    t = CONFIG.tls_dir
    return [os.path.join(CONFIG.easyrsa_dir, "pki"), CONFIG.server_dir, CONFIG_PATH,
            os.path.join(t, "ca"), os.path.join(t, "ca.crt"), os.path.join(t, "tls.crt"),
            os.path.join(t, "tls.key"), CONFIG.db_path]


def _arc(path: str) -> str:
    p = os.path.abspath(path).replace(os.sep, "/")
    return p.split(":", 1)[-1].lstrip("/")


def _key(passphrase: str, salt: bytes, n_log2: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**n_log2, r=8, p=1).derive(passphrase.encode())


def encrypt(data: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + bytes([N_LOG2]) + salt + nonce
    return header + AESGCM(_key(passphrase, salt, N_LOG2)).encrypt(nonce, data, header)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < 37 + 16 or blob[:8] != MAGIC:
        raise BackupError("this is not an ovpn-panel backup")
    n_log2, salt, nonce, header = blob[8], blob[9:25], blob[25:37], blob[:37]
    if not 14 <= n_log2 <= 20:
        raise BackupError("unsupported backup parameters")
    try:
        return AESGCM(_key(passphrase, salt, n_log2)).decrypt(nonce, blob[37:], header)
    except InvalidTag:
        raise BackupError("wrong passphrase, or the backup is corrupted")


def _only_files_and_dirs(info: tarfile.TarInfo):
    if not (info.isreg() or info.isdir()):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def make_archive(paths=None) -> bytes:
    paths = paths if paths is not None else sources()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            if not os.path.exists(path):
                continue
            src = path
            if os.path.abspath(path) == os.path.abspath(CONFIG.db_path):
                # a consistent snapshot, even while the panel is writing
                src = os.path.join(tmp, "panel.db")
                live = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                copy = sqlite3.connect(src)
                live.backup(copy)
                copy.close()
                live.close()
            tar.add(src, arcname=_arc(path), filter=_only_files_and_dirs)
    return buf.getvalue()


def _check_members(tar: tarfile.TarFile, paths) -> list:
    allowed = [_arc(p) for p in paths]
    members = tar.getmembers()
    if sum(m.size for m in members) > MAX_UNPACKED:
        raise BackupError("the backup is unreasonably large")
    for m in members:
        name = posixpath.normpath(m.name)
        if name.startswith("/") or name == ".." or name.startswith("../") or "\x00" in name:
            raise BackupError(f"unsafe path in the backup: {m.name!r}")
        if not (m.isreg() or m.isdir()):
            raise BackupError(f"unexpected entry in the backup: {m.name!r}")
        if not any(name == a or name.startswith(a + "/") for a in allowed):
            raise BackupError(f"path not part of an ovpn-panel backup: {m.name!r}")
    return members


def restore_archive(plain: bytes, paths=None):
    """Unpacks into a staging area first (everything is validated), then replaces the live files."""
    paths = paths if paths is not None else sources()
    with tempfile.TemporaryDirectory(prefix="ovpn-restore-") as stage:
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
            for m in _check_members(tar, paths):
                target = os.path.join(stage, posixpath.normpath(m.name))
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    os.chmod(target, m.mode & 0o777)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with tar.extractfile(m) as src:
                        atomic_write(target, src.read(), m.mode & 0o777)
        restored = []
        for path in paths:
            staged = os.path.join(stage, _arc(path))
            if not os.path.exists(staged):
                continue
            if os.path.isdir(staged):
                shutil.rmtree(path, ignore_errors=True)
                shutil.copytree(staged, path, copy_function=shutil.copy2)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(staged, "rb") as f:
                    atomic_write(path, f.read(), os.stat(staged).st_mode & 0o777)
            restored.append(path)
    return restored
