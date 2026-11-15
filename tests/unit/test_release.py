import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release  # noqa: E402


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="needs a unix shell")
def test_the_release_hash_matches_what_the_installer_computes():
    """proxmox/create-ct.sh hashes the downloaded files in the shell; the two must agree byte for byte."""
    shell = subprocess.run(
        ["bash", "-c", "cd \"$1\" && find app -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 "
                       "| LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1", "_", str(ROOT)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert release.tree_hash() == shell


def test_the_hash_changes_when_a_file_changes(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "a.py").write_text("one\n")
    monkeypatch.setattr(release, "APP", app)
    monkeypatch.setattr(release, "ROOT", tmp_path)
    first = release.tree_hash()
    (app / "a.py").write_text("two\n")
    assert release.tree_hash() != first
    (app / "a.py").write_text("one\n")
    (app / "b.py").write_text("")
    assert release.tree_hash() != first  # an added file counts too


def test_crlf_files_are_refused_because_they_would_hash_differently_on_linux(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "a.py").write_bytes(b"one\r\n")
    monkeypatch.setattr(release, "APP", app)
    monkeypatch.setattr(release, "ROOT", tmp_path)
    with pytest.raises(SystemExit):
        release.tree_hash()


def test_the_installer_still_has_the_lines_the_release_tool_rewrites():
    text = (ROOT / "proxmox" / "create-ct.sh").read_text(encoding="utf-8")
    release.sub_once(text, r'^PINNED_REF=".*"$', 'PINNED_REF="v9.9.9"', "PINNED_REF")
    release.sub_once(text, r'^PINNED_TREE_SHA256=".*"$', 'PINNED_TREE_SHA256="x"', "PINNED_TREE_SHA256")
    init = (ROOT / "app" / "ovpnpanel" / "__init__.py").read_text(encoding="utf-8")
    release.sub_once(init, r'^__version__ = ".*"$', '__version__ = "9.9.9"', "__version__")


def test_the_firewall_script_keeps_its_three_guarantees():
    text = (ROOT / "app" / "firewall.sh").read_text(encoding="utf-8")
    assert "-i tun+ -o tun+ -j DROP" in text                       # clients cannot reach each other
    assert "MASQUERADE" in text                                    # NAT
    # the panel port: allowed networks first, then a catch-all DROP
    assert text.index('-s "$cidr" -j RETURN') < text.index('--dport "$PANEL_PORT" -j DROP')
