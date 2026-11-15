import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlsplit

import segno
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__, audit, db, profile, ratelimit, security, settings
from . import helper_client as helper
from .config import CONFIG

log = logging.getLogger("ovpn-panel")
# the application's own messages (including the audit trail) go to stderr, hence to the journal
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
log.addHandler(_handler)
log.setLevel(logging.INFO)

NAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{1,31}$"  # ends up in the certificate CN and the .ovpn file name
MAX_SESSIONS = 10
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "no-store",
}


# ---------------------------------------------------------------- pages and Content-Security-Policy

def _load_page(name: str):
    """The pages are single files with one inline script and style: the CSP allows exactly those
    blocks by hash, so injected markup or script cannot run."""
    with open(os.path.join(_STATIC, name), encoding="utf-8", newline="") as f:
        html = f.read()

    def digest(blocks):
        return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(b.encode("utf-8")).digest()).decode() + "'"
                        for b in blocks)

    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    styles = re.findall(r"<style>(.*?)</style>", html, re.S)
    csp = "; ".join([
        "default-src 'none'",
        f"script-src {digest(scripts)}",
        f"style-src {digest(styles)}",
        "style-src-attr 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ])
    return html.encode("utf-8"), csp


_PAGES = {name: _load_page(f"{name}.html") for name in ("index", "connect")}


def _page(name: str) -> Response:
    body, csp = _PAGES[name]
    return Response(body, media_type="text/html; charset=utf-8", headers={"Content-Security-Policy": csp})


# ---------------------------------------------------------------- host names and client addresses

_hosts_cache = [0.0, frozenset()]


def allowed_hosts() -> frozenset:
    """Host names the panel answers to. Anything else (DNS rebinding, a forged Host header) is refused."""
    now = time.monotonic()
    if now - _hosts_cache[0] > 10:
        hosts = {h.lower() for h in CONFIG.panel_hosts} | {"localhost", "127.0.0.1", "::1"}
        if CONFIG.default_panel_host:
            hosts.add(CONFIG.default_panel_host.lower())
        try:
            with db.connect() as conn:
                url = settings.load(conn)["panel_url"]
            if url:
                hosts.add((urlsplit(url).hostname or "").lower())
        except sqlite3.Error:
            pass
        _hosts_cache[:] = [now, frozenset(h for h in hosts if h)]
    return _hosts_cache[1]


def forget_hosts():
    _hosts_cache[0] = 0.0


def _request_host(request: Request) -> str:
    host = request.headers.get("host", "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if ":" in host else host


def client_ip(request: Request) -> str:
    """The peer address; X-Forwarded-For is only believed when it comes from a trusted proxy."""
    peer = request.client.host if request.client else "?"
    trusted = set(CONFIG.trusted_proxies)
    if peer in trusted:
        for hop in reversed([h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]):
            if hop not in trusted:
                return hop
    return peer


# ---------------------------------------------------------------- application

def _backfill_cert_expiry():
    """Databases from older versions do not know when each certificate expires."""
    try:
        with db.connect() as conn:
            for row in conn.execute("SELECT id, cn FROM users WHERE cert_expires IS NULL").fetchall():
                info = helper.call("cert_info", cn=row["cn"], timeout=15)
                if info.get("not_after"):
                    conn.execute("UPDATE users SET cert_expires = ? WHERE id = ?", (info["not_after"], row["id"]))
    except Exception:  # noqa: BLE001 - best effort; the helper may not be up yet
        log.info("could not backfill certificate expiry dates yet")


@asynccontextmanager
async def lifespan(_app):
    db.init()
    _backfill_cert_expiry()
    yield


app = FastAPI(title="ovpn-panel", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def guard(request: Request, call_next):
    if _request_host(request) not in allowed_hosts():
        return PlainTextResponse("Invalid host header", status_code=400, headers=SECURITY_HEADERS)
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def _helper(op: str, **args):
    """Calls the privileged helper and turns its errors into clean HTTP errors."""
    try:
        return helper.call(op, **args)
    except helper.HelperError as e:
        if e.code == "unavailable":
            raise HTTPException(503, "The privileged helper is not running (systemctl status ovpn-panel-helper)")
        if e.code == "not_found":
            raise HTTPException(404, e.message)
        if e.code == "invalid":
            raise HTTPException(400, f"Rejected by the helper: {e.message}")
        log.error("helper operation %s failed: %s", op, e.message)
        raise HTTPException(500, "The operation failed on the OpenVPN server (see journalctl -u ovpn-panel-helper)")


# ---------------------------------------------------------------- authentication

def current_admin(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    if creds is None:
        raise HTTPException(401, "Missing token")
    with db.connect() as conn:
        row = conn.execute(
            """SELECT a.* FROM tokens t JOIN admins a ON a.id = t.admin_id
               WHERE t.token_hash = ? AND t.expires_at > ?""",
            (security.token_hash(creds.credentials), time.time()),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Session expired or invalid")
    return dict(row)


class LoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = client_ip(request)
    username = body.username.strip().lower()[:64]
    with db.connect() as conn:
        if ratelimit.blocked(conn, ip, username):
            conn.commit()
            log.warning("login blocked for %s from %s", username, ip)
            raise HTTPException(429, "Too many attempts, try again in a few minutes")
        row = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        # an unknown user costs the same hashing work as a wrong password: no user enumeration by timing
        good = security.verify_password(body.password, row["pw_hash"] if row else security.DUMMY_HASH)
        if row is None or not good:
            ratelimit.record_failure(conn, ip, username)
            audit.record(conn, username, ip, "login", ok=False)
            conn.commit()
            raise HTTPException(401, "Invalid credentials")
        ratelimit.clear(conn, ip, username)
        now = time.time()
        token = security.new_token()
        conn.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO tokens(token_hash, admin_id, expires_at, created_at, ip) VALUES (?, ?, ?, ?, ?)",
                     (security.token_hash(token), row["id"], now + CONFIG.session_ttl_hours * 3600, now, ip))
        conn.execute(
            "DELETE FROM tokens WHERE admin_id = ? AND token_hash NOT IN "
            "(SELECT token_hash FROM tokens WHERE admin_id = ? ORDER BY created_at DESC LIMIT ?)",
            (row["id"], row["id"], MAX_SESSIONS))
        audit.record(conn, username, ip, "login")
        audit.prune(conn)
    return {"token": token, "username": row["username"]}


@app.post("/api/logout")
def logout(request: Request, creds: HTTPAuthorizationCredentials = Depends(bearer), admin=Depends(current_admin)):
    with db.connect() as conn:
        conn.execute("DELETE FROM tokens WHERE token_hash = ?", (security.token_hash(creds.credentials),))
        audit.record(conn, admin["username"], client_ip(request), "logout")
    return {"ok": True}


@app.get("/api/me")
def me(admin=Depends(current_admin)):
    return {"username": admin["username"]}


class PasswordIn(BaseModel):
    current: str = Field(max_length=1000)
    new: str = Field(min_length=10, max_length=1000)


@app.post("/api/me/password")
def change_password(body: PasswordIn, request: Request,
                    creds: HTTPAuthorizationCredentials = Depends(bearer), admin=Depends(current_admin)):
    ip = client_ip(request)
    with db.connect() as conn:
        if not security.verify_password(body.current, admin["pw_hash"]):
            audit.record(conn, admin["username"], ip, "password.change", ok=False)
            conn.commit()
            raise HTTPException(400, "The current password is incorrect")
        conn.execute("UPDATE admins SET pw_hash = ? WHERE id = ?", (security.hash_password(body.new), admin["id"]))
        # sign out every other session, not this one
        conn.execute("DELETE FROM tokens WHERE admin_id = ? AND token_hash != ?",
                     (admin["id"], security.token_hash(creds.credentials)))
        audit.record(conn, admin["username"], ip, "password.change")
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- server

@app.get("/api/overview")
def overview(_=Depends(current_admin)):
    server = {"running": False, "port": CONFIG.port, "proto": CONFIG.proto, "subnet": CONFIG.subnet, "version": ""}
    try:
        ov = helper.call("overview", timeout=15)
        state = ov.get("state") or {}
        server.update(state)
        server.update(ov.get("stats") or {})
        server["version"] = ov.get("version", "")
        server["running"] = state.get("state") == "CONNECTED"
        if ov.get("error"):
            server["error"] = ov["error"]
    except helper.HelperError as e:
        server["error"] = e.message
    with db.connect() as conn:
        users = conn.execute("SELECT COUNT(*) AS n, SUM(enabled) AS enabled FROM users").fetchone()
        s = settings.load(conn)
    return {"server": server, "users": users["n"], "users_enabled": users["enabled"] or 0,
            "endpoint": f"{s['endpoint_host']}:{s['public_port']}", "panel_version": __version__}


@app.get("/api/pki")
def pki_status(_=Depends(current_admin)):
    return _helper("pki_status", timeout=20)


@app.get("/api/server/log")
def server_log(lines: int = 150, _=Depends(current_admin)):
    return _helper("journal", lines=min(max(lines, 1), 1000), timeout=30)


@app.post("/api/server/restart")
def server_restart(request: Request, admin=Depends(current_admin)):
    _helper("restart")
    with db.connect() as conn:
        audit.record(conn, admin["username"], client_ip(request), "server.restart")
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = 100, before: int = 0, _=Depends(current_admin)):
    with db.connect() as conn:
        return audit.entries(conn, limit, before)


# ---------------------------------------------------------------- VPN users

def _user_out(row, clients) -> dict:
    d = {k: row[k] for k in ("id", "name", "note", "created_at", "cert_expires")}
    d["enabled"] = bool(row["enabled"])
    c = clients.get(row["cn"])
    d["online"] = c is not None
    d.update(c or {})
    return d


def _get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "User not found")
    return row


def _panel_base(s: dict) -> str:
    """Base address of the activation links. Never derived from the request's Host header."""
    if s["panel_url"]:
        return s["panel_url"]
    host = CONFIG.default_panel_host or (CONFIG.panel_hosts[0] if CONFIG.panel_hosts else "")
    if not host:
        raise HTTPException(409, "Set the public panel URL in Settings first")
    return f"https://{host}:{int(CONFIG.panel_port)}"


def _share(conn, user, s: dict) -> dict:
    """A new activation link; earlier links of the same user stop working."""
    token, expires = security.new_token(), time.time() + s["share_hours"] * 3600
    conn.execute("DELETE FROM shares WHERE expires_at < ? OR uses_left <= 0 OR user_id = ?", (time.time(), user["id"]))
    conn.execute("INSERT INTO shares(token_hash, user_id, expires_at, uses_left) VALUES (?, ?, ?, ?)",
                 (security.token_hash(token), user["id"], expires, s["share_downloads"]))
    url = f"{_panel_base(s)}/p/{token}"
    return {"url": url, "expires_at": expires, "downloads": s["share_downloads"],
            "qr": segno.make(url, error="m").svg_data_uri(scale=5, border=2, light="#fff")}


def _profile_text(conn, user) -> str:
    return profile.build(_helper("profile_material", cn=user["cn"]), settings.load(conn))


def _clients() -> dict:
    try:
        return helper.call("clients", timeout=15)
    except helper.HelperError:
        return {}


@app.get("/api/users")
def list_users(_=Depends(current_admin)):
    clients = _clients()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY name").fetchall()
    return [_user_out(r, clients) for r in rows]


class UserIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    note: str = Field(default="", max_length=120)


@app.post("/api/users")
def create_user(body: UserIn, request: Request, admin=Depends(current_admin)):
    cn = f"{body.name}_{secrets.token_hex(3)}"
    with db.connect() as conn:
        s = settings.load(conn)
        try:
            cur = conn.execute("INSERT INTO users(name, cn, note, created_at) VALUES (?, ?, ?, ?)",
                               (body.name, cn, body.note.strip(), db.now_iso()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A user with this name already exists")
        # if the helper fails the exception leaves the transaction uncommitted: no user without a certificate
        issued = _helper("issue_cert", cn=cn, days=s["cert_days"])
        conn.execute("UPDATE users SET cert_expires = ? WHERE id = ?", (issued["not_after"], cur.lastrowid))
        user = _get_user(conn, cur.lastrowid)
        share = _share(conn, user, s)
        audit.record(conn, admin["username"], client_ip(request), "user.create", body.name,
                     f"certificate valid for {s['cert_days']} days")
        return {"user": _user_out(user, {}), "share": share}


@app.post("/api/users/{user_id}/share")
def share_user(user_id: int, request: Request, admin=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        if not user["enabled"]:
            raise HTTPException(409, "User is disabled: enable it before sharing the profile")
        share = _share(conn, user, settings.load(conn))
        audit.record(conn, admin["username"], client_ip(request), "share.create", user["name"])
        return {"user": _user_out(user, _clients()), "share": share}


@app.get("/api/users/{user_id}/profile")
def user_profile(user_id: int, request: Request, admin=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        content = _profile_text(conn, user)
        audit.record(conn, admin["username"], client_ip(request), "profile.download", user["name"])
    return PlainTextResponse(content, media_type="application/x-openvpn-profile", headers={
        "Content-Disposition": f'attachment; filename="{user["name"]}.ovpn"'})


class UserPatch(BaseModel):
    enabled: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=120)


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, admin=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        if body.note is not None:
            conn.execute("UPDATE users SET note = ? WHERE id = ?", (body.note.strip(), user_id))
        if body.enabled is not None:
            conn.execute("UPDATE users SET enabled = ? WHERE id = ?", (int(body.enabled), user_id))
            _helper("set_disabled", cn=user["cn"], disabled=not body.enabled)
            if not body.enabled:
                conn.execute("DELETE FROM shares WHERE user_id = ?", (user_id,))
            audit.record(conn, admin["username"], client_ip(request),
                         "user.enable" if body.enabled else "user.disable", user["name"])
    return {"ok": True}


@app.post("/api/users/{user_id}/kick")
def kick_user(user_id: int, request: Request, admin=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        _helper("kill", cn=user["cn"])
        audit.record(conn, admin["username"], client_ip(request), "user.kick", user["name"])
    return {"ok": True}


@app.post("/api/users/{user_id}/renew")
def renew_user(user_id: int, request: Request, admin=Depends(current_admin)):
    """Replaces the certificate (e.g. lost phone). The helper issues the new one and revokes the old one
    as a single step; if it fails, nothing changes."""
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        s = settings.load(conn)
        new_cn = f"{user['name']}_{secrets.token_hex(3)}"
        conn.execute("UPDATE users SET cn = ? WHERE id = ?", (new_cn, user_id))
        renewed = _helper("renew_cert", old_cn=user["cn"], new_cn=new_cn, days=s["cert_days"],
                          keep_disabled=not user["enabled"])
        conn.execute("UPDATE users SET cert_expires = ? WHERE id = ?", (renewed["not_after"], user_id))
        conn.execute("DELETE FROM shares WHERE user_id = ?", (user_id,))
        user = _get_user(conn, user_id)
        audit.record(conn, admin["username"], client_ip(request), "user.renew", user["name"])
        # a disabled user gets a renewed certificate but no link: enable the user first
        share = _share(conn, user, s) if user["enabled"] else None
        return {"user": _user_out(user, {}), "share": share}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, request: Request, admin=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        _helper("revoke_cert", cn=user["cn"])  # idempotent; if it fails the user stays and can be retried
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        audit.record(conn, admin["username"], client_ip(request), "user.delete", user["name"])
    return {"ok": True}


# ---------------------------------------------------------------- settings

class SettingsIn(BaseModel):
    endpoint_host: str = Field(min_length=1, max_length=253)
    public_port: int = Field(ge=1, le=65535)
    panel_url: str = Field(default="", max_length=300)
    dns: str = Field(default="", max_length=200)
    full_tunnel: bool
    routes: str = Field(default="", max_length=2000)
    client_to_client: bool
    cert_days: int = Field(ge=1, le=3650)
    share_hours: int = Field(ge=1, le=720)
    share_downloads: int = Field(ge=1, le=50)


@app.get("/api/settings")
def get_settings(_=Depends(current_admin)):
    with db.connect() as conn:
        values = settings.load(conn)
    return {"values": values, "server": {"port": CONFIG.port, "proto": CONFIG.proto, "subnet": CONFIG.subnet}}


@app.put("/api/settings")
def put_settings(body: SettingsIn, request: Request, admin=Depends(current_admin)):
    try:
        values = settings.validate_settings(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db.connect() as conn:
        old = settings.load(conn)
        settings.save(conn, values)
        new = settings.load(conn)
        changed = sorted(k for k in values if old.get(k) != new.get(k))
        restarted = settings.effective_server(old) != settings.effective_server(new)
        if restarted:
            # the helper rolls server.conf back if OpenVPN does not start; then the exception below
            # also discards the database change
            _helper("apply_server_conf", restart=True, **settings.server_params(new))
        audit.record(conn, admin["username"], client_ip(request), "settings.update", "",
                     ", ".join(f"{k}={new[k]}" for k in changed) or "no changes")
    forget_hosts()
    return {"values": new, "restarted": restarted}


# ---------------------------------------------------------------- public pages (no login)

@app.get("/")
def index():
    return _page("index")


@app.get("/panel-ca.crt")
def panel_ca():
    """The certificate (not the key) of the CA that signed the panel's HTTPS certificate."""
    try:
        with open(os.path.join(CONFIG.tls_dir, "ca.crt"), "rb") as f:
            pem = f.read()
    except OSError:
        raise HTTPException(404, "No panel CA on this installation")
    return Response(pem, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="ovpn-panel-ca.crt"'})


@app.get("/p/{token}")
def activation_page(token: str):
    """If the link is not valid, the page itself says so."""
    return _page("connect")


def _link_user(conn, token: str):
    return conn.execute(
        """SELECT u.id, u.name, u.cn, s.expires_at, s.uses_left, s.token_hash
           FROM shares s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ? AND s.uses_left > 0 AND u.enabled = 1""",
        (security.token_hash(token), time.time()),
    ).fetchone()


@app.get("/dl/{token}/info")
def shared_info(token: str):
    with db.connect() as conn:
        user = _link_user(conn, token)
    if user is None:
        raise HTTPException(404, "Link expired or invalid")
    return {"name": user["name"], "filename": f"{user['name']}.ovpn", "expires_at": user["expires_at"]}


@app.get("/dl/{token}")
def shared_download(token: str, request: Request):
    with db.connect() as conn:
        user = _link_user(conn, token)
        if user is not None:
            # counted before the profile is produced; if that fails the exception rolls the count back
            used = conn.execute("UPDATE shares SET uses_left = uses_left - 1 WHERE token_hash = ? AND uses_left > 0",
                                (user["token_hash"],))
            if used.rowcount != 1:
                user = None
        if user is None:
            return PlainTextResponse("This link has expired or is not valid. Ask your administrator for a new one.",
                                     status_code=404)
        content = _profile_text(conn, user)
        audit.record(conn, "link", client_ip(request), "share.download", user["name"])
    return PlainTextResponse(content, media_type="application/x-openvpn-profile", headers={
        "Content-Disposition": f'attachment; filename="{user["name"]}.ovpn"'})
