"""Parametri d'installazione (scritti dall'installer in /etc/ovpn-panel/config.json).

Cio' che l'admin puo' cambiare dal pannello sta invece nel database (settings.py).
"""
import json
import os
from types import SimpleNamespace

DEFAULTS = {
    "db_path": "/var/lib/ovpn-panel/panel.db",
    "token_ttl_hours": 12,
    "share_ttl_hours": 72,
    "public_host": "vpn.example.com",
    # server OpenVPN
    "port": 1194,
    "proto": "udp",
    "subnet": "10.9.0.0/24",
    "easyrsa_dir": "/etc/openvpn/easy-rsa",
    "server_dir": "/etc/openvpn/server",
    "mgmt_socket": "/run/openvpn-mgmt.sock",
    "restart_cmd": ["systemctl", "restart", "openvpn-server@server"],
}


def load() -> SimpleNamespace:
    data = dict(DEFAULTS)
    path = os.environ.get("OVPN_PANEL_CONFIG", "/etc/ovpn-panel/config.json")
    if os.path.exists(path):
        with open(path) as f:
            data.update(json.load(f))
    return SimpleNamespace(**data)


CONFIG = load()
