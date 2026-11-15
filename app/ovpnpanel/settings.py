"""Settings editable from the panel.

- PROFILE_KEYS apply to profiles downloaded from now on (endpoint, certificate and link policy).
- SERVER_KEYS go into server.conf: saving them restarts OpenVPN and they apply to every client.
"""
import json
import re

from . import validate
from .config import CONFIG

PROFILE_KEYS = {"endpoint_host", "public_port", "panel_url", "cert_days", "share_hours", "share_downloads"}
SERVER_KEYS = {"dns", "full_tunnel", "routes", "client_to_client"}


def defaults() -> dict:
    return {
        "endpoint_host": CONFIG.public_host,
        "public_port": int(CONFIG.port),
        "panel_url": "",
        "dns": "1.1.1.1, 9.9.9.9",
        "full_tunnel": True,
        "routes": "192.168.1.0/24",
        "client_to_client": False,
        "cert_days": int(CONFIG.cert_days),
        "share_hours": int(CONFIG.share_hours),
        "share_downloads": int(CONFIG.share_downloads),
    }


def load(conn) -> dict:
    s = defaults()
    for r in conn.execute("SELECT key, value FROM settings"):
        if r["key"] in s:
            s[r["key"]] = json.loads(r["value"])
    return s


def validate_settings(values: dict) -> dict:
    """Normalizes the values; raises ValueError with a user-facing message."""
    out = dict(values)
    try:
        out["endpoint_host"] = validate.host(values["endpoint_host"])
    except validate.ValidationError:
        raise ValueError("Endpoint: enter a hostname (e.g. home.ddns.net) or an IP address")

    url = values["panel_url"].strip().rstrip("/")
    if url and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s]*)?", url):
        raise ValueError("Panel URL: must be an https:// address, e.g. https://vpn.example.net:8443")
    out["panel_url"] = url

    try:
        out["public_port"] = validate.int_range(values["public_port"], "Public port", 1, 65535)
        out["cert_days"] = validate.int_range(values["cert_days"], "Certificate validity", 1, 3650)
        out["share_hours"] = validate.int_range(values["share_hours"], "Link validity", 1, 720)
        out["share_downloads"] = validate.int_range(values["share_downloads"], "Downloads per link", 1, 50)
        out["dns"] = ", ".join(validate.ipv4_addresses(values["dns"], "DNS", 4))
        routes = validate.ipv4_networks(values["routes"], "Routed networks")
    except validate.ValidationError as e:
        raise ValueError(str(e))
    if not values["full_tunnel"] and not routes:
        raise ValueError("Routed networks: enter at least one network, or choose All traffic")
    out["routes"] = ", ".join(routes)
    out["full_tunnel"] = bool(values["full_tunnel"])
    out["client_to_client"] = bool(values["client_to_client"])
    return out


def validate_server(values: dict) -> dict:
    """Strict validation of what reaches server.conf. Used by the privileged helper."""
    try:
        dns = validate.ipv4_addresses(values["dns"], "dns", 4)
        routes = validate.ipv4_networks(values["routes"], "routes")
        full, c2c = values["full_tunnel"], values["client_to_client"]
    except (KeyError, validate.ValidationError) as e:
        raise ValueError(f"invalid server settings: {e}")
    if not isinstance(full, bool) or not isinstance(c2c, bool):
        raise ValueError("invalid server settings: flags must be booleans")
    if not full and not routes:
        raise ValueError("invalid server settings: no routes")
    return {"dns": dns, "routes": routes, "full_tunnel": full, "client_to_client": c2c}


def server_params(s: dict) -> dict:
    return {"dns": validate.split_csv(s["dns"]), "routes": validate.split_csv(s["routes"]),
            "full_tunnel": s["full_tunnel"], "client_to_client": s["client_to_client"]}


def effective_server(s: dict) -> dict:
    """What server.conf will actually contain: the routes only count in split-tunnel mode.
    Comparing this (not the raw fields) avoids restarting OpenVPN when nothing real changed."""
    p = server_params(s)
    if p["full_tunnel"]:
        p["routes"] = []
    return p


def save(conn, values: dict):
    for k, v in values.items():
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
