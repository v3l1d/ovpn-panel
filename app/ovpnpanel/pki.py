"""Certificati client (easy-rsa) e profili .ovpn."""
import os
import secrets
import shutil
import subprocess

from .config import CONFIG

# CRL a 180 giorni (default) bloccherebbe tutti i client alla scadenza
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
    """Profilo .ovpn completo, rigenerato dalla PKI a ogni richiesta."""
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
    # openvpn rilegge la CRL a ogni connessione: sostituzione atomica,
    # altrimenti un client che si collega durante la scrittura trova il file troncato
    dest = os.path.join(CONFIG.server_dir, "crl.pem")
    shutil.copyfile(os.path.join(CONFIG.easyrsa_dir, "pki", "crl.pem"), dest + ".tmp")
    os.chmod(dest + ".tmp", 0o644)
    os.replace(dest + ".tmp", dest)
    set_disabled(cn, False)


def set_disabled(cn: str, disabled: bool):
    """Blocca/sblocca un client via client-config-dir (vale dalla connessione successiva)."""
    ccd = os.path.join(CONFIG.server_dir, "ccd")
    os.makedirs(ccd, exist_ok=True)
    path = os.path.join(ccd, cn)
    if disabled:
        with open(path, "w") as f:
            f.write("disable\n")
        os.chmod(path, 0o644)
    elif os.path.exists(path):
        os.remove(path)
