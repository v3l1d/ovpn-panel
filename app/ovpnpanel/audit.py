"""Audit log: who did what, from where. Kept in the database and mirrored to the journal."""
import json
import logging
import re
import time

from .config import CONFIG

log = logging.getLogger("ovpn-panel.audit")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value, limit: int) -> str:
    return _CONTROL.sub(" ", str(value))[:limit]


def record(conn, actor: str, ip: str, action: str, target: str = "", detail: str = "", ok: bool = True):
    row = {"actor": _clean(actor, 64), "ip": _clean(ip, 64), "action": _clean(action, 64),
           "target": _clean(target, 128), "detail": _clean(detail, 512), "ok": bool(ok)}
    conn.execute(
        "INSERT INTO audit(ts, actor, ip, action, target, detail, ok) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), row["actor"], row["ip"], row["action"], row["target"], row["detail"], int(row["ok"])),
    )
    log.info(json.dumps(row, sort_keys=True))


def prune(conn):
    conn.execute("DELETE FROM audit WHERE ts < ?", (time.time() - CONFIG.audit_retention_days * 86400,))


def entries(conn, limit: int = 100, before: int = 0) -> list:
    limit = min(max(int(limit), 1), 500)
    rows = conn.execute(
        "SELECT * FROM audit WHERE (? = 0 OR id < ?) ORDER BY id DESC LIMIT ?", (before, before, limit)
    ).fetchall()
    return [dict(r) for r in rows]
