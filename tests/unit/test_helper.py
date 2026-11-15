import contextlib
import json
import os
import socket
import threading

import pytest

pytest.importorskip("grp")  # the helper is unix-only

import pwd  # noqa: E402

from ovpnpanel import config, helper_client  # noqa: E402
from ovpnpanel.helper import ops, server, serverconf  # noqa: E402
from ovpnpanel.helper.common import OpError  # noqa: E402


@contextlib.contextmanager
def no_lock():
    yield


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A real helper daemon on a temporary socket, serving test operations only."""
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setattr(config.CONFIG, "panel_user", me)
    path = str(tmp_path / "run" / "helper.sock")
    monkeypatch.setattr(config.CONFIG, "helper_socket", path)

    def crash():
        return 1 / 0

    def deny():
        raise OpError("not_found", "nope")

    monkeypatch.setattr(ops, "OPS", {"echo": lambda value: {"value": value}, "crash": crash, "deny": deny})
    srv = server.make_server(path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield path
    srv.shutdown()
    srv.server_close()


def raw(path, payload: bytes) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(path)
    s.sendall(payload)
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf)


# ---------------------------------------------------------------- the daemon

def test_a_call_round_trips(helper):
    assert helper_client.call("echo", value="hi") == {"value": "hi"}


def test_the_socket_is_not_world_accessible(helper):
    mode = os.stat(helper).st_mode & 0o777
    assert mode == 0o660
    assert os.stat(os.path.dirname(helper)).st_mode & 0o777 == 0o750


def test_errors_keep_their_code(helper):
    with pytest.raises(helper_client.HelperError) as e:
        helper_client.call("deny")
    assert e.value.code == "not_found" and e.value.message == "nope"


def test_crashes_do_not_leak_details(helper):
    with pytest.raises(helper_client.HelperError) as e:
        helper_client.call("crash")
    assert e.value.code == "failed" and "division" not in e.value.message.lower()


def test_unknown_operations_do_not_exist(helper):
    with pytest.raises(helper_client.HelperError) as e:
        helper_client.call("rm_rf")
    assert e.value.code == "invalid"
    for name in ("__class__", "OPS", "run", "os.system"):
        assert raw(helper, json.dumps({"op": name}).encode() + b"\n")["error"] == "invalid"


def test_unexpected_arguments_are_refused(helper):
    assert raw(helper, b'{"op": "echo", "args": {"value": 1, "extra": 2}}\n')["error"] == "invalid"
    assert raw(helper, b'{"op": "echo", "args": {}}\n')["error"] == "invalid"


@pytest.mark.parametrize("payload", [b"not json\n", b"[]\n", b'{"op": 5}\n', b'{"op": "echo", "args": []}\n', b"\n"])
def test_malformed_requests_are_refused(helper, payload):
    assert raw(helper, payload)["ok"] is False


def test_oversized_requests_are_refused(helper):
    assert raw(helper, b'{"op": "echo", "args": {"value": "' + b"a" * 70000 + b'"}}\n')["error"] == "invalid"


def test_an_absent_helper_is_reported_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(config.CONFIG, "helper_socket", str(tmp_path / "nothing.sock"))
    with pytest.raises(helper_client.HelperError) as e:
        helper_client.call("echo", value=1)
    assert e.value.code == "unavailable"


@pytest.mark.skipif(os.geteuid() == 0, reason="root is always allowed in")
def test_other_users_are_dropped_without_an_answer(helper, monkeypatch):
    monkeypatch.setattr(server, "_panel_uid", lambda: None)  # the panel user is now someone else
    with pytest.raises(helper_client.HelperError):
        helper_client.call("echo", value=1)


# ---------------------------------------------------------------- operations: validation

@pytest.mark.parametrize("call", [
    lambda: ops.OPS["issue_cert"](cn="../x", days=30),
    lambda: ops.OPS["issue_cert"](cn="anna_abcdef", days=0),
    lambda: ops.OPS["issue_cert"](cn="anna_abcdef", days=99999),
    lambda: ops.OPS["issue_cert"](cn="anna_abcdef", days=True),
    lambda: ops.OPS["profile_material"](cn="ca"),
    lambda: ops.OPS["revoke_cert"](cn="x\ny_abcdef"),
    lambda: ops.OPS["kill"](cn="anna_abcdef\nstatus"),
    lambda: ops.OPS["set_disabled"](cn="anna_abcdef", disabled="yes"),
    lambda: ops.OPS["renew_cert"](old_cn="anna_abcdef", new_cn="bad", days=30),
    lambda: ops.OPS["renew_cert"](old_cn="anna_abcdef", new_cn="anna_123456", days=30, keep_disabled="x"),
    lambda: ops.OPS["journal"](lines="all"),
    lambda: ops.OPS["apply_server_conf"](dns=["1.1.1.1\npush x"], routes=[], full_tunnel=True, client_to_client=False),
    lambda: ops.OPS["apply_server_conf"](dns=[], routes=["x"], full_tunnel=False, client_to_client=False),
    lambda: ops.OPS["apply_server_conf"](dns=[], routes=[], full_tunnel="true", client_to_client=False),
])
def test_arguments_are_validated_before_anything_runs(call):
    with pytest.raises(OpError) as e:
        call()
    assert e.value.code == "invalid"


def test_the_privileged_interface_is_exactly_this():
    """Adding an operation must be a conscious decision: this list is the attack surface."""
    assert set(ops.OPS) == {
        "issue_cert", "cert_info", "profile_material", "revoke_cert", "renew_cert", "set_disabled",
        "apply_server_conf", "restart", "kill", "clients", "overview", "journal", "pki_status"}


# ---------------------------------------------------------------- operations: rollback

@pytest.fixture
def pki_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "pki_lock", no_lock)
    monkeypatch.setattr(ops.pki, "issue", lambda cn, days: calls.append(("issue", cn)) or "2030-01-01T00:00:00+00:00")
    monkeypatch.setattr(ops.pki, "revoke", lambda cn: calls.append(("revoke", cn)))
    monkeypatch.setattr(ops.pki, "set_disabled", lambda cn, d: calls.append(("disabled", cn, d)))
    monkeypatch.setattr(ops, "_kill", lambda cn: calls.append(("kill", cn)))
    return calls


def test_renewal_issues_revokes_then_disconnects(pki_calls):
    out = ops.OPS["renew_cert"](old_cn="anna_aaaaaa", new_cn="anna_bbbbbb", days=30)
    assert out == {"not_after": "2030-01-01T00:00:00+00:00"}
    assert pki_calls == [("issue", "anna_bbbbbb"), ("revoke", "anna_aaaaaa"), ("kill", "anna_aaaaaa")]


def test_a_failed_revocation_rolls_the_new_certificate_back(pki_calls, monkeypatch):
    def revoke(cn):
        pki_calls.append(("revoke", cn))
        if cn == "anna_aaaaaa":
            raise OpError("failed", "easyrsa exploded")

    monkeypatch.setattr(ops.pki, "revoke", revoke)
    with pytest.raises(OpError) as e:
        ops.OPS["renew_cert"](old_cn="anna_aaaaaa", new_cn="anna_bbbbbb", days=30)
    assert "rolled back" in e.value.message
    assert pki_calls == [("issue", "anna_bbbbbb"), ("revoke", "anna_aaaaaa"), ("revoke", "anna_bbbbbb")]


def test_if_issuing_fails_nothing_is_revoked(pki_calls, monkeypatch):
    def issue(cn, days):
        raise OpError("failed", "no")

    monkeypatch.setattr(ops.pki, "issue", issue)
    with pytest.raises(OpError):
        ops.OPS["renew_cert"](old_cn="anna_aaaaaa", new_cn="anna_bbbbbb", days=30)
    assert pki_calls == []


def test_renewing_a_disabled_user_keeps_the_new_certificate_blocked(pki_calls):
    ops.OPS["renew_cert"](old_cn="anna_aaaaaa", new_cn="anna_bbbbbb", days=30, keep_disabled=True)
    assert ("disabled", "anna_bbbbbb", True) in pki_calls


def test_a_server_config_that_does_not_start_is_rolled_back(monkeypatch):
    written, restarts = [], []
    monkeypatch.setattr(ops, "pki_lock", no_lock)
    monkeypatch.setattr(ops.serverconf, "read", lambda: "OLD\n")
    monkeypatch.setattr(ops.serverconf, "write_text", written.append)

    def restart():
        restarts.append(1)
        if len(restarts) == 1:
            raise OpError("failed", "did not start")

    monkeypatch.setattr(ops, "_restart_and_wait", restart)
    with pytest.raises(OpError) as e:
        ops.OPS["apply_server_conf"](dns=["1.1.1.1"], routes=[], full_tunnel=True, client_to_client=False)
    assert "previous ones were restored" in e.value.message
    assert written[-1] == "OLD\n" and len(restarts) == 2


# ---------------------------------------------------------------- server.conf

PARAMS = [
    {"dns": ["1.1.1.1", "9.9.9.9"], "routes": [], "full_tunnel": True, "client_to_client": False},
    {"dns": [], "routes": ["192.168.1.0/24", "10.0.0.0/8"], "full_tunnel": False, "client_to_client": True},
]
KNOWN = ("# ", "port ", "proto ", "dev ", "ca ", "cert ", "key ", "dh ", "tls-crypt ", "tls-version-min ",
         "crl-verify ", "client-config-dir ", "topology ", "server ", "keepalive ", "data-ciphers ", "user ",
         "group ", "persist-key", "persist-tun", "management ", "verb ", "explicit-exit-notify ", "push ",
         "client-to-client")


@pytest.mark.parametrize("params", PARAMS)
def test_every_line_of_server_conf_is_a_known_directive(params):
    for line in serverconf.render(params).splitlines():
        assert line.startswith(KNOWN), line


@pytest.mark.parametrize("params", PARAMS)
def test_only_expected_options_are_pushed(params):
    import re

    for line in serverconf.render(params).splitlines():
        if line.startswith("push "):
            assert (line in ('push "redirect-gateway def1 bypass-dhcp"', 'push "block-outside-dns"')
                    or re.fullmatch(r'push "route \d+\.\d+\.\d+\.\d+ \d+\.\d+\.\d+\.\d+"', line)
                    or re.fullmatch(r'push "dhcp-option DNS \d+\.\d+\.\d+\.\d+"', line)), line


def test_server_conf_never_contains_code_execution_hooks():
    text = serverconf.render(PARAMS[0]).lower()
    for dangerous in ("script-security", "\nup ", "\ndown ", "plugin", "auth-user-pass-verify", "client-connect",
                      "tls-verify", "learn-address", "ipchange", "route-up"):
        assert dangerous not in text


def test_full_tunnel_and_split_tunnel_differ_as_expected():
    full, split = serverconf.render(PARAMS[0]), serverconf.render(PARAMS[1])
    assert "redirect-gateway" in full and "route 192.168.1.0 255.255.255.0" not in full
    assert "redirect-gateway" not in split and 'push "route 192.168.1.0 255.255.255.0"' in split
    assert "client-to-client" in split and "client-to-client" not in full


def test_server_conf_comes_from_the_root_owned_config(monkeypatch):
    monkeypatch.setattr(config.CONFIG, "proto", "udp\nscript-security 2")
    with pytest.raises(ValueError):
        serverconf.render(PARAMS[0])
