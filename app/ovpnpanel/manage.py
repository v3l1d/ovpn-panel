"""Admin CLI: ovpn-panel <command>"""
import argparse
import getpass
import os
import re
import sqlite3
import sys

from . import db, security, serverconf, settings


def cmd_init(_args):
    """Creates the database and writes server.conf from the current settings."""
    db.init()
    with db.connect() as conn:
        serverconf.write(settings.load(conn))
    print("Database initialized, server.conf written.")


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
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{1,31}$", args.username):
        sys.exit("Invalid username (lowercase letters, digits, _ . -)")
    pw = _read_password()
    db.init()
    with db.connect() as conn:
        try:
            conn.execute("INSERT INTO admins(username, pw_hash, created_at) VALUES (?, ?, ?)",
                         (args.username, security.hash_password(pw), db.now_iso()))
        except sqlite3.IntegrityError:
            sys.exit("Admin already exists (use reset-password)")
    print(f"Admin '{args.username}' created.")


def cmd_reset_password(args):
    pw = _read_password()
    with db.connect() as conn:
        cur = conn.execute("UPDATE admins SET pw_hash = ? WHERE username = ?",
                           (security.hash_password(pw), args.username))
        if cur.rowcount == 0:
            sys.exit("Admin not found")
        conn.execute("DELETE FROM tokens WHERE admin_id = (SELECT id FROM admins WHERE username = ?)",
                     (args.username,))
    print("Password updated.")


def main():
    p = argparse.ArgumentParser(prog="ovpn-panel")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    for name, fn in (("create-admin", cmd_create_admin), ("reset-password", cmd_reset_password)):
        a = sub.add_parser(name)
        a.add_argument("username")
        a.set_defaults(fn=fn)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
