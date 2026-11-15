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
    expires_at  REAL NOT NULL,
    created_at  REAL NOT NULL DEFAULT 0,
    ip          TEXT
);
-- one VPN user = one client certificate
CREATE TABLE IF NOT EXISTS users(
    id           INTEGER PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    cn           TEXT UNIQUE NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    cert_expires TEXT
);
-- temporary links to the activation page (the profile is rebuilt from the PKI)
CREATE TABLE IF NOT EXISTS shares(
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  REAL NOT NULL,
    uses_left   INTEGER NOT NULL DEFAULT 5
);
CREATE TABLE IF NOT EXISTS settings(
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    ip      TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '',
    ok      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
-- failed logins; kept in the database so a restart does not reset the lockout
CREATE TABLE IF NOT EXISTS login_attempts(
    id   INTEGER PRIMARY KEY,
    key  TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_key ON login_attempts(key, ts);
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
        # without a commit, changes are discarded (implicit rollback)
        conn.close()


def _columns(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn):
    """Brings a database created by an older version up to date."""
    wanted = {
        "users": [("cert_expires", "TEXT")],
        "shares": [("uses_left", "INTEGER NOT NULL DEFAULT 5")],
        "tokens": [("created_at", "REAL NOT NULL DEFAULT 0"), ("ip", "TEXT")],
    }
    for table, columns in wanted.items():
        have = _columns(conn, table)
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init():
    os.makedirs(os.path.dirname(CONFIG.db_path), exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
