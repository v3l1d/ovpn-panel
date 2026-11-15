import io
import os
import sqlite3
import tarfile

import pytest

from ovpnpanel import config
from ovpnpanel.helper import backup


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A miniature installation under tmp_path, with the configuration pointing at it."""
    base = tmp_path / "root"
    pki, server, tls = base / "etc/openvpn/easy-rsa/pki", base / "etc/openvpn/server", base / "etc/ovpn-panel"
    for d in (pki / "private", server / "ccd", tls / "ca", base / "var/lib/ovpn-panel"):
        d.mkdir(parents=True)
    files = {
        pki / "private/ca.key": "CA-PRIVATE-KEY", pki / "index.txt": "V\t300101000000Z\t\t01\tunknown\t/CN=server\n",
        pki / "ca.crt": "CA-CERT", server / "tc.key": "TLS-CRYPT", server / "server.key": "SERVER-KEY",
        server / "ccd/anna_abcdef": "disable\n", tls / "config.json": '{"port": 1194}', tls / "ca/ca.key": "PANEL-CA-KEY",
        tls / "tls.crt": "LEAF", tls / "tls.key": "LEAF-KEY", tls / "ca.crt": "PANEL-CA",
    }
    for path, text in files.items():
        path.write_text(text)
    for secret in (pki / "private/ca.key", server / "server.key", tls / "ca/ca.key", tls / "tls.key"):
        os.chmod(secret, 0o600)
    db_path = base / "var/lib/ovpn-panel/panel.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users(name TEXT)")
    conn.execute("INSERT INTO users VALUES ('anna')")
    conn.commit()
    conn.close()

    for key, value in {"easyrsa_dir": str(base / "etc/openvpn/easy-rsa"), "server_dir": str(server),
                       "tls_dir": str(tls), "db_path": str(db_path)}.items():
        monkeypatch.setattr(config.CONFIG, key, value)
    monkeypatch.setattr(backup, "CONFIG_PATH", str(tls / "config.json"))
    return base, files


def snapshot(files):
    return {str(p): p.read_text() for p in files}


def test_a_backup_restores_everything(tree):
    base, files = tree
    before = snapshot(files)
    blob = backup.encrypt(backup.make_archive(), "a long passphrase!")
    for path in files:
        path.unlink()
    (base / "var/lib/ovpn-panel/panel.db").unlink()

    restored = backup.restore_archive(backup.decrypt(blob, "a long passphrase!"))
    assert len(restored) >= 5
    assert snapshot(files) == before
    row = sqlite3.connect(base / "var/lib/ovpn-panel/panel.db").execute("SELECT name FROM users").fetchone()
    assert row == ("anna",)


@pytest.mark.skipif(os.name == "nt", reason="unix permissions")
def test_secret_files_keep_their_restrictive_permissions(tree):
    base, files = tree
    plain = backup.make_archive()
    for path in files:
        path.unlink()
    backup.restore_archive(plain)
    for secret in ("etc/openvpn/easy-rsa/pki/private/ca.key", "etc/openvpn/server/server.key", "etc/ovpn-panel/ca/ca.key"):
        assert os.stat(base / secret).st_mode & 0o777 == 0o600


def test_restoring_replaces_directories_instead_of_mixing_them(tree):
    base, files = tree
    plain = backup.make_archive()
    stale = base / "etc/openvpn/easy-rsa/pki/issued_after_the_backup.crt"
    stale.write_text("must not survive")
    backup.restore_archive(plain)
    assert not stale.exists()


def test_the_database_snapshot_is_consistent_while_it_is_open(tree):
    base, _ = tree
    live = sqlite3.connect(base / "var/lib/ovpn-panel/panel.db")
    live.execute("INSERT INTO users VALUES ('marco')")  # an open, uncommitted write
    plain = backup.make_archive()
    live.rollback()
    assert plain  # the snapshot did not fail because of the open transaction


def test_a_wrong_passphrase_fails(tree):
    blob = backup.encrypt(backup.make_archive(), "a long passphrase!")
    with pytest.raises(backup.BackupError, match="wrong passphrase"):
        backup.decrypt(blob, "another long passphrase")


def test_any_modification_is_detected(tree):
    blob = bytearray(backup.encrypt(backup.make_archive(), "a long passphrase!"))
    for position in (len(blob) // 2, len(blob) - 1, 20, 30):  # body, tag, salt, nonce
        tampered = bytearray(blob)
        tampered[position] ^= 0x01
        with pytest.raises(backup.BackupError):
            backup.decrypt(bytes(tampered), "a long passphrase!")
    with pytest.raises(backup.BackupError):
        backup.decrypt(bytes(blob[:-10]), "a long passphrase!")  # truncated


def test_other_files_are_not_mistaken_for_backups():
    for junk in (b"", b"hello", b"OVPNPBK1", b"x" * 200):
        with pytest.raises(backup.BackupError):
            backup.decrypt(junk, "a long passphrase!")


def test_encryption_is_salted(tree):
    plain = backup.make_archive()
    assert backup.encrypt(plain, "a long passphrase!") != backup.encrypt(plain, "a long passphrase!")


def test_the_passphrase_must_be_long_enough():
    with pytest.raises(backup.BackupError):
        backup.check_passphrase("short")
    assert backup.check_passphrase("long enough pass")


# ---------------------------------------------------------------- hostile archives

def hostile(name, kind="file", link=None, data=b"pwned"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, link
        elif kind == "dir":
            info.type = tarfile.DIRTYPE
        else:
            info.size = len(data)
        tar.addfile(info, io.BytesIO(data) if kind == "file" else None)
    return buf.getvalue()


def test_hostile_archives_are_refused_and_write_nothing(tree, tmp_path):
    base, files = tree
    before = snapshot(files)
    inside = backup._arc(str(base / "etc/openvpn/server")) + "/evil"
    attacks = [
        hostile("../../evil"), hostile("/etc/cron.d/evil"), hostile("etc/passwd"),
        hostile(inside + "/../../../../evil"), hostile(inside, kind="symlink", link="/etc/shadow"),
        hostile("x\x00y"),
    ]
    for plain in attacks:
        with pytest.raises(backup.BackupError):
            backup.restore_archive(plain)
    assert snapshot(files) == before
    assert not (tmp_path / "evil").exists() and not os.path.exists("/evil")


def test_decompression_bombs_are_refused(tree, monkeypatch):
    monkeypatch.setattr(backup, "MAX_UNPACKED", 10)
    base, _ = tree
    with pytest.raises(backup.BackupError, match="large"):
        backup.restore_archive(backup.make_archive())
