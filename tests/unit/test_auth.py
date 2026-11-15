import time

from fastapi.routing import APIRoute

from ovpnpanel import main, security

# The only routes that may answer without a login. Anything else showing up here is a bug.
PUBLIC = {"/", "/panel-ca.crt", "/p/{token}", "/dl/{token}", "/dl/{token}/info", "/api/login", "/api/health"}


def _needs_login(route) -> bool:
    def walk(dependant):
        return any(d.call is main.current_admin or walk(d) for d in dependant.dependencies)
    return walk(route.dependant)


def test_only_the_known_public_routes_skip_the_login():
    open_routes = {r.path for r in main.app.routes if isinstance(r, APIRoute) and not _needs_login(r)}
    assert open_routes == PUBLIC


def test_protected_routes_reject_missing_and_bogus_tokens(panel):
    for route in main.app.routes:
        if not isinstance(route, APIRoute) or route.path in PUBLIC:
            continue
        path = route.path.replace("{user_id}", "1")
        for method in route.methods:
            for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
                r = panel.client.request(method, path, json={} if method != "GET" else None, headers=headers)
                assert r.status_code == 401, f"{method} {path} answered {r.status_code}"


def test_login_works_and_wrong_password_is_rejected(panel):
    assert panel.login().status_code == 200
    assert panel.login(password="wrong").status_code == 401
    assert panel.login(username="nobody").status_code == 401


def test_unknown_user_still_costs_a_password_hash(panel, monkeypatch):
    """Otherwise response times would reveal which user names exist."""
    seen = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda p, h: seen.append(h) or real(p, h))
    panel.login(username="nobody")
    assert seen == [security.DUMMY_HASH]


def test_logout_invalidates_the_token(panel):
    headers = panel.auth()
    assert panel.client.get("/api/me", headers=headers).status_code == 200
    assert panel.client.post("/api/logout", headers=headers).status_code == 200
    assert panel.client.get("/api/me", headers=headers).status_code == 401


def test_expired_sessions_are_rejected(panel):
    headers = panel.auth()
    with panel.db.connect() as conn:
        conn.execute("UPDATE tokens SET expires_at = ?", (time.time() - 1,))
    assert panel.client.get("/api/me", headers=headers).status_code == 401


def test_session_lifetime_comes_from_the_configuration(panel, cfg):
    before = time.time()
    panel.auth()
    (expires,) = [r["expires_at"] for r in panel.rows("SELECT expires_at FROM tokens")]
    assert abs(expires - (before + cfg.session_ttl_hours * 3600)) < 60


def test_tokens_are_stored_hashed(panel):
    token = panel.auth()["Authorization"].split()[1]
    stored = [r["token_hash"] for r in panel.rows("SELECT token_hash FROM tokens")]
    assert stored == [security.token_hash(token)] and token not in stored


def test_an_admin_keeps_at_most_ten_sessions(panel):
    first = panel.auth()
    for _ in range(11):
        panel.auth()
    assert len(panel.rows("SELECT 1 FROM tokens")) == main.MAX_SESSIONS
    assert panel.client.get("/api/me", headers=first).status_code == 401  # the oldest one went


def test_changing_the_password_signs_out_the_other_sessions(panel):
    mine, other = panel.auth(), panel.auth()
    r = panel.client.post("/api/me/password", json={"current": "correct-horse-battery", "new": "another-long-password"},
                          headers=mine)
    assert r.status_code == 200
    assert panel.client.get("/api/me", headers=mine).status_code == 200
    assert panel.client.get("/api/me", headers=other).status_code == 401
    assert panel.login(password="another-long-password").status_code == 200
    assert panel.login().status_code == 401


def test_changing_the_password_needs_the_current_one(panel):
    r = panel.client.post("/api/me/password", json={"current": "wrong", "new": "another-long-password"},
                          headers=panel.auth())
    assert r.status_code == 400
    assert panel.login().status_code == 200  # unchanged


def test_short_new_passwords_are_refused(panel):
    r = panel.client.post("/api/me/password", json={"current": "correct-horse-battery", "new": "short"},
                          headers=panel.auth())
    assert r.status_code == 422


def test_login_attempts_are_audited(panel):
    panel.login(password="wrong")
    panel.login()
    events = panel.rows("SELECT actor, action, ok FROM audit ORDER BY id")
    assert events == [{"actor": "admin", "action": "login", "ok": 0}, {"actor": "admin", "action": "login", "ok": 1}]


# ---------------------------------------------------------------- rate limiting

def test_five_failures_lock_an_address_out_even_for_the_right_password(panel):
    for _ in range(5):
        assert panel.login(password="wrong").status_code == 401
    assert panel.login(password="wrong").status_code == 429
    assert panel.login().status_code == 429


def test_the_lockout_survives_a_restart(panel):
    for _ in range(5):
        panel.login(password="wrong")
    from fastapi.testclient import TestClient

    restarted = TestClient(main.app)  # a new client = a new process as far as the application can tell
    r = restarted.post("/api/login", json={"username": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 429


def test_a_successful_login_resets_the_counter(panel):
    for _ in range(4):
        panel.login(password="wrong")
    assert panel.login().status_code == 200
    for _ in range(4):
        assert panel.login(password="wrong").status_code == 401


def test_one_user_name_is_throttled_across_addresses(panel, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "trusted_proxies", ["testclient"])
    for i in range(20):
        panel.login(password="wrong", headers={"X-Forwarded-For": f"198.51.100.{i}"})
    r = panel.login(headers={"X-Forwarded-For": "203.0.113.99"})  # a fresh address, right password
    assert r.status_code == 429


def test_forwarded_for_is_ignored_unless_the_proxy_is_trusted(panel):
    """Otherwise a client could dodge the lockout by inventing addresses."""
    for i in range(5):
        panel.login(password="wrong", headers={"X-Forwarded-For": f"198.51.100.{i}"})
    assert panel.login(headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429


def test_a_trusted_proxy_reveals_the_real_client_address(panel, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "trusted_proxies", ["testclient"])
    for _ in range(5):
        panel.login(password="wrong", headers={"X-Forwarded-For": "198.51.100.7"})
    assert panel.login(headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 429
    assert panel.login(headers={"X-Forwarded-For": "198.51.100.8"}).status_code == 200  # someone else behind it
