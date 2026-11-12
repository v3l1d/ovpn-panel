import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import CONFIG

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins(
    id          INTEGER PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    pw_hash     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens(
    token_hash  TEXT PRIMARY KEY,
    admin_id    INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    expires_at  REAL NOT NULL
);
-- un utente VPN = un certificato client
CREATE TABLE IF NOT EXISTS users(
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    cn          TEXT UNIQUE NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
-- link temporanei alla pagina di attivazione (il profilo si rigenera dalla PKI)
CREATE TABLE IF NOT EXISTS shares(
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(CONFIG.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        # senza commit le modifiche vengono scartate (rollback implicito)
        conn.close()


def init():
    os.makedirs(os.path.dirname(CONFIG.db_path), exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
