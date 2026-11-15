"""Admin CLI that only touches the panel database. Runs as the panel user, through `ovpn-panel`:

    ovpn-panel create-admin NAME     ovpn-panel reset-password NAME
    ovpn-panel unlock                ovpn-panel audit [N]
"""
import argparse
import getpass
import os
import sqlite3
import sys
import time

from . import audit, db, ratelimit, security, validate


def _read_password():
    # OVPN_PANEL_PASSWORD allows non-interactive use (installer)
    pw = os.environ.get("OVPN_PANEL_PASSWORD")
    if pw is None:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match")
    if len(pw) < 10:
        sys.exit("The password must be at least 10 characters long")
    return pw


def cmd_create_admin(args):
    try:
        validate.name(args.username)
    except validate.ValidationError:
        sys.exit("Invalid username (lowercase letters, digits, _ . -)")
    pw = _read_password()
    db.init()
    with db.connect() as conn:
        try:
            conn.execute("INSERT INTO admins(username, pw_hash, created_at) VALUES (?, ?, ?)",
                         (args.username, security.hash_password(pw), db.now_iso()))
        except sqlite3.IntegrityError:
            sys.exit("Admin already exists (use reset-password)")
        audit.record(conn, "cli", "local", "admin.create", args.username)
    print(f"Admin '{args.username}' created.")


def cmd_reset_password(args):
    pw = _read_password()
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM admins WHERE username = ?", (args.username,)).fetchone()
        if row is None:
            sys.exit("Admin not found")
        conn.execute("UPDATE admins SET pw_hash = ? WHERE id = ?", (security.hash_password(pw), row["id"]))
        conn.execute("DELETE FROM tokens WHERE admin_id = ?", (row["id"],))
        audit.record(conn, "cli", "local", "password.reset", args.username)
    print("Password updated; every session of that admin was signed out.")


def cmd_unlock(_args):
    """Clears the failed-login lockouts (e.g. after locking yourself out)."""
    with db.connect() as conn:
        ratelimit.clear_all(conn)
        audit.record(conn, "cli", "local", "login.unlock")
    print("Login lockouts cleared.")


def cmd_audit(args):
    with db.connect() as conn:
        for e in reversed(audit.entries(conn, args.count)):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
            print(f"{when}  {e['actor']:<14} {e['ip']:<16} {e['action']:<18} {e['target']:<18} "
                  f"{'ok' if e['ok'] else 'FAILED'}  {e['detail']}")


def main():
    p = argparse.ArgumentParser(prog="ovpn-panel")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("create-admin", cmd_create_admin), ("reset-password", cmd_reset_password)):
        a = sub.add_parser(name)
        a.add_argument("username")
        a.set_defaults(fn=fn)
    sub.add_parser("unlock").set_defaults(fn=cmd_unlock)
    a = sub.add_parser("audit")
    a.add_argument("count", nargs="?", type=int, default=50)
    a.set_defaults(fn=cmd_audit)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
