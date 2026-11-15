import base64
import hashlib
import re

from ovpnpanel import main


# ---------------------------------------------------------------- Host header

def test_an_unknown_host_is_refused(panel):
    for path in ("/", "/api/health", "/p/abc"):
        r = panel.client.get(path, headers={"Host": "evil.example"})
        assert r.status_code == 400, path


def test_configured_hosts_work_with_and_without_a_port(panel):
    for host in ("testserver", "testserver:8443", "192.0.2.10:8443", "localhost", "TESTSERVER"):
        assert panel.client.get("/api/health", headers={"Host": host}).status_code == 200, host


def test_ipv6_literal_hosts_are_parsed(panel):
    assert panel.client.get("/api/health", headers={"Host": "[::1]:8443"}).status_code == 200
    assert panel.client.get("/api/health", headers={"Host": "[2001:db8::1]:8443"}).status_code == 400


def test_the_public_panel_url_adds_its_host(panel):
    assert panel.client.get("/api/health", headers={"Host": "panel.example.net"}).status_code == 400
    body = {"endpoint_host": "vpn.example.net", "public_port": 1194, "panel_url": "https://panel.example.net:8443",
            "dns": "1.1.1.1", "full_tunnel": True, "routes": "", "client_to_client": False,
            "cert_days": 730, "share_hours": 24, "share_downloads": 5}
    assert panel.client.put("/api/settings", json=body, headers=panel.auth()).status_code == 200
    assert panel.client.get("/api/health", headers={"Host": "panel.example.net"}).status_code == 200


def test_activation_links_never_use_the_request_host(panel):
    link = panel.create_user()["share"]["url"]
    assert link.startswith("https://192.0.2.10:8443/p/")
    r = panel.client.post("/api/users", json={"name": "marco"},
                          headers={**panel.auth(), "Host": "localhost:9999", "X-Forwarded-Host": "evil.example"})
    assert r.json()["share"]["url"].startswith("https://192.0.2.10:8443/p/")


def test_activation_links_use_the_configured_panel_url(panel):
    body = {"endpoint_host": "vpn.example.net", "public_port": 1194, "panel_url": "https://panel.example.net",
            "dns": "1.1.1.1", "full_tunnel": True, "routes": "", "client_to_client": False,
            "cert_days": 730, "share_hours": 24, "share_downloads": 5}
    panel.client.put("/api/settings", json=body, headers=panel.auth())
    assert panel.create_user()["share"]["url"].startswith("https://panel.example.net/p/")


# ---------------------------------------------------------------- headers and CSP

def _page(panel, path):
    r = panel.client.get(path)
    assert r.status_code == 200
    return r


def test_pages_carry_a_strict_content_security_policy(panel):
    for path in ("/", "/p/anything"):
        csp = _page(panel, path).headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        script_src = re.search(r"script-src ([^;]*)", csp).group(1)
        assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
        assert "'self'" not in script_src and "*" not in script_src


def test_the_csp_allows_exactly_the_inline_blocks_of_the_page(panel):
    for path in ("/", "/p/anything"):
        r = _page(panel, path)
        csp = r.headers["content-security-policy"]
        for tag in ("script", "style"):
            for block in re.findall(rf"<{tag}>(.*?)</{tag}>", r.text, re.S):
                digest = "'sha256-" + base64.b64encode(hashlib.sha256(block.encode()).digest()).decode() + "'"
                assert digest in csp, f"{tag} block of {path} is not allowed by the CSP"


def test_pages_have_no_inline_event_handlers_or_external_resources(panel):
    """Inline handlers would need 'unsafe-inline'; external hosts would leak who uses the panel."""
    for path in ("/", "/p/anything"):
        markup = re.sub(r"<(script|style)>.*?</\1>", "", _page(panel, path).text, flags=re.S)
        assert not re.search(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", markup), "inline event handler"
        assert not re.search(r'(?:src|href)=["\']https?://(?!openvpn\.net)', markup), "external resource"


def test_every_response_has_the_hardening_headers(panel):
    for path in ("/", "/api/health", "/nope"):
        h = panel.client.get(path).headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert h["cache-control"] == "no-store"
        assert h["cross-origin-opener-policy"] == "same-origin"


def test_the_api_documentation_is_not_exposed(panel):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert panel.client.get(path).status_code == 404


def test_the_panel_ca_is_public_but_the_key_is_not_served(panel, cfg, tmp_path):
    import os

    os.makedirs(cfg.tls_dir)
    (tmp_path / "tls" / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
    (tmp_path / "tls" / "ca.key").write_text("SECRET")
    r = panel.client.get("/panel-ca.crt")
    assert r.status_code == 200 and b"BEGIN CERTIFICATE" in r.content
    assert panel.client.get("/ca.key").status_code == 404


def test_errors_do_not_leak_internal_details(panel):
    panel.helper.fail("issue_cert", message="Traceback: /etc/openvpn/easy-rsa/pki/private/ca.key")
    r = panel.client.post("/api/users", json={"name": "anna"}, headers=panel.auth())
    assert r.status_code == 500
    assert "ca.key" not in r.text and "Traceback" not in r.text
