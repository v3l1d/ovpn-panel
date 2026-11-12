"""CLI di amministrazione: ovpn-panel <comando>"""
import argparse
import getpass
import os
import re
import sqlite3
import sys

from . import db, security, serverconf, settings


def cmd_init(_args):
    """Crea il database e scrive server.conf con le impostazioni correnti."""
    db.init()
    with db.connect() as conn:
        serverconf.write(settings.load(conn))
    print("Database inizializzato, server.conf scritto.")


def _read_password():
    # OVPN_PANEL_PASSWORD permette l'uso non interattivo (installer)
    pw = os.environ.get("OVPN_PANEL_PASSWORD")
    if pw is None:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Ripeti password: "):
            sys.exit("Le password non coincidono")
    if len(pw) < 10:
        sys.exit("La password deve avere almeno 10 caratteri")
    return pw


def cmd_create_admin(args):
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{1,31}$", args.username):
        sys.exit("Username non valido (minuscole, cifre, _ . -)")
    pw = _read_password()
    db.init()
    with db.connect() as conn:
        try:
            conn.execute("INSERT INTO admins(username, pw_hash, created_at) VALUES (?, ?, ?)",
                         (args.username, security.hash_password(pw), db.now_iso()))
        except sqlite3.IntegrityError:
            sys.exit("Amministratore gia' esistente (usa reset-password)")
    print(f"Amministratore '{args.username}' creato.")


def cmd_reset_password(args):
    pw = _read_password()
    with db.connect() as conn:
        cur = conn.execute("UPDATE admins SET pw_hash = ? WHERE username = ?",
                           (security.hash_password(pw), args.username))
        if cur.rowcount == 0:
            sys.exit("Amministratore non trovato")
        conn.execute("DELETE FROM tokens WHERE admin_id = (SELECT id FROM admins WHERE username = ?)",
                     (args.username,))
    print("Password aggiornata.")


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
