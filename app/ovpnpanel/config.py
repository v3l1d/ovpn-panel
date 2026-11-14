"""Install-time parameters (written by the installer to /etc/ovpn-panel/config.json).

What the admin can change from the panel lives in the database instead (settings.py).
"""
import json
import os
from types import SimpleNamespace

DEFAULTS = {
    "db_path": "/var/lib/ovpn-panel/panel.db",
    "token_ttl_hours": 12,
    "share_ttl_hours": 72,
    "public_host": "vpn.example.com",
    # OpenVPN server
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
