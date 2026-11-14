"""End-to-end test against a real installation (created by tests/run.sh).

Drives the API the way the panel does and brings up real OpenVPN tunnels. SMOKE=1 runs only
the quick checks (used after restarting the container, to verify everything comes back by itself).
"""
import os
import subprocess
import sys
import time

import requests

URL = os.environ["PANEL_URL"]
INTERNET = "1.1.1.1"
S = requests.Session()
S.verify = os.environ["CA_FILE"]
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


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def ping(ip, tries=10):
    return any(subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True).returncode == 0
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


def start_vpn(profile, tag, extra=""):
    path, log = f"/tmp/{tag}.ovpn", f"/tmp/{tag}.log"
    with open(path, "w") as f:
        f.write(profile + extra)
    sh(f"openvpn --config {path} --daemon --log {log} --writepid /tmp/{tag}.pid")
    return log


def stop_vpn(tag):
    sh(f"kill $(cat /tmp/{tag}.pid) 2>/dev/null; sleep 1")


def user(name):
    return next((u for u in api("GET", "/api/users") if u["name"] == name), None)


def server_up():
    try:
        return api("GET", "/api/overview")["server"]["running"]
    except Exception:
        return False


def done():
    print(f"\n{'ALL OK' if not failures else f'{len(failures)} CHECKS FAILED'}")
    sys.exit(1 if failures else 0)


# in the test container: no default route through the VPN, only the VPN network and one public IP
# to exercise the server's NAT
SPLIT = f'pull-filter ignore "redirect-gateway"\nroute {INTERNET} 255.255.255.255\n'

step("Login")
check(S.post(URL + "/api/login", json={"username": "admin", "password": "wrong"}, timeout=30).status_code == 401,
      "wrong password rejected")
tok = api("POST", "/api/login", json={"username": os.environ["ADMIN_USER"], "password": os.environ["ADMIN_PASSWORD"]})["token"]
S.headers["Authorization"] = "Bearer " + tok
check(True, "admin login")

step("Server")
check(wait_for(server_up), "OpenVPN running (management interface reachable)")
ov = api("GET", "/api/overview")
check(ov["server"].get("version", "").startswith("2."), f"OpenVPN version read ({ov['server'].get('version')})")
log_lines = api("GET", "/api/server/log?lines=50")
check(len(log_lines) > 0, "server log available")
check(not any(l["msg"].startswith("MANAGEMENT:") for l in log_lines), "log free of the panel's own management lines")

if os.environ.get("SMOKE"):
    done()

name = f"e2e{int(time.time()) % 100000}"
try:
    step("Create a user")
    r = api("POST", "/api/users", json={"name": name, "note": "automated test"})
    uid, share = r["user"]["id"], r["share"]
    check("/p/" in share["url"] and share["qr"].startswith("data:image/svg+xml"), "activation link and QR code")
    check(S.post(URL + "/api/users", json={"name": name}, timeout=30).status_code == 409, "duplicate name rejected")
    check(S.post(URL + "/api/users", json={"name": "Not Valid"}, timeout=30).status_code == 422, "invalid name rejected")
    token = share["url"].rsplit("/", 1)[1]
    page = api("GET", f"/p/{token}")
    check("Set up your VPN" in page, "activation page")
    check(api("GET", f"/dl/{token}/info")["filename"] == f"{name}.ovpn", "the page recognizes the profile")
    profile = api("GET", f"/dl/{token}")
    check(profile == api("GET", f"/api/users/{uid}/profile") and "<tls-crypt>" in profile and "remote pve 1194" in profile,
          "profile from the link matches the one in the panel")

    step("Connect")
    log = start_vpn(profile, "a", SPLIT)
    check(wait_for(lambda: connected(log)), "OpenVPN tunnel established")
    check(ping("10.9.0.1"), "ping the server through the tunnel")
    check("dev tun" in sh(f"ip route get {INTERNET}") and ping(INTERNET), "Internet through the server (NAT)")
    check(wait_for(lambda: (user(name) or {}).get("online")), "user shown online in the panel")
    u = user(name)
    check((u.get("vpn_ip") or "").startswith("10.9.0.") and u.get("real_address"), f"VPN IP and real address ({u.get('vpn_ip')})")
    check(api("GET", "/api/overview")["server"]["nclients"] >= 1, "client counted in the statistics")

    step("Disconnect and disable")
    api("POST", f"/api/users/{uid}/kick")
    check(any("kill" in l["msg"].lower() or name in l["msg"] for l in api("GET", "/api/server/log?lines=40")),
          "disconnect recorded in the log")
    api("PATCH", f"/api/users/{uid}", json={"enabled": False})
    check(wait_for(lambda: not user(name)["online"], 30), "disabled user no longer connected")
    time.sleep(8)
    check(not user(name)["online"], "and cannot reconnect")
    check(S.get(URL + f"/dl/{token}/info", timeout=30).status_code == 404, "activation link invalidated")
    stop_vpn("a")
    api("PATCH", f"/api/users/{uid}", json={"enabled": True})
    log = start_vpn(profile, "b", SPLIT)
    check(wait_for(lambda: connected(log)), "re-enabled: reconnects with the same profile")
    stop_vpn("b")

    step("Renew the certificate")
    r = api("POST", f"/api/users/{uid}/renew")
    new_profile = api("GET", f"/dl/{r['share']['url'].rsplit('/', 1)[1]}")
    check(new_profile != profile, "new profile issued")
    log = start_vpn(profile, "c", SPLIT)
    check(not wait_for(lambda: connected(log), 12), "the old profile is rejected (certificate revoked)")
    stop_vpn("c")
    log = start_vpn(new_profile, "d", SPLIT)
    check(wait_for(lambda: connected(log)), "the new profile connects")
    stop_vpn("d")

    step("Server settings")
    original = api("GET", "/api/settings")["values"]
    check(S.put(URL + "/api/settings", json={**original, "endpoint_host": "not valid!"}, timeout=30).status_code == 422,
          "invalid endpoint rejected")
    r = api("PUT", "/api/settings", json={**original, "full_tunnel": False, "routes": "10.77.0.0/24", "dns": "9.9.9.9"})
    check(r["restarted"], "saving the routing restarts OpenVPN")
    check(wait_for(server_up, 30), "OpenVPN back up after the restart")
    log = start_vpn(new_profile, "e")
    check(wait_for(lambda: connected(log)), "connects with the new settings")
    routes = sh("ip route")
    check("10.77.0.0/24" in routes and "0.0.0.0/1" not in routes, "the client only gets the configured network (split tunnel)")
    stop_vpn("e")
    r = api("PUT", "/api/settings", json={**original, "endpoint_host": "home.example.net", "public_port": 51194})
    check(not r["restarted"], "changing the endpoint does not restart OpenVPN")
    check("remote home.example.net 51194" in api("GET", f"/api/users/{uid}/profile"), "the profile uses the new endpoint")
    api("PUT", "/api/settings", json=original)
    check(wait_for(server_up, 30), "original settings restored")

    step("Restart and account")
    api("POST", "/api/server/restart")
    check(wait_for(server_up, 30), "OpenVPN restarted from the panel")
    check(S.post(URL + "/api/me/password", json={"current": "wrong", "new": "newpassword123"}, timeout=30).status_code == 400,
          "password change with a wrong current password rejected")
finally:
    if user(name):
        api("DELETE", f"/api/users/{user(name)['id']}")
check(user(name) is None and S.get(URL + f"/dl/{token}/info", timeout=30).status_code == 404, "user deleted and link invalidated")

done()
