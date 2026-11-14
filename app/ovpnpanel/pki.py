"""Client certificates (easy-rsa) and .ovpn profiles."""
import os
import secrets
import shutil
import subprocess

from .config import CONFIG

# the default 180-day CRL would lock out every client when it expires
_ENV = {"EASYRSA_BATCH": "1", "EASYRSA_CRL_DAYS": "3650", "EASYRSA_CERT_EXPIRE": "3650",
        "EASYRSA_ALGO": "ec", "EASYRSA_CURVE": "prime256v1"}


def _easyrsa(*args):
    d = CONFIG.easyrsa_dir
    env = dict(os.environ, EASYRSA_PKI=os.path.join(d, "pki"), **_ENV)
    subprocess.run(["./easyrsa", *args], cwd=d, env=env, capture_output=True, text=True, check=True)


def _read(path) -> str:
    with open(path) as f:
        return f.read()


def _pem_block(text: str, label: str) -> str:
    begin, end = f"-----BEGIN {label}-----", f"-----END {label}-----"
    start = text.index(begin)
    return text[start:text.index(end, start) + len(end)]


def new_cn(name: str) -> str:
    return f"{name}_{secrets.token_hex(3)}"


def issue(cn: str):
    _easyrsa("build-client-full", cn, "nopass")


def profile(cn: str, s: dict) -> str:
    """Complete .ovpn profile, rebuilt from the PKI on every request."""
    pki = os.path.join(CONFIG.easyrsa_dir, "pki")
    ca = _pem_block(_read(os.path.join(pki, "ca.crt")), "CERTIFICATE")
    cert = _pem_block(_read(os.path.join(pki, "issued", f"{cn}.crt")), "CERTIFICATE")
    key = _read(os.path.join(pki, "private", f"{cn}.key")).strip()
    tc = _read(os.path.join(CONFIG.server_dir, "tc.key")).strip()
    lines = [
        "client",
        "dev tun",
        f"proto {CONFIG.proto}",
        f"remote {s['endpoint_host']} {s['public_port']}",
        "resolv-retry infinite",
        "nobind",
        "persist-key",
        "persist-tun",
        "remote-cert-tls server",
        "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
        "verb 3",
    ]
    if CONFIG.proto == "udp":
        lines.append("explicit-exit-notify 1")
    return "\n".join(lines + [
        f"<ca>\n{ca}\n</ca>",
        f"<cert>\n{cert}\n</cert>",
        f"<key>\n{key}\n</key>",
        f"<tls-crypt>\n{tc}\n</tls-crypt>",
        "",
    ])


def revoke(cn: str):
    _easyrsa("revoke", cn)
    _easyrsa("gen-crl")
    # openvpn rereads the CRL on every connection: replace it atomically, otherwise
    # a client connecting while it is being written would see a truncated file
    dest = os.path.join(CONFIG.server_dir, "crl.pem")
    shutil.copyfile(os.path.join(CONFIG.easyrsa_dir, "pki", "crl.pem"), dest + ".tmp")
    os.chmod(dest + ".tmp", 0o644)
    os.replace(dest + ".tmp", dest)
    set_disabled(cn, False)


def set_disabled(cn: str, disabled: bool):
    """Blocks/unblocks a client via client-config-dir (applies from its next connection)."""
    ccd = os.path.join(CONFIG.server_dir, "ccd")
    os.makedirs(ccd, exist_ok=True)
    path = os.path.join(ccd, cn)
    if disabled:
        with open(path, "w") as f:
            f.write("disable\n")
        os.chmod(path, 0o644)
    elif os.path.exists(path):
        os.remove(path)
