"""Login rate limiting, persisted in the database.

An IP is blocked after 5 failures in 10 minutes. A user name is blocked after 20 failures in the
same window, whatever the source, which slows down distributed guessing without letting a single
client lock the admin out.
"""
import time

WINDOW = 600
IP_LIMIT = 5
USER_LIMIT = 20


def _keys(ip: str, username: str):
    return f"ip:{ip}", f"user:{username}"


def blocked(conn, ip: str, username: str) -> bool:
    conn.execute("DELETE FROM login_attempts WHERE ts < ?", (time.time() - WINDOW,))
    ip_key, user_key = _keys(ip, username)
    count = lambda key: conn.execute("SELECT COUNT(*) FROM login_attempts WHERE key = ?", (key,)).fetchone()[0]
    return count(ip_key) >= IP_LIMIT or count(user_key) >= USER_LIMIT


def record_failure(conn, ip: str, username: str):
    now = time.time()
    for key in _keys(ip, username):
        conn.execute("INSERT INTO login_attempts(key, ts) VALUES (?, ?)", (key, now))


def clear(conn, ip: str, username: str):
    conn.execute("DELETE FROM login_attempts WHERE key IN (?, ?)", _keys(ip, username))


def clear_all(conn):
    conn.execute("DELETE FROM login_attempts")
