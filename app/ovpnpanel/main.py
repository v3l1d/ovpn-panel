import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from typing import Optional

import segno
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, mgmt, pki, security, serverconf, settings
from .config import CONFIG

log = logging.getLogger("ovpn-panel")

# the name ends up in the certificate CN and in the .ovpn file name
NAME_RE = r"^[a-z0-9][a-z0-9_.-]{1,31}$"
_static = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="ovpn-panel", docs_url=None, redoc_url=None)
bearer = HTTPBearer(auto_error=False)
throttle = security.LoginThrottle()
# serializes PKI changes, server.conf writes and restarts
pki_lock = threading.Lock()


@app.on_event("startup")
def _startup():
    db.init()


def _pki_error(e: Exception):
    if isinstance(e, subprocess.CalledProcessError):
        log.error("command failed %s: %s", e.cmd, e.stderr)
    else:
        log.exception("PKI/OpenVPN error")
    raise HTTPException(500, "The operation failed on the OpenVPN server (see journalctl -u ovpn-panel)")


def _clients() -> dict:
    try:
        return mgmt.clients()
    except mgmt.MgmtError:
        return {}


# ---------------------------------------------------------------- authentication

def current_admin(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
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
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    if throttle.blocked(ip):
        raise HTTPException(429, "Too many attempts, try again in a few minutes")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM admins WHERE username = ?", (body.username.strip().lower(),)).fetchone()
        if row is None or not security.verify_password(body.password, row["pw_hash"]):
            throttle.fail(ip)
            raise HTTPException(401, "Invalid credentials")
        throttle.reset(ip)
        token = security.new_token()
        conn.execute("DELETE FROM tokens WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT INTO tokens(token_hash, admin_id, expires_at) VALUES (?, ?, ?)",
            (security.token_hash(token), row["id"], time.time() + CONFIG.token_ttl_hours * 3600),
        )
    return {"token": token, "username": row["username"]}


@app.post("/api/logout")
def logout(creds: HTTPAuthorizationCredentials = Depends(bearer), _=Depends(current_admin)):
    with db.connect() as conn:
        conn.execute("DELETE FROM tokens WHERE token_hash = ?", (security.token_hash(creds.credentials),))
    return {"ok": True}


@app.get("/api/me")
def me(admin=Depends(current_admin)):
    return {"username": admin["username"]}


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=10)


@app.post("/api/me/password")
def change_password(body: PasswordIn, creds: HTTPAuthorizationCredentials = Depends(bearer),
                    admin=Depends(current_admin)):
    if not security.verify_password(body.current, admin["pw_hash"]):
        raise HTTPException(400, "The current password is incorrect")
    with db.connect() as conn:
        conn.execute("UPDATE admins SET pw_hash = ? WHERE id = ?", (security.hash_password(body.new), admin["id"]))
        # sign out the other sessions, not the current one
        conn.execute("DELETE FROM tokens WHERE admin_id = ? AND token_hash != ?",
                     (admin["id"], security.token_hash(creds.credentials)))
    return {"ok": True}


# ---------------------------------------------------------------- server

@app.get("/api/overview")
def overview(_=Depends(current_admin)):
    server = {"running": False, "port": CONFIG.port, "proto": CONFIG.proto, "subnet": CONFIG.subnet}
    try:
        server.update(mgmt.state())
        server.update(mgmt.stats())
        server["version"] = mgmt.version()
        server["running"] = server.get("state") == "CONNECTED"
    except mgmt.MgmtError as e:
        server["error"] = str(e)
    with db.connect() as conn:
        users = conn.execute("SELECT COUNT(*) AS n, SUM(enabled) AS enabled FROM users").fetchone()
        s = settings.load(conn)
    return {"server": server, "users": users["n"], "users_enabled": users["enabled"] or 0,
            "endpoint": f"{s['endpoint_host']}:{s['public_port']}"}


def _journal_log(lines: int):
    """OpenVPN log from the journal (full history, unlike the management buffer)."""
    try:
        r = subprocess.run(
            # generous margin: MANAGEMENT lines are dropped after reading
            ["journalctl", "-u", "openvpn-server@server", "-n", str(min(lines * 20, 20000)),
             "-o", "json", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = []
    for raw in r.stdout.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, str) and not msg.startswith("MANAGEMENT:"):
            out.append({"time": int(e["__REALTIME_TIMESTAMP"]) // 1_000_000,
                        "flags": "N" if int(e.get("PRIORITY", 6)) <= 4 else "I", "msg": msg})
    return out[-lines:]


@app.get("/api/server/log")
def server_log(lines: int = 150, _=Depends(current_admin)):
    lines = min(max(lines, 1), 1000)
    entries = _journal_log(lines)
    if entries is not None:
        return entries
    try:
        # without journald (e.g. installed outside systemd): management buffer
        return [e for e in mgmt.log(lines) if not e["msg"].startswith("MANAGEMENT:")]
    except mgmt.MgmtError as e:
        raise HTTPException(503, str(e))


@app.post("/api/server/restart")
def server_restart(_=Depends(current_admin)):
    with pki_lock:
        try:
            serverconf.restart()
        except Exception as e:
            _pki_error(e)
    return {"ok": True}


# ---------------------------------------------------------------- VPN users

def _user_out(row, clients) -> dict:
    d = {k: row[k] for k in ("id", "name", "note", "created_at")}
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


def _share(conn, user, request) -> dict:
    token, expires = security.new_token(), time.time() + CONFIG.share_ttl_hours * 3600
    conn.execute("DELETE FROM shares WHERE expires_at < ?", (time.time(),))
    conn.execute("INSERT INTO shares(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                 (security.token_hash(token), user["id"], expires))
    base = settings.load(conn)["panel_url"] or str(request.base_url).rstrip("/")
    url = f"{base}/p/{token}"
    return {"url": url, "expires_at": expires,
            "qr": segno.make(url, error="m").svg_data_uri(scale=5, border=2, light="#fff")}


@app.get("/api/users")
def list_users(_=Depends(current_admin)):
    clients = _clients()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY name").fetchall()
    return [_user_out(r, clients) for r in rows]


class UserIn(BaseModel):
    name: str = Field(pattern=NAME_RE)
    note: str = Field(default="", max_length=120)


@app.post("/api/users")
def create_user(body: UserIn, request: Request, _=Depends(current_admin)):
    cn = pki.new_cn(body.name)
    with pki_lock, db.connect() as conn:
        try:
            cur = conn.execute("INSERT INTO users(name, cn, note, created_at) VALUES (?, ?, ?, ?)",
                               (body.name, cn, body.note.strip(), db.now_iso()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A user with this name already exists")
        try:
            pki.issue(cn)
        except Exception as e:
            _pki_error(e)
        user = _get_user(conn, cur.lastrowid)
        return {"user": _user_out(user, {}), "share": _share(conn, user, request)}


@app.post("/api/users/{user_id}/share")
def share_user(user_id: int, request: Request, _=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        if not user["enabled"]:
            raise HTTPException(409, "User is disabled: enable it before sharing the profile")
        return {"user": _user_out(user, _clients()), "share": _share(conn, user, request)}


@app.get("/api/users/{user_id}/profile")
def user_profile(user_id: int, _=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
        content = pki.profile(user["cn"], settings.load(conn))
    return PlainTextResponse(content, media_type="application/x-openvpn-profile", headers={
        "Content-Disposition": f'attachment; filename="{user["name"]}.ovpn"', "Cache-Control": "no-store"})


class UserPatch(BaseModel):
    enabled: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=120)


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UserPatch, _=Depends(current_admin)):
    with pki_lock, db.connect() as conn:
        user = _get_user(conn, user_id)
        if body.note is not None:
            conn.execute("UPDATE users SET note = ? WHERE id = ?", (body.note.strip(), user_id))
        if body.enabled is not None:
            conn.execute("UPDATE users SET enabled = ? WHERE id = ?", (int(body.enabled), user_id))
            try:
                pki.set_disabled(user["cn"], not body.enabled)
                if not body.enabled:
                    conn.execute("DELETE FROM shares WHERE user_id = ?", (user_id,))
                    mgmt.kill(user["cn"])
            except mgmt.MgmtError:
                pass  # OpenVPN is down: the block still applies on the next connection
            except Exception as e:
                _pki_error(e)
    return {"ok": True}


@app.post("/api/users/{user_id}/kick")
def kick_user(user_id: int, _=Depends(current_admin)):
    with db.connect() as conn:
        user = _get_user(conn, user_id)
    try:
        mgmt.kill(user["cn"])
    except mgmt.MgmtError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@app.post("/api/users/{user_id}/renew")
def renew_user(user_id: int, request: Request, _=Depends(current_admin)):
    """Revokes the certificate and issues a new one (e.g. lost phone)."""
    with pki_lock, db.connect() as conn:
        user = _get_user(conn, user_id)
        cn = pki.new_cn(user["name"])
        try:
            pki.issue(cn)
            pki.revoke(user["cn"])
            mgmt.kill(user["cn"])
        except mgmt.MgmtError:
            pass
        except Exception as e:
            _pki_error(e)
        conn.execute("UPDATE users SET cn = ?, enabled = 1 WHERE id = ?", (cn, user_id))
        conn.execute("DELETE FROM shares WHERE user_id = ?", (user_id,))
        user = _get_user(conn, user_id)
        return {"user": _user_out(user, {}), "share": _share(conn, user, request)}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, _=Depends(current_admin)):
    with pki_lock, db.connect() as conn:
        user = _get_user(conn, user_id)
        try:
            pki.revoke(user["cn"])
            mgmt.kill(user["cn"])
        except mgmt.MgmtError:
            pass
        except Exception as e:
            _pki_error(e)
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True}


# ---------------------------------------------------------------- settings

class SettingsIn(BaseModel):
    endpoint_host: str = Field(min_length=1, max_length=253)
    public_port: int = Field(ge=1, le=65535)
    panel_url: str = ""
    dns: str = ""
    full_tunnel: bool
    routes: str = ""
    client_to_client: bool


@app.get("/api/settings")
def get_settings(_=Depends(current_admin)):
    with db.connect() as conn:
        values = settings.load(conn)
    return {"values": values, "server": {"port": CONFIG.port, "proto": CONFIG.proto, "subnet": CONFIG.subnet}}


@app.put("/api/settings")
def put_settings(body: SettingsIn, _=Depends(current_admin)):
    try:
        values = settings.validate(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))
    with pki_lock, db.connect() as conn:
        old = settings.load(conn)
        settings.save(conn, values)
        new = settings.load(conn)
        restarted = any(old[k] != new[k] for k in settings.SERVER_KEYS)
        if restarted:
            try:
                serverconf.write(new)
                serverconf.restart()
            except Exception as e:
                _pki_error(e)
    return {"values": new, "restarted": restarted}


# ---------------------------------------------------------------- public page for the end user

def _shared_user(token: str):
    with db.connect() as conn:
        return conn.execute(
            """SELECT u.*, s.expires_at FROM shares s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND u.enabled = 1""",
            (security.token_hash(token), time.time()),
        ).fetchone()


@app.get("/p/{token}")
def activation_page(token: str):
    """If the link is not valid, the page itself says so."""
    return FileResponse(os.path.join(_static, "connect.html"),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/dl/{token}/info")
def shared_info(token: str):
    user = _shared_user(token)
    if user is None:
        raise HTTPException(404, "Link expired or invalid")
    return {"name": user["name"], "filename": f"{user['name']}.ovpn", "expires_at": user["expires_at"]}


@app.get("/dl/{token}")
def shared_download(token: str):
    user = _shared_user(token)
    if user is None:
        return PlainTextResponse("This link has expired or is not valid. Ask your administrator for a new one.",
                                 status_code=404)
    with db.connect() as conn:
        content = pki.profile(user["cn"], settings.load(conn))
    return PlainTextResponse(content, media_type="application/x-openvpn-profile", headers={
        "Content-Disposition": f'attachment; filename="{user["name"]}.ovpn"', "Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=_static, html=True), name="static")
