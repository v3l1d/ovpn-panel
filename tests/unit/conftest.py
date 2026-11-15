import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "app")))

from ovpnpanel import config  # noqa: E402
from ovpnpanel import helper_client  # noqa: E402

PASSWORD = "correct-horse-battery"


class FakeHelper:
    """Stands in for the privileged helper: records calls and can be told to fail."""

    def __init__(self):
        self.calls = []
        self.errors = {}   # op -> HelperError raised on every call
        self.certs = {}    # cn -> validity days

    def __call__(self, op, timeout=0, **args):
        self.calls.append((op, args))
        if op in self.errors:
            raise self.errors[op]
        return getattr(self, f"_{op}")(**args)

    def called(self, op):
        return [a for o, a in self.calls if o == op]

    def fail(self, op, code="failed", message="boom: secret internal detail"):
        self.errors[op] = helper_client.HelperError(code, message)

    def _issue_cert(self, cn, days):
        self.certs[cn] = days
        return {"not_after": "2030-01-01T00:00:00+00:00"}

    def _cert_info(self, cn):
        return {"status": "V", "not_after": "2030-01-01T00:00:00+00:00"}

    def _profile_material(self, cn):
        if cn not in self.certs:
            raise helper_client.HelperError("not_found", "certificate not found or revoked")
        return {"ca": "CA-PEM", "cert": "CERT-PEM", "key": "KEY-PEM", "tls_crypt": "TLSCRYPT-KEY"}

    def _revoke_cert(self, cn):
        self.certs.pop(cn, None)
        return {}

    def _renew_cert(self, old_cn, new_cn, days, keep_disabled=False):
        self.certs.pop(old_cn, None)
        self.certs[new_cn] = days
        return {"not_after": "2031-01-01T00:00:00+00:00"}

    def _set_disabled(self, cn, disabled):
        return {}

    def _apply_server_conf(self, **args):
        return {}

    def _restart(self):
        return {}

    def _kill(self, cn):
        return {}

    def _clients(self):
        return {}

    def _overview(self):
        return {"state": {"up_since": 1, "state": "CONNECTED"},
                "stats": {"nclients": 0, "bytesin": 0, "bytesout": 0}, "version": "2.6.3"}

    def _journal(self, lines=150):
        return [{"time": 1, "flags": "I", "msg": "hello"}]

    def _pki_status(self):
        return {"items": [], "panel_ca_fingerprint": "AA:BB"}


class Panel:
    def __init__(self, client, helper, main, db):
        self.client, self.helper, self.main, self.db = client, helper, main, db

    def login(self, password=PASSWORD, username="admin", headers=None):
        return self.client.post("/api/login", json={"username": username, "password": password}, headers=headers)

    def auth(self, headers=None):
        r = self.login(headers=headers)
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}

    def create_user(self, name="anna", note=""):
        r = self.client.post("/api/users", json={"name": name, "note": note}, headers=self.auth())
        assert r.status_code == 200, r.text
        return r.json()

    def rows(self, sql, *args):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    values = {
        "db_path": str(tmp_path / "panel.db"),
        "tls_dir": str(tmp_path / "tls"),
        "panel_hosts": ["testserver", "192.0.2.10"],
        "default_panel_host": "192.0.2.10",
        "panel_port": 8443,
        "public_host": "vpn.example.net",
        "trusted_proxies": [],
        "session_ttl_hours": 8,
    }
    for key, value in values.items():
        monkeypatch.setattr(config.CONFIG, key, value, raising=False)
    return config.CONFIG


@pytest.fixture
def panel(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from ovpnpanel import db, main, security

    db.init()
    with db.connect() as conn:
        conn.execute("INSERT INTO admins(username, pw_hash, created_at) VALUES ('admin', ?, ?)",
                     (security.hash_password(PASSWORD), db.now_iso()))
    helper = FakeHelper()
    monkeypatch.setattr(helper_client, "call", helper)
    main.forget_hosts()
    return Panel(TestClient(main.app), helper, main, db)
