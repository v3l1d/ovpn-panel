"""End-to-end tests against a real installation (created by tests/run.sh).

They drive the API the way the panel does, bring up real OpenVPN tunnels (one of them in its own
network namespace) and check the security claims on the real system: firewall, client isolation,
certificate lifetime, links, sessions, audit log.

PHASE selects what runs (tests/run.sh calls them in this order):
    old        the previous release, before the upgrade        dr_pre / dr_post   around backup+restore
    upgraded   after upgrading it in place                     lockout / locked   login rate limit
    main       everything else                                 smoke              quick checks
"""
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
import urllib3

PHASE = os.environ.get("PHASE", "main")
URL = os.environ["PANEL_URL"]
INTERNET = "1.1.1.1"
SERVER = "pve"
S = requests.Session()
S.verify = False if PHASE == "old" else os.environ["CA_FILE"]
if PHASE == "old":
    urllib3.disable_warnings()
failures = []


def step(title):
    print(f"\n=== {title}")


def check(ok, what):
    print(("  OK   " if ok else "  FAIL ") + what)
    if not ok:
        failures.append(what)


def api(method, path, expect=200, **kw):
    r = S.request(method, URL + path, timeout=60, **kw)
    if expect is not None and r.status_code != expect:
        raise AssertionError(f"{method} {path}: expected {expect}, got {r.status_code} {r.text[:200]}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text


def status(method, path, **kw):
    return S.request(method, URL + path, timeout=60, **kw).status_code


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw).stdout


def ping(ip, tries=10, ns=None):
    prefix = ["ip", "netns", "exec", ns] if ns else []
    return any(subprocess.run([*prefix, "ping", "-c", "1", "-W", "1", ip], capture_output=True).returncode == 0
               for _ in range(tries))


def wait_for(fn, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(1)
    return False


def connected(log):
    return os.path.exists(log) and "Initialization Sequence Completed" in open(log).read()


def start_vpn(profile, tag, extra="", ns=None, server_ip=None):
    """A real OpenVPN client. extra: split tunnel by default, since the test container keeps its own routes."""
    if server_ip:
        profile = profile.replace(f"remote {SERVER} ", f"remote {server_ip} ")
    path, log = f"/tmp/{tag}.ovpn", f"/tmp/{tag}.log"
    with open(path, "w") as f:
        f.write(profile + extra)
    prefix = f"ip netns exec {ns} " if ns else ""
    sh(f"{prefix}openvpn --config {path} --daemon --log {log} --writepid /tmp/{tag}.pid")
    return log


def stop_vpn(tag):
    sh(f"kill $(cat /tmp/{tag}.pid) 2>/dev/null; sleep 1")


SPLIT = f'pull-filter ignore "redirect-gateway"\nroute {INTERNET} 255.255.255.255\n'
SPLIT_PEER = 'pull-filter ignore "redirect-gateway"\n'


def users():
    return {u["name"]: u for u in api("GET", "/api/users")}


def user(name):
    return users().get(name)


def server_up():
    try:
        return api("GET", "/api/overview")["server"]["running"]
    except Exception:
        return False


def login(username=None, password=None):
    return S.post(URL + "/api/login", json={"username": username or os.environ["ADMIN_USER"],
                                            "password": password or os.environ["ADMIN_PASSWORD"]}, timeout=30)


def sign_in():
    r = login()
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    S.headers["Authorization"] = "Bearer " + r.json()["token"]
    return r.json()["token"]


def settings_now():
    return api("GET", "/api/settings")["values"]


def put_settings(**changes):
    return api("PUT", "/api/settings", json={**settings_now(), **changes})


def cert_days(profile):
    pem = re.search(r"<cert>\n(.*?)\n</cert>", profile, re.S).group(1)
    out = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem, capture_output=True, text=True).stdout
    end = datetime.strptime(out.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (end - datetime.now(timezone.utc)).days


def done():
    print(f"\n{'ALL OK' if not failures else f'{len(failures)} CHECKS FAILED'}")
    sys.exit(1 if failures else 0)


# ---------------------------------------------------------------- phases around the upgrade

def phase_old():
    step("Previous release")
    sign_in()
    api("POST", "/api/users", json={"name": "legacy", "note": "created by the previous release"})
    api("POST", "/api/users", json={"name": "legacyoff"})
    ids = {u["name"]: u["id"] for u in api("GET", "/api/users")}
    api("PATCH", f"/api/users/{ids['legacyoff']}", json={"enabled": False})
    profile = api("GET", f"/api/users/{ids['legacy']}/profile")
    off_profile = api("GET", f"/api/users/{ids['legacyoff']}/profile")
    open("/cache/legacy.ovpn", "w").write(profile)
    open("/cache/legacyoff.ovpn", "w").write(off_profile)
    log = start_vpn(profile, "old1", SPLIT)
    check(wait_for(lambda: connected(log)), "the previous release works: legacy user connects")
    stop_vpn("old1")
    check(cert_days(profile) > 3000, f"its certificates last about 10 years ({cert_days(profile)} days)")
    done()


def phase_upgraded():
    step("After the in-place upgrade")
    sign_in()
    check(True, "the old admin password still works")
    got = users()
    check({"legacy", "legacyoff"} <= set(got), "users survived the upgrade")
    check(got["legacyoff"]["enabled"] is False, "a disabled user is still disabled")
    check(wait_for(lambda: (user("legacy") or {}).get("cert_expires")), "certificate expiry dates were backfilled")
    check(server_up(), "OpenVPN running")
    s = settings_now()
    check(s["cert_days"] == 730 and s["share_downloads"] == 5, "new settings have sensible defaults")
    log = start_vpn(open("/cache/legacy.ovpn").read(), "up1", SPLIT)
    check(wait_for(lambda: connected(log)), "a profile issued BEFORE the upgrade still connects (PKI untouched)")
    check(wait_for(lambda: (user("legacy") or {}).get("online")), "and shows as online in the new panel")
    stop_vpn("up1")
    log = start_vpn(open("/cache/legacyoff.ovpn").read(), "up2", SPLIT)
    check(not wait_for(lambda: connected(log), 12), "a disabled user's old profile is still refused")
    stop_vpn("up2")
    check(isinstance(api("GET", "/api/audit"), list), "the new audit log is available")
    done()


def phase_dr_pre():
    step("Before backup")
    sign_in()
    api("POST", "/api/users", json={"name": "drtest", "note": "survives the disaster"})
    api("POST", "/api/users", json={"name": "drgone"})
    ids = {u["name"]: u["id"] for u in api("GET", "/api/users")}
    open("/cache/dr.ovpn", "w").write(api("GET", f"/api/users/{ids['drtest']}/profile"))
    open("/cache/drgone.ovpn", "w").write(api("GET", f"/api/users/{ids['drgone']}/profile"))
    api("DELETE", f"/api/users/{ids['drgone']}")  # revoked before the backup
    check(True, "users prepared: drtest (valid) and drgone (revoked)")
    done()


def phase_dr_post():
    step("After the restore")
    sign_in()
    check("drtest" in users() and "drgone" not in users(), "the database came back")
    log = start_vpn(open("/cache/dr.ovpn").read(), "dr1", SPLIT)
    check(wait_for(lambda: connected(log)), "a certificate issued before the disaster connects (PKI restored)")
    stop_vpn("dr1")
    log = start_vpn(open("/cache/drgone.ovpn").read(), "dr2", SPLIT)
    check(not wait_for(lambda: connected(log), 12), "a certificate revoked before the backup is still revoked (CRL restored)")
    stop_vpn("dr2")
    done()


def phase_lockout():
    step("Login rate limit")
    codes = [login(password="wrong-password").status_code for _ in range(6)]
    check(codes == [401] * 5 + [429], f"five failures, then locked out {codes}")
    check(login().status_code == 429, "even the right password is refused while locked out")
    done()


def phase_locked():
    step("After a restart")
    check(login().status_code == 429, "the lockout survived the restart of the whole container")
    done()


def phase_smoke():
    step("Quick checks")
    sign_in()
    check(server_up(), "OpenVPN running after the restart")
    check(status("GET", "/api/health") == 200, "panel answering")
    check("drtest" in users(), "data intact")
    check(len(api("GET", "/api/pki")["items"]) == 5, "certificate status available")
    done()


# ---------------------------------------------------------------- the main phase

@contextlib.contextmanager
def peer_namespace():
    """A second network, joined to this one by a veth pair and NAT, to run a second VPN client."""
    sh("ip netns add peer")
    sh("ip link add vpeer0 type veth peer name vpeer1")
    sh("ip link set vpeer1 netns peer")
    sh("ip addr add 192.168.99.1/30 dev vpeer0; ip link set vpeer0 up")
    sh("ip netns exec peer ip addr add 192.168.99.2/30 dev vpeer1")
    sh("ip netns exec peer ip link set vpeer1 up; ip netns exec peer ip link set lo up")
    sh("ip netns exec peer ip route add default via 192.168.99.1")
    sh("sysctl -qw net.ipv4.ip_forward=1")
    sh("iptables -t nat -A POSTROUTING -s 192.168.99.0/30 -j MASQUERADE")
    try:
        yield
    finally:
        sh("iptables -t nat -D POSTROUTING -s 192.168.99.0/30 -j MASQUERADE")
        sh("ip netns del peer")
        sh("ip link del vpeer0 2>/dev/null")


def phase_main():
    check(login(password="wrong-password").status_code == 401, "a wrong password is rejected")
    sign_in()
    original = settings_now()
    created = []
    name = f"e2e{int(time.time()) % 100000}"
    try:
        step("HTTPS and host names")
        check(status("GET", "/api/health") == 200, "panel reachable over HTTPS with the private CA")
        try:
            requests.get(URL + "/api/health", timeout=10)  # system trust store: our CA is not in it
            check(False, "without the CA the certificate is not trusted")
        except (requests.exceptions.SSLError, OSError):
            check(True, "without the CA the certificate is not trusted")
        try:
            requests.get(URL.replace("https://", "http://") + "/", timeout=10)
            check(False, "plain HTTP is refused")
        except requests.exceptions.RequestException:
            check(True, "plain HTTP is refused")
        check(status("GET", "/api/health", headers={"Host": "evil.example"}) == 400, "a forged Host header is refused")
        csp = S.get(URL + "/", timeout=10).headers.get("Content-Security-Policy", "")
        check("default-src 'none'" in csp and "unsafe-inline" not in re.search(r"script-src ([^;]*)", csp).group(1),
              "pages carry a strict Content-Security-Policy")
        check(S.get(URL + "/panel-ca.crt", timeout=10).text.startswith("-----BEGIN CERTIFICATE-----"), "the panel CA can be downloaded")

        step("Users, links and certificate lifetime")
        r = api("POST", "/api/users", json={"name": name, "note": "automated test"})
        created.append(r["user"]["id"])
        uid, share = r["user"]["id"], r["share"]
        check(share["url"].startswith("https://") and "/p/" in share["url"], "activation link and QR code")
        host = share["url"].split("/")[2].split(":")[0]
        r2 = S.post(URL + "/api/users", json={"name": name + "x"}, headers={"Host": "localhost"}, timeout=60).json()
        created.append(r2["user"]["id"])
        check(r2["share"]["url"].split("/")[2].split(":")[0] == host and "localhost" not in r2["share"]["url"],
              "links do not follow the request's Host header")
        api("DELETE", f"/api/users/{r2['user']['id']}")
        created.remove(r2["user"]["id"])
        token = share["url"].rsplit("/", 1)[1]
        check("Set up your VPN" in api("GET", f"/p/{token}"), "activation page")
        profile = api("GET", f"/dl/{token}")
        check(profile == api("GET", f"/api/users/{uid}/profile") and "<tls-crypt>" in profile
              and "verify-x509-name server name" in profile, "profile from the link matches the panel's")
        check(700 <= cert_days(profile) <= 731, f"client certificates last 2 years by default ({cert_days(profile)} days)")
        put_settings(cert_days=30, share_downloads=2)
        r3 = api("POST", "/api/users", json={"name": name + "short"})
        created.append(r3["user"]["id"])
        short_profile = api("GET", f"/api/users/{r3['user']['id']}/profile")
        check(25 <= cert_days(short_profile) <= 31, f"the validity is configurable ({cert_days(short_profile)} days)")
        t3 = r3["share"]["url"].rsplit("/", 1)[1]
        check([status("GET", f"/dl/{t3}") for _ in range(3)] == [200, 200, 404], "a link works for the allowed number of downloads only")
        api("DELETE", f"/api/users/{r3['user']['id']}")
        created.remove(r3["user"]["id"])
        put_settings(cert_days=original["cert_days"], share_downloads=original["share_downloads"])

        step("Connect")
        log = start_vpn(profile, "a", SPLIT)
        check(wait_for(lambda: connected(log)), "OpenVPN tunnel established")
        check(ping("10.9.0.1"), "ping the server through the tunnel")
        check("dev tun" in sh(f"ip route get {INTERNET}") and ping(INTERNET), "Internet through the server (NAT)")
        check(wait_for(lambda: (user(name) or {}).get("online")), "user shown online in the panel")
        u = user(name)
        check((u.get("vpn_ip") or "").startswith("10.9.0.") and u.get("real_address"), f"VPN IP and real address ({u.get('vpn_ip')})")
        check(u.get("cert_expires"), "certificate expiry shown")

        step("Firewall: the panel is not reachable from the VPN")
        check(sh(f"curl -sk --max-time 6 -o /dev/null -w '%{{http_code}}' {URL}/api/health") == "200",
              "the panel answers on the local network")
        check(sh("curl -sk --max-time 6 -o /dev/null -w '%{http_code}' https://10.9.0.1:8443/api/health") != "200",
              "but not to a VPN client (10.9.0.1:8443)")

        step("Firewall: VPN clients cannot reach each other")
        with peer_namespace():
            peer = api("POST", "/api/users", json={"name": name + "peer"})
            created.append(peer["user"]["id"])
            peer_profile = api("GET", f"/api/users/{peer['user']['id']}/profile")
            server_ip = sh(f"getent hosts {SERVER}").split()[0]
            logb = start_vpn(peer_profile, "b", SPLIT_PEER, ns="peer", server_ip=server_ip)
            check(wait_for(lambda: connected(logb)), "a second client connects from another network")
            check(wait_for(lambda: (user(name + "peer") or {}).get("online")), "and shows as online")
            peer_ip, a_ip = user(name + "peer")["vpn_ip"], user(name)["vpn_ip"]
            check(not ping(peer_ip, tries=4), "client-to-client off: A cannot reach B")
            check(not ping(a_ip, tries=4, ns="peer"), "and B cannot reach A")
            check(ping("10.9.0.1", ns="peer"), "while both still reach the server")

            put_settings(client_to_client=True)
            check(wait_for(server_up, 30), "client-to-client switched on (OpenVPN restarted)")
            stop_vpn("a")
            stop_vpn("b")
            log = start_vpn(profile, "a", SPLIT)
            logb = start_vpn(peer_profile, "b", SPLIT_PEER, ns="peer", server_ip=server_ip)
            check(wait_for(lambda: connected(log)) and wait_for(lambda: connected(logb)), "both reconnect")
            check(wait_for(lambda: (user(name) or {}).get("online") and (user(name + "peer") or {}).get("online")),
                  "both online again")
            peer_ip = user(name + "peer")["vpn_ip"]
            check(ping(peer_ip), "client-to-client on: A reaches B (OpenVPN forwards internally)")
            put_settings(client_to_client=False)
            check(wait_for(server_up, 30), "switched off again")
            stop_vpn("b")
            api("DELETE", f"/api/users/{peer['user']['id']}")
            created.remove(peer["user"]["id"])
            log = start_vpn(profile, "a", SPLIT)
            check(wait_for(lambda: connected(log)), "reconnect for the next checks")

        step("Disconnect, disable, enable")
        check(wait_for(lambda: (user(name) or {}).get("online")), "online before the test")
        api("POST", f"/api/users/{uid}/kick")
        api("PATCH", f"/api/users/{uid}", json={"enabled": False})
        check(wait_for(lambda: not user(name)["online"], 30), "disabled user no longer connected")
        time.sleep(8)
        check(not user(name)["online"], "and cannot reconnect")
        check(status("GET", f"/dl/{token}/info") == 404, "activation link invalidated")
        stop_vpn("a")
        api("PATCH", f"/api/users/{uid}", json={"enabled": True})
        log = start_vpn(profile, "b", SPLIT)
        check(wait_for(lambda: connected(log)), "re-enabled: reconnects with the same profile")
        stop_vpn("b")

        step("Renewing certificates")
        r = api("POST", f"/api/users/{uid}/renew")
        new_profile = api("GET", f"/dl/{r['share']['url'].rsplit('/', 1)[1]}")
        check(new_profile != profile, "new profile issued")
        log = start_vpn(profile, "c", SPLIT)
        check(not wait_for(lambda: connected(log), 12), "the old profile is rejected (certificate revoked)")
        stop_vpn("c")
        log = start_vpn(new_profile, "d", SPLIT)
        check(wait_for(lambda: connected(log)), "the new profile connects")
        stop_vpn("d")
        api("PATCH", f"/api/users/{uid}", json={"enabled": False})
        r = api("POST", f"/api/users/{uid}/renew")
        check(r["share"] is None and user(name)["enabled"] is False, "renewing a disabled user keeps it disabled")
        disabled_profile = api("GET", f"/api/users/{uid}/profile")
        log = start_vpn(disabled_profile, "e", SPLIT)
        check(not wait_for(lambda: connected(log), 12), "and the new certificate of a disabled user cannot connect")
        stop_vpn("e")
        api("PATCH", f"/api/users/{uid}", json={"enabled": True})

        step("Server settings")
        check(status("PUT", "/api/settings", json={**settings_now(), "endpoint_host": "not valid!"}) == 422, "invalid endpoint rejected")
        r = put_settings(full_tunnel=False, routes="10.77.0.0/24", dns="9.9.9.9")
        check(r["restarted"], "saving the routing restarts OpenVPN")
        check(wait_for(server_up, 30), "OpenVPN back up after the restart")
        log = start_vpn(api("GET", f"/api/users/{uid}/profile"), "f")
        check(wait_for(lambda: connected(log)), "connects with the new settings")
        routes = sh("ip route")
        check("10.77.0.0/24" in routes and "0.0.0.0/1" not in routes, "the client only gets the configured network (split tunnel)")
        stop_vpn("f")
        r = put_settings(endpoint_host="home.example.net", public_port=51194)
        check(not r["restarted"], "changing the endpoint does not restart OpenVPN")
        check("remote home.example.net 51194" in api("GET", f"/api/users/{uid}/profile"), "the profile uses the new endpoint")
        check(not put_settings(cert_days=60)["restarted"], "nor does changing the certificate validity")
        put_settings(**{k: original[k] for k in original})
        check(wait_for(server_up, 30), "original settings restored")
        api("POST", "/api/server/restart")
        check(wait_for(server_up, 30), "OpenVPN restarted from the panel")
        log_lines = api("GET", "/api/server/log?lines=100")
        check(len(log_lines) > 0 and not any(l["msg"].startswith("MANAGEMENT:") for l in log_lines), "server log without the management chatter")

        step("Sessions and passwords")
        other = S.post(URL + "/api/login", json={"username": os.environ["ADMIN_USER"], "password": os.environ["ADMIN_PASSWORD"]}, timeout=30).json()["token"]
        check(S.get(URL + "/api/me", headers={"Authorization": "Bearer " + other}, timeout=30).status_code == 200, "a second session works")
        check(status("POST", "/api/me/password", json={"current": "wrong", "new": "newpassword123"}) == 400, "wrong current password refused")
        sign_in()
        check(status("POST", "/api/logout") == 200 and status("GET", "/api/me") == 401, "logout invalidates the token")
        sign_in()

        step("PKI status and audit log")
        pki = api("GET", "/api/pki")
        check({i["key"] for i in pki["items"]} == {"ca", "server", "crl", "panel", "panel_ca"}
              and all(i["days_left"] and i["days_left"] > 0 for i in pki["items"]), "certificate status lists everything")
        check(len(pki["panel_ca_fingerprint"]) > 40, "panel CA fingerprint shown")
        actions = {e["action"] for e in api("GET", "/api/audit?limit=500")}
        wanted = {"login", "logout", "user.create", "user.delete", "user.disable", "user.enable", "user.renew",
                  "user.kick", "settings.update", "server.restart", "share.download", "profile.download"}
        check(wanted <= actions, f"the audit log recorded the actions (missing: {sorted(wanted - actions)})")
        check(any(e["action"] == "login" and not e["ok"] for e in api("GET", "/api/audit?limit=500")), "failed logins are recorded")
    finally:
        for i in created:
            S.delete(URL + f"/api/users/{i}", timeout=60)
        with contextlib.suppress(Exception):
            put_settings(**{k: original[k] for k in original})
    check(not [n for n in users() if n.startswith(name)], "test users cleaned up")
    done()


PHASES = {"old": phase_old, "upgraded": phase_upgraded, "main": phase_main, "dr_pre": phase_dr_pre,
          "dr_post": phase_dr_post, "lockout": phase_lockout, "locked": phase_locked, "smoke": phase_smoke}
PHASES[PHASE]()
