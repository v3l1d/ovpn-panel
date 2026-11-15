import os
import shutil
import subprocess

import pytest

pytest.importorskip("grp")
if shutil.which("openssl") is None:
    pytest.skip("openssl is not installed", allow_module_level=True)

from ovpnpanel import config  # noqa: E402
from ovpnpanel.helper import tlsca  # noqa: E402

HOSTS = ["192.0.2.10", "panel.example.net", "localhost", "127.0.0.1"]


@pytest.fixture
def tls(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CONFIG, "tls_dir", str(tmp_path))
    monkeypatch.setattr(config.CONFIG, "panel_user", "no-such-user-xyz")  # no group to chown to
    return tmp_path


def openssl(*args):
    return subprocess.run(["openssl", *args], capture_output=True, text=True)


def test_the_ca_is_created_once_and_its_key_is_private(tls):
    assert tlsca.ensure_ca() is True and tlsca.ensure_ca() is False
    assert os.stat(tls / "ca").st_mode & 0o777 == 0o700
    assert os.stat(tls / "ca/ca.key").st_mode & 0o777 == 0o600
    assert os.stat(tls / "ca.crt").st_mode & 0o777 == 0o644
    assert b"PRIVATE KEY" not in (tls / "ca.crt").read_bytes()  # the published copy is the certificate only


def test_the_panel_certificate_chains_to_the_ca_and_covers_every_host(tls):
    assert tlsca.ensure_leaf(HOSTS) is True
    assert openssl("verify", "-CAfile", str(tls / "ca.crt"), str(tls / "tls.crt")).returncode == 0
    text = openssl("x509", "-in", str(tls / "tls.crt"), "-noout", "-text").stdout
    for host in HOSTS:
        assert host in text
    assert "CA:FALSE" in text and "TLS Web Server Authentication" in text
    assert os.stat(tls / "tls.key").st_mode & 0o777 == 0o640


def test_a_valid_certificate_is_left_alone_and_a_new_host_triggers_reissue(tls):
    tlsca.ensure_leaf(HOSTS)
    before = (tls / "tls.crt").read_bytes()
    assert tlsca.ensure_leaf(HOSTS) is False and (tls / "tls.crt").read_bytes() == before
    assert tlsca.ensure_leaf(HOSTS + ["new.example.net"]) is True
    assert "new.example.net" in openssl("x509", "-in", str(tls / "tls.crt"), "-noout", "-text").stdout


def test_an_expiring_certificate_is_renewed(tls):
    tlsca.ensure_leaf(HOSTS)
    assert tlsca.ensure_leaf(HOSTS, min_days=100000) is True


def test_a_certificate_not_signed_by_our_ca_is_replaced(tls):
    """The previous version used a self-signed certificate: an upgrade must move to the CA."""
    tlsca.ensure_ca()
    openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "30",
            "-subj", "/CN=old", "-keyout", str(tls / "tls.key"), "-out", str(tls / "tls.crt"))
    assert tlsca.leaf_problem(HOSTS, 60) == "not signed by the panel CA"
    assert tlsca.ensure_leaf(HOSTS) is True
    assert openssl("verify", "-CAfile", str(tls / "ca.crt"), str(tls / "tls.crt")).returncode == 0


def test_the_certificate_lifetime_is_bounded(tls):
    tlsca.ensure_leaf(HOSTS)
    from ovpnpanel.helper.common import cert_end, days_left

    assert 800 <= days_left(cert_end(str(tls / "tls.crt"))) <= tlsca.LEAF_DAYS


def test_wanted_hosts_are_normalized_and_deduplicated(tls, monkeypatch):
    monkeypatch.setattr(config.CONFIG, "panel_hosts", ["Panel.Example.NET", "bad host", "panel.example.net", "192.0.2.10"])
    monkeypatch.setattr(config.CONFIG, "default_panel_host", "192.0.2.10")
    hosts = tlsca.wanted_hosts()
    assert hosts.count("panel.example.net") == 1 and hosts.count("192.0.2.10") == 1
    assert "bad host" not in hosts and "localhost" in hosts and "127.0.0.1" in hosts


def test_a_missing_host_list_is_refused(tls):
    from ovpnpanel.helper.common import OpError

    tlsca.ensure_ca()
    with pytest.raises(OpError):
        tlsca.issue_leaf([])
