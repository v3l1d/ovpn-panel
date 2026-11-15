"""Install-time parameters (/etc/ovpn-panel/config.json, written by the installer).

The file is owned by root and read-only for the panel, so a compromised panel cannot change what
the privileged helper uses (port, protocol, subnet, paths, restart command).
What the admin can change from the panel lives in the database instead (settings.py).
"""
import json
import os
from types import SimpleNamespace

DEFAULTS = {
    "db_path": "/var/lib/ovpn-panel/panel.db",
    "panel_user": "ovpn-panel",
    "helper_socket": "/run/ovpn-panel/helper.sock",
    "panel_port": 8443,
    "panel_hosts": [],           # host names the panel answers to (Host header allow-list)
    "default_panel_host": "",    # host used in activation links when no panel URL is set
    "panel_allowed_cidrs": [],   # networks that may reach the panel port (firewall)
    "trusted_proxies": [],       # reverse proxies whose X-Forwarded-For is believed
    "session_ttl_hours": 8,
    "audit_retention_days": 365,
    "public_host": "vpn.example.com",
    # defaults for settings editable in the panel
    "cert_days": 730,
    "share_hours": 24,
    "share_downloads": 5,
    # OpenVPN server
    "port": 1194,
    "proto": "udp",
    "subnet": "10.9.0.0/24",
    "crl_days": 365,
    "easyrsa_dir": "/etc/openvpn/easy-rsa",
    "server_dir": "/etc/openvpn/server",
    "tls_dir": "/etc/ovpn-panel",
    "mgmt_socket": "/run/openvpn-mgmt.sock",
    "restart_cmd": ["systemctl", "restart", "openvpn-server@server"],
}


CONFIG_PATH = os.environ.get("OVPN_PANEL_CONFIG", "/etc/ovpn-panel/config.json")


def load() -> SimpleNamespace:
    data = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            data.update(json.load(f))
    return SimpleNamespace(**data)


CONFIG = load()
