"""Builds a client .ovpn profile from the certificate material returned by the helper."""
from .config import CONFIG


def build(material: dict, s: dict) -> str:
    lines = [
        "client",
        "dev tun",
        f"proto {CONFIG.proto}",
        f"remote {s['endpoint_host']} {int(s['public_port'])}",
        "resolv-retry infinite",
        "nobind",
        "persist-key",
        "persist-tun",
        "remote-cert-tls server",
        "verify-x509-name server name",
        "tls-version-min 1.2",
        "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
        "verb 3",
    ]
    if CONFIG.proto == "udp":
        lines.append("explicit-exit-notify 1")
    return "\n".join(lines + [
        f"<ca>\n{material['ca']}\n</ca>",
        f"<cert>\n{material['cert']}\n</cert>",
        f"<key>\n{material['key']}\n</key>",
        f"<tls-crypt>\n{material['tls_crypt']}\n</tls-crypt>",
        "",
    ])
