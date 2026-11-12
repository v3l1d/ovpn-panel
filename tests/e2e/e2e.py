"""Test end-to-end contro un'installazione reale (creata da tests/run.sh).

Usa l'API come il pannello e alza tunnel OpenVPN veri. SMOKE=1 fa solo i controlli rapidi
(usato dopo il riavvio del container per verificare che tutto riparta da solo).
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
        raise AssertionError(f"{method} {path}: atteso {expect}, ricevuto {r.status_code} {r.text[:200]}")
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


# test: niente default route nel container, solo la rete VPN e un IP pubblico per provare il NAT
SPLIT = f'pull-filter ignore "redirect-gateway"\nroute {INTERNET} 255.255.255.255\n'

step("Login")
check(S.post(URL + "/api/login", json={"username": "admin", "password": "sbagliata"}, timeout=30).status_code == 401,
      "password errata rifiutata")
tok = api("POST", "/api/login", json={"username": os.environ["ADMIN_USER"], "password": os.environ["ADMIN_PASSWORD"]})["token"]
S.headers["Authorization"] = "Bearer " + tok
check(True, "login amministratore")

step("Server")
check(wait_for(server_up), "OpenVPN in esecuzione (management raggiungibile)")
ov = api("GET", "/api/overview")
check(ov["server"].get("version", "").startswith("2."), f"versione OpenVPN letta ({ov['server'].get('version')})")
check(len(api("GET", "/api/server/log?lines=50")) > 0, "log del server disponibile")

if os.environ.get("SMOKE"):
    print(f"\n{'TUTTO OK' if not failures else f'{len(failures)} VERIFICHE FALLITE'}")
    sys.exit(1 if failures else 0)

name = f"e2e{int(time.time()) % 100000}"
try:
    step("Creazione utente")
    r = api("POST", "/api/users", json={"name": name, "note": "test automatico"})
    uid, share = r["user"]["id"], r["share"]
    check("/p/" in share["url"] and share["qr"].startswith("data:image/svg+xml"), "link di attivazione e QR")
    check(S.post(URL + "/api/users", json={"name": name}, timeout=30).status_code == 409, "nome duplicato rifiutato")
    check(S.post(URL + "/api/users", json={"name": "Nome Non Valido"}, timeout=30).status_code == 422, "nome non valido rifiutato")
    token = share["url"].rsplit("/", 1)[1]
    page = api("GET", f"/p/{token}")
    check("Attiva la VPN" in page, "pagina di attivazione")
    check(api("GET", f"/dl/{token}/info")["filename"] == f"{name}.ovpn", "la pagina riconosce il profilo")
    profile = api("GET", f"/dl/{token}")
    check(profile == api("GET", f"/api/users/{uid}/profile") and "<tls-crypt>" in profile and "remote pve 1194" in profile,
          "profilo scaricato dal link uguale a quello del pannello")

    step("Connessione")
    log = start_vpn(profile, "a", SPLIT)
    check(wait_for(lambda: connected(log)), "tunnel OpenVPN stabilito")
    check(ping("10.9.0.1"), "ping al server attraverso il tunnel")
    check("dev tun" in sh(f"ip route get {INTERNET}") and ping(INTERNET), "uscita su Internet attraverso il server (NAT)")
    check(wait_for(lambda: (user(name) or {}).get("online")), "utente online nel pannello")
    u = user(name)
    check((u.get("vpn_ip") or "").startswith("10.9.0.") and u.get("real_address"), f"IP VPN e indirizzo reale ({u.get('vpn_ip')})")
    check(api("GET", "/api/overview")["server"]["nclients"] >= 1, "client conteggiato nelle statistiche")

    step("Disconnetti e disabilita")
    api("POST", f"/api/users/{uid}/kick")
    check(any("kill" in l["msg"].lower() or name in l["msg"] for l in api("GET", "/api/server/log?lines=40")),
          "disconnessione registrata nel log")
    api("PATCH", f"/api/users/{uid}", json={"enabled": False})
    check(wait_for(lambda: not user(name)["online"], 30), "utente disabilitato non piu' connesso")
    time.sleep(8)
    check(not user(name)["online"], "e non riesce a riconnettersi")
    check(S.get(URL + f"/dl/{token}/info", timeout=30).status_code == 404, "link di attivazione invalidato")
    stop_vpn("a")
    api("PATCH", f"/api/users/{uid}", json={"enabled": True})
    log = start_vpn(profile, "b", SPLIT)
    check(wait_for(lambda: connected(log)), "riabilitato: si riconnette con lo stesso profilo")
    stop_vpn("b")

    step("Rinnovo certificato")
    r = api("POST", f"/api/users/{uid}/renew")
    new_profile = api("GET", f"/dl/{r['share']['url'].rsplit('/', 1)[1]}")
    check(new_profile != profile, "nuovo profilo emesso")
    log = start_vpn(profile, "c", SPLIT)
    check(not wait_for(lambda: connected(log), 12), "il vecchio profilo e' rifiutato (certificato revocato)")
    stop_vpn("c")
    log = start_vpn(new_profile, "d", SPLIT)
    check(wait_for(lambda: connected(log)), "il nuovo profilo si connette")
    stop_vpn("d")

    step("Impostazioni del server")
    original = api("GET", "/api/settings")["values"]
    check(S.put(URL + "/api/settings", json={**original, "endpoint_host": "non valido!"}, timeout=30).status_code == 422,
          "endpoint non valido rifiutato")
    r = api("PUT", "/api/settings", json={**original, "full_tunnel": False, "routes": "10.77.0.0/24", "dns": "9.9.9.9"})
    check(r["restarted"], "salvare l'instradamento riavvia OpenVPN")
    check(wait_for(server_up, 30), "OpenVPN di nuovo attivo dopo il riavvio")
    log = start_vpn(new_profile, "e")
    check(wait_for(lambda: connected(log)), "connessione con le nuove impostazioni")
    routes = sh("ip route")
    check("10.77.0.0/24" in routes and "0.0.0.0/1" not in routes, "il client riceve solo la rete indicata (split tunnel)")
    stop_vpn("e")
    r = api("PUT", "/api/settings", json={**original, "endpoint_host": "casa.example.net", "public_port": 51194})
    check(not r["restarted"], "cambiare l'endpoint non riavvia OpenVPN")
    check("remote casa.example.net 51194" in api("GET", f"/api/users/{uid}/profile"), "il profilo usa il nuovo endpoint")
    api("PUT", "/api/settings", json=original)
    check(wait_for(server_up, 30), "impostazioni originali ripristinate")

    step("Riavvio e account")
    api("POST", "/api/server/restart")
    check(wait_for(server_up, 30), "riavvio di OpenVPN dal pannello")
    check(S.post(URL + "/api/me/password", json={"current": "sbagliata", "new": "nuovapassword123"}, timeout=30).status_code == 400,
          "cambio password con password attuale errata rifiutato")
finally:
    if user(name):
        api("DELETE", f"/api/users/{user(name)['id']}")
check(user(name) is None and S.get(URL + f"/dl/{token}/info", timeout=30).status_code == 404, "utente eliminato e link invalidato")

print(f"\n{'TUTTO OK' if not failures else f'{len(failures)} VERIFICHE FALLITE'}")
sys.exit(1 if failures else 0)
