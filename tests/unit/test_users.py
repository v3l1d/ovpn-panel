import time

import pytest

SETTINGS = {"endpoint_host": "vpn.example.net", "public_port": 1194, "panel_url": "", "dns": "1.1.1.1, 9.9.9.9",
            "full_tunnel": True, "routes": "", "client_to_client": False,
            "cert_days": 730, "share_hours": 24, "share_downloads": 5}


def put_settings(panel, **changes):
    return panel.client.put("/api/settings", json={**SETTINGS, **changes}, headers=panel.auth())


def users(panel):
    return panel.rows("SELECT * FROM users ORDER BY id")


# ---------------------------------------------------------------- creating users

def test_creating_a_user_issues_a_certificate_and_a_link(panel):
    r = panel.create_user("anna", "Anna's phone")
    assert r["user"]["name"] == "anna" and r["share"]["url"].startswith("https://192.0.2.10:8443/p/")
    assert r["share"]["qr"].startswith("data:image/svg+xml")
    (row,) = users(panel)
    assert row["cn"].startswith("anna_") and row["cert_expires"] == "2030-01-01T00:00:00+00:00"
    assert panel.helper.called("issue_cert") == [{"cn": row["cn"], "days": 730}]


def test_the_certificate_validity_comes_from_the_settings(panel):
    assert put_settings(panel, cert_days=30).status_code == 200
    panel.create_user("anna")
    assert panel.helper.called("issue_cert")[0]["days"] == 30


def test_if_the_helper_fails_no_user_is_left_behind(panel):
    panel.helper.fail("issue_cert")
    r = panel.client.post("/api/users", json={"name": "anna"}, headers=panel.auth())
    assert r.status_code == 500 and users(panel) == []
    assert panel.rows("SELECT 1 FROM shares") == []


@pytest.mark.parametrize("name", ["Bad Name", "a", "x" * 33, "ok\n", "../etc", "-leading", "semi;colon", "ü", ""])
def test_invalid_user_names_are_rejected(panel, name):
    assert panel.client.post("/api/users", json={"name": name}, headers=panel.auth()).status_code == 422
    assert panel.helper.called("issue_cert") == []


def test_duplicate_names_are_rejected_without_touching_the_helper(panel):
    panel.create_user("anna")
    r = panel.client.post("/api/users", json={"name": "anna"}, headers=panel.auth())
    assert r.status_code == 409 and len(panel.helper.called("issue_cert")) == 1


# ---------------------------------------------------------------- renew

def test_renewing_replaces_the_certificate_and_the_link(panel):
    first = panel.create_user("anna")
    (before,) = users(panel)
    r = panel.client.post(f"/api/users/{before['id']}/renew", headers=panel.auth())
    assert r.status_code == 200
    (after,) = users(panel)
    assert after["cn"] != before["cn"] and after["cn"].startswith("anna_")
    assert after["cert_expires"] == "2031-01-01T00:00:00+00:00"
    assert panel.helper.called("renew_cert") == [{"old_cn": before["cn"], "new_cn": after["cn"], "days": 730,
                                                  "keep_disabled": False}]
    old_token = first["share"]["url"].rsplit("/", 1)[1]
    assert panel.client.get(f"/dl/{old_token}").status_code == 404  # the old link died with the old certificate


def test_a_failed_renewal_changes_nothing(panel):
    first = panel.create_user("anna")
    (before,) = users(panel)
    panel.helper.fail("renew_cert")
    r = panel.client.post(f"/api/users/{before['id']}/renew", headers=panel.auth())
    assert r.status_code == 500
    (after,) = users(panel)
    assert after["cn"] == before["cn"] and after["cert_expires"] == before["cert_expires"]
    token = first["share"]["url"].rsplit("/", 1)[1]
    assert panel.client.get(f"/dl/{token}").status_code == 200  # the user keeps working


def test_renewing_a_disabled_user_keeps_it_disabled(panel):
    panel.create_user("anna")
    (user,) = users(panel)
    panel.client.patch(f"/api/users/{user['id']}", json={"enabled": False}, headers=panel.auth())
    r = panel.client.post(f"/api/users/{user['id']}/renew", headers=panel.auth())
    assert r.status_code == 200 and r.json()["share"] is None
    assert panel.helper.called("renew_cert")[0]["keep_disabled"] is True
    assert users(panel)[0]["enabled"] == 0


# ---------------------------------------------------------------- delete, enable, disable, kick

def test_deleting_revokes_the_certificate_first(panel):
    panel.create_user("anna")
    (user,) = users(panel)
    assert panel.client.delete(f"/api/users/{user['id']}", headers=panel.auth()).status_code == 200
    assert panel.helper.called("revoke_cert") == [{"cn": user["cn"]}] and users(panel) == []
    assert panel.client.delete(f"/api/users/{user['id']}", headers=panel.auth()).status_code == 404


def test_if_the_revocation_fails_the_user_stays_so_it_can_be_retried(panel):
    panel.create_user("anna")
    (user,) = users(panel)
    panel.helper.fail("revoke_cert")
    assert panel.client.delete(f"/api/users/{user['id']}", headers=panel.auth()).status_code == 500
    assert len(users(panel)) == 1
    panel.helper.errors.clear()
    assert panel.client.delete(f"/api/users/{user['id']}", headers=panel.auth()).status_code == 200


def test_disabling_and_enabling(panel):
    panel.create_user("anna")
    (user,) = users(panel)
    headers = panel.auth()
    panel.client.patch(f"/api/users/{user['id']}", json={"enabled": False}, headers=headers)
    assert users(panel)[0]["enabled"] == 0 and panel.rows("SELECT 1 FROM shares") == []
    panel.client.patch(f"/api/users/{user['id']}", json={"enabled": True}, headers=headers)
    assert users(panel)[0]["enabled"] == 1
    assert [c["disabled"] for c in panel.helper.called("set_disabled")] == [True, False]


def test_a_failed_disable_leaves_the_user_enabled(panel):
    panel.create_user("anna")
    (user,) = users(panel)
    panel.helper.fail("set_disabled")
    r = panel.client.patch(f"/api/users/{user['id']}", json={"enabled": False}, headers=panel.auth())
    assert r.status_code == 500 and users(panel)[0]["enabled"] == 1


def test_a_disabled_user_cannot_get_a_new_link(panel):
    panel.create_user("anna")
    (user,) = users(panel)
    panel.client.patch(f"/api/users/{user['id']}", json={"enabled": False}, headers=panel.auth())
    assert panel.client.post(f"/api/users/{user['id']}/share", headers=panel.auth()).status_code == 409


def test_unknown_users_give_404(panel):
    headers = panel.auth()
    assert panel.client.post("/api/users/999/kick", headers=headers).status_code == 404
    assert panel.client.post("/api/users/999/renew", headers=headers).status_code == 404
    assert panel.client.get("/api/users/999/profile", headers=headers).status_code == 404


# ---------------------------------------------------------------- activation links

def token_of(r):
    return r["share"]["url"].rsplit("/", 1)[1]


def test_the_activation_page_downloads_the_profile(panel):
    token = token_of(panel.create_user("anna"))
    info = panel.client.get(f"/dl/{token}/info").json()
    assert info["filename"] == "anna.ovpn"
    r = panel.client.get(f"/dl/{token}")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    for expected in ("remote vpn.example.net 1194", "verify-x509-name server name", "tls-version-min 1.2",
                     "<ca>\nCA-PEM\n</ca>", "<key>\nKEY-PEM\n</key>", "<tls-crypt>\nTLSCRYPT-KEY\n</tls-crypt>"):
        assert expected in r.text


def test_a_link_stops_working_after_its_downloads(panel):
    assert put_settings(panel, share_downloads=2).status_code == 200
    token = token_of(panel.create_user("anna"))
    assert [panel.client.get(f"/dl/{token}").status_code for _ in range(3)] == [200, 200, 404]
    assert panel.client.get(f"/dl/{token}/info").status_code == 404


def test_looking_at_the_page_does_not_use_up_a_download(panel):
    token = token_of(panel.create_user("anna"))
    for _ in range(20):
        panel.client.get(f"/dl/{token}/info")
        panel.client.get(f"/p/{token}")
    assert panel.rows("SELECT uses_left FROM shares") == [{"uses_left": 5}]


def test_a_failed_download_does_not_use_up_a_download(panel):
    token = token_of(panel.create_user("anna"))
    panel.helper.fail("profile_material")
    assert panel.client.get(f"/dl/{token}").status_code == 500
    assert panel.rows("SELECT uses_left FROM shares") == [{"uses_left": 5}]


def test_a_new_link_cancels_the_previous_one(panel):
    token = token_of(panel.create_user("anna"))
    (user,) = users(panel)
    new = panel.client.post(f"/api/users/{user['id']}/share", headers=panel.auth()).json()
    assert panel.client.get(f"/dl/{token}").status_code == 404
    assert panel.client.get(f"/dl/{token_of(new)}").status_code == 200


def test_expired_links_stop_working(panel):
    token = token_of(panel.create_user("anna"))
    with panel.db.connect() as conn:
        conn.execute("UPDATE shares SET expires_at = ?", (time.time() - 1,))
    assert panel.client.get(f"/dl/{token}").status_code == 404


def test_the_link_lifetime_comes_from_the_settings(panel):
    put_settings(panel, share_hours=2)
    created = time.time()
    panel.create_user("anna")
    (row,) = panel.rows("SELECT expires_at FROM shares")
    assert abs(row["expires_at"] - (created + 2 * 3600)) < 60


def test_garbage_tokens_give_the_same_answer_as_expired_ones(panel):
    for token in ("x", "A" * 43, "../../etc/passwd"):
        assert panel.client.get(f"/dl/{token}/info").status_code in (404, 405)


# ---------------------------------------------------------------- settings

@pytest.mark.parametrize("changes", [
    {"endpoint_host": "bad host"}, {"endpoint_host": "a\nb"}, {"endpoint_host": ""},
    {"dns": "1.1.1.1\npush x"}, {"dns": "not-an-ip"}, {"routes": "10.0.0.0/99", "full_tunnel": False},
    {"routes": "", "full_tunnel": False}, {"panel_url": "http://insecure.example"}, {"panel_url": "javascript:x"},
    {"cert_days": 0}, {"cert_days": 99999}, {"share_hours": 0}, {"share_downloads": 0}, {"public_port": 70000},
])
def test_invalid_settings_are_rejected(panel, changes):
    assert put_settings(panel, **changes).status_code in (400, 422)
    assert panel.helper.called("apply_server_conf") == []


def test_server_settings_reach_the_helper_as_plain_lists(panel):
    r = put_settings(panel, full_tunnel=False, routes="192.168.1.0/24, 10.0.0.0/8", dns="9.9.9.9")
    assert r.status_code == 200 and r.json()["restarted"] is True
    assert panel.helper.called("apply_server_conf") == [{
        "restart": True, "dns": ["9.9.9.9"], "routes": ["192.168.1.0/24", "10.0.0.0/8"],
        "full_tunnel": False, "client_to_client": False}]


def test_profile_only_settings_do_not_restart_openvpn(panel):
    r = put_settings(panel, endpoint_host="home.example.net", public_port=51194, cert_days=90)
    assert r.status_code == 200 and r.json()["restarted"] is False
    assert panel.helper.called("apply_server_conf") == []


def test_unused_routes_do_not_count_as_a_routing_change(panel):
    """In full-tunnel mode the routes field is ignored, so editing it must not restart OpenVPN."""
    assert put_settings(panel, routes="10.1.0.0/16").json()["restarted"] is False
    assert put_settings(panel, full_tunnel=False, routes="10.1.0.0/16").json()["restarted"] is True
    assert put_settings(panel, full_tunnel=False, routes="10.1.0.0/16", cert_days=10).json()["restarted"] is False
    assert put_settings(panel, full_tunnel=False, routes="10.2.0.0/16").json()["restarted"] is True


def test_if_openvpn_rejects_the_settings_they_are_not_saved(panel):
    panel.helper.fail("apply_server_conf")
    assert put_settings(panel, dns="9.9.9.9").status_code == 500
    assert panel.client.get("/api/settings", headers=panel.auth()).json()["values"]["dns"] == "1.1.1.1, 9.9.9.9"


# ---------------------------------------------------------------- audit and helper outages

def test_actions_are_audited(panel):
    panel.create_user("anna")
    (user,) = users(panel)
    headers = panel.auth()
    panel.client.patch(f"/api/users/{user['id']}", json={"enabled": False}, headers=headers)
    panel.client.patch(f"/api/users/{user['id']}", json={"enabled": True}, headers=headers)
    panel.client.post(f"/api/users/{user['id']}/renew", headers=headers)
    put_settings(panel, cert_days=90)
    panel.client.delete(f"/api/users/{user['id']}", headers=headers)
    actions = [r["action"] for r in panel.rows("SELECT action FROM audit ORDER BY id")]
    for wanted in ("user.create", "user.disable", "user.enable", "user.renew", "settings.update", "user.delete"):
        assert wanted in actions
    detail = panel.rows("SELECT detail FROM audit WHERE action = 'settings.update'")[0]["detail"]
    assert "cert_days=90" in detail


def test_the_audit_log_can_be_read_and_paged(panel):
    for i in range(5):
        panel.create_user(f"user{i}")
    headers = panel.auth()
    page = panel.client.get("/api/audit?limit=3", headers=headers).json()
    assert len(page) == 3 and page[0]["id"] > page[-1]["id"]
    older = panel.client.get(f"/api/audit?limit=100&before={page[-1]['id']}", headers=headers).json()
    assert older and all(e["id"] < page[-1]["id"] for e in older)


def test_audit_entries_cannot_inject_control_characters(panel):
    panel.login(username="evil\nuser\x1b[31m")
    (row,) = panel.rows("SELECT actor FROM audit")
    assert "\n" not in row["actor"] and "\x1b" not in row["actor"]


def test_old_audit_entries_are_pruned_on_login(panel, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "audit_retention_days", 1)
    with panel.db.connect() as conn:
        conn.execute("INSERT INTO audit(ts, actor, ip, action) VALUES (?, 'x', 'y', 'ancient')", (time.time() - 5 * 86400,))
    panel.login()
    assert panel.rows("SELECT 1 FROM audit WHERE action = 'ancient'") == []


def test_when_the_helper_is_down_the_panel_says_so_instead_of_crashing(panel):
    panel.helper.fail("issue_cert", code="unavailable", message="connection refused")
    r = panel.client.post("/api/users", json={"name": "anna"}, headers=panel.auth())
    assert r.status_code == 503 and "helper" in r.json()["detail"]
    panel.helper.fail("overview", code="unavailable", message="connection refused")
    o = panel.client.get("/api/overview", headers=panel.auth())
    assert o.status_code == 200 and o.json()["server"]["running"] is False and o.json()["server"]["error"]
