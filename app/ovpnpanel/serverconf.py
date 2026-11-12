"""server.conf generato dal pannello."""
import ipaddress
import os
import subprocess

from . import settings
from .config import CONFIG


def render(s: dict) -> str:
    net = ipaddress.IPv4Network(CONFIG.subnet)
    lines = [
        "# Generato da ovpn-panel: le modifiche manuali vengono sovrascritte",
        f"port {CONFIG.port}",
        f"proto {CONFIG.proto}",
        "dev tun",
        "ca ca.crt",
        "cert server.crt",
        "key server.key",
        "dh none",
        "tls-crypt tc.key",
        "crl-verify crl.pem",
        "client-config-dir ccd",
        "topology subnet",
        f"server {net.network_address} {net.netmask}",
        "keepalive 10 120",
        "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
        "user nobody",
        "group nogroup",
        "persist-key",
        "persist-tun",
        f"management {CONFIG.mgmt_socket} unix",
        "verb 3",
    ]
    if CONFIG.proto == "udp":
        lines.append("explicit-exit-notify 1")
    if s["full_tunnel"]:
        lines.append('push "redirect-gateway def1 bypass-dhcp"')
    else:
        lines += [f'push "route {a} {m}"' for a, m in settings.routes(s)]
    lines += [f'push "dhcp-option DNS {d}"' for d in settings.dns_servers(s)]
    if s["client_to_client"]:
        lines.append("client-to-client")
    return "\n".join(lines) + "\n"


def write(s: dict):
    path = os.path.join(CONFIG.server_dir, "server.conf")
    with open(path + ".tmp", "w") as f:
        f.write(render(s))
    os.replace(path + ".tmp", path)


def restart():
    subprocess.run(CONFIG.restart_cmd, capture_output=True, text=True, check=True)
