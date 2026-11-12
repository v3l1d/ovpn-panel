"""Impostazioni modificabili dal pannello.

- PROFILE_KEYS finiscono nei profili .ovpn: valgono per i profili scaricati da ora in poi.
- SERVER_KEYS finiscono in server.conf: salvarle riavvia OpenVPN e valgono per tutti i client.
"""
import ipaddress
import json
import re

from .config import CONFIG

PROFILE_KEYS = {"endpoint_host", "public_port", "panel_url"}
SERVER_KEYS = {"dns", "full_tunnel", "routes", "client_to_client"}

_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def defaults() -> dict:
    return {
        "endpoint_host": CONFIG.public_host,
        "public_port": int(CONFIG.port),
        "panel_url": "",
        "dns": "1.1.1.1, 9.9.9.9",
        "full_tunnel": True,
        "routes": "192.168.1.0/24",
        "client_to_client": False,
    }


def load(conn) -> dict:
    s = defaults()
    for r in conn.execute("SELECT key, value FROM settings"):
        if r["key"] in s:
            s[r["key"]] = json.loads(r["value"])
    return s


def _split(value: str):
    return [p.strip() for p in value.split(",") if p.strip()]


def validate(values: dict) -> dict:
    """Normalizza i valori; solleva ValueError con un messaggio per l'utente."""
    out = dict(values)

    host = values["endpoint_host"].strip().strip("[]")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_RE.match(host):
            raise ValueError("Endpoint: inserisci un nome host (es. casa.ddns.net) o un IP")
    out["endpoint_host"] = host

    url = values["panel_url"].strip().rstrip("/")
    if url and not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", url):
        raise ValueError("URL del pannello: deve iniziare con https://")
    out["panel_url"] = url

    try:
        out["dns"] = ", ".join(str(ipaddress.IPv4Address(d)) for d in _split(values["dns"]))
    except ValueError as e:
        raise ValueError(f"DNS: {e}")

    try:
        nets = [ipaddress.IPv4Network(n, strict=False) for n in _split(values["routes"])]
    except ValueError as e:
        raise ValueError(f"Reti instradate: {e}")
    if not values["full_tunnel"] and not nets:
        raise ValueError("Reti instradate: indica almeno una rete, oppure scegli Tutto il traffico")
    out["routes"] = ", ".join(str(n) for n in nets)
    return out


def save(conn, values: dict):
    for k, v in values.items():
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))


def routes(s: dict):
    for n in _split(s["routes"]):
        net = ipaddress.IPv4Network(n, strict=False)
        yield str(net.network_address), str(net.netmask)


def dns_servers(s: dict):
    return _split(s["dns"])
