#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""CoilGate command-line admin tool.

Day-to-day account management happens in the web Admin area. This tool is
for creating the first admin and for recovery when nobody can sign in.

  python manage.py create-user admin            (the first account is always an admin)
  python manage.py create-user anna --group "Super User"
  python manage.py set-password admin
  python manage.py set-group anna Admin
  python manage.py list-users
  python manage.py list-groups
  python manage.py unlock admin
  python manage.py reset-2fa admin
  python manage.py disable anna / enable anna
  python manage.py sign-out anna
  python manage.py delete-user anna
  python manage.py backup coilgate-backup.db
  python manage.py prune
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from datetime import datetime

from app import db as database
from app import groups
from app import passwords
from app import settings as runtime
from app.config import INSTANCE_DIR, ensure_private_dir, load_settings
from app.sessions import log_event


def open_db(cfg):
    ensure_private_dir(INSTANCE_DIR)
    database.adopt_gatehouse_database(cfg["database"])
    conn = database.connect(cfg["database"])
    database.init_schema(conn)
    return conn


def find_user(conn, username):
    name = passwords.normalize_username(username)
    row = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
    if row is None:
        sys.exit(f"No user called '{name}'.")
    return row


def prompt_new_password(conn, username: str) -> str:
    s = runtime.load(conn)
    print(f"Choose a password of at least {s['min_password_length']} characters.")
    while True:
        pw = getpass.getpass("New password: ")
        problems = passwords.policy_problems(pw, username, s["site_name"], s["min_password_length"])
        if not problems and s["check_pwned_passwords"]:
            count = passwords.pwned_count(pw)
            if count is None:
                print("  (Couldn't reach the breached-password service; skipping that check.)")
            elif count:
                problems.append(f"This password has appeared in {count:,} data breaches.")
        if problems:
            for p in problems:
                print(f"  - {p}")
            continue
        if getpass.getpass("Type it again: ") != pw:
            print("  - The passwords don't match.")
            continue
        return pw


def admin_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM users u JOIN user_groups g ON g.id = u.group_id"
        " WHERE g.builtin = 'admin' AND u.is_active = 1").fetchone()[0]


def find_group(conn, name: str) -> groups.Group:
    """A group by name (any case), or by the old role names admin and user."""
    all_groups = groups.load_all(conn)
    wanted = {"admin": "Admin", "user": "User"}.get(name.strip().lower(), name).strip().lower()
    for g in all_groups.values():
        if g.name.lower() == wanted or g.builtin == wanted:
            return g
    sys.exit(f"No group called '{name}'. Groups: " + ", ".join(g.name for g in all_groups.values()))


def is_admin(conn, user) -> bool:
    row = conn.execute("SELECT builtin FROM user_groups WHERE id = ?", (user["group_id"],)).fetchone()
    return bool(row and row[0] == groups.ADMIN)


def cmd_create_user(cfg, conn, args):
    name = passwords.normalize_username(args.username)
    if not passwords.USERNAME_RE.match(name):
        sys.exit("Usernames are 2-32 characters: lowercase letters, digits, '.', '_' or '-'.")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
        sys.exit(f"User '{name}' already exists. Use set-password to change the password.")
    first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    asked = find_group(conn, args.group or args.role or "User")
    group = find_group(conn, "Admin") if first else asked
    pw = prompt_new_password(conn, name)
    now = int(time.time())
    conn.execute(
        "INSERT INTO users (username, password_hash, group_id, created_at, password_changed_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, passwords.hash_password(pw), group.id, now, now))
    conn.commit()
    note = " (the first account is always an admin)" if first and not asked.is_admin else ""
    print(f"Created '{name}' in the {group.name} group{note}.")


def cmd_set_password(cfg, conn, args):
    user = find_user(conn, args.username)
    pw = prompt_new_password(conn, user["username"])
    conn.execute(
        "UPDATE users SET password_hash = ?, password_changed_at = ?, must_change_password = 0,"
        " temp_password_expires_at = NULL WHERE id = ?",
        (passwords.hash_password(pw), int(time.time()), user["id"]))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    conn.commit()
    log_event(conn, "admin_unlock", True, user_id=user["id"], username=user["username"])
    print(f"Password changed for '{user['username']}'. All their sessions were signed out.")


def cmd_set_group(cfg, conn, args):
    user = find_user(conn, args.username)
    group = find_group(conn, args.group)
    if is_admin(conn, user) and not group.is_admin and user["is_active"] and admin_count(conn) <= 1:
        sys.exit("That's the only admin. Make someone else an admin first.")
    conn.execute("UPDATE users SET group_id = ?, schedule_id = CASE WHEN ? THEN NULL"
                 " ELSE schedule_id END WHERE id = ?", (group.id, int(group.is_admin), user["id"]))
    conn.commit()
    print(f"'{user['username']}' is now in the {group.name} group.")


def cmd_list_users(cfg, conn, args):
    rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    if not rows:
        print("No users yet. Create the first admin with: python manage.py create-user <name>")
        return
    names = {g.id: g.name for g in groups.load_all(conn).values()}
    print(f"{'Username':<20} {'Group':<14} {'Active':<7} {'2FA':<5} {'Last sign-in':<17} From")
    for r in rows:
        last = datetime.fromtimestamp(r["last_login_at"]).strftime("%Y-%m-%d %H:%M") \
            if r["last_login_at"] else ("pending" if r["must_change_password"] else "never")
        print(f"{r['username']:<20} {names.get(r['group_id'], '?')[:14]:<14} "
              f"{'yes' if r['is_active'] else 'no':<7} "
              f"{'on' if r['totp_enabled'] else 'off':<5} {last:<17} {r['last_login_ip'] or ''}")


def cmd_list_groups(cfg, conn, args):
    all_groups = groups.load_all(conn)
    for g in all_groups.values():
        print(f"{g.name} ({g.members} account{'s' if g.members != 1 else ''}, two-step "
              f"{'required' if g.require_2fa else 'optional'})")
        for line in groups.summary(g, all_groups):
            print(f"  - {line}")


def cmd_unlock(cfg, conn, args):
    user = find_user(conn, args.username)
    log_event(conn, "admin_unlock", True, user_id=user["id"], username=user["username"])
    print(f"Cleared the failed-attempt lock for '{user['username']}'.")
    print("Note: a per-IP lock (from many failures on one address) expires on its own.")


def cmd_reset_2fa(cfg, conn, args):
    user = find_user(conn, args.username)
    conn.execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL WHERE id = ?",
                 (user["id"],))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    conn.commit()
    print(f"Two-step verification turned off for '{user['username']}'. "
          "They'll set it up again at their next sign-in.")


def cmd_set_active(active: bool):
    def run(cfg, conn, args):
        user = find_user(conn, args.username)
        if not active and is_admin(conn, user) and admin_count(conn) <= 1:
            sys.exit("That's the only admin. Make someone else an admin first.")
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(active), user["id"]))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        conn.commit()
        print(f"'{user['username']}' is now {'enabled' if active else 'disabled'}.")
    return run


def cmd_sign_out(cfg, conn, args):
    user = find_user(conn, args.username)
    n = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],)).rowcount
    conn.commit()
    print(f"Signed out {n} session(s) for '{user['username']}'.")


def cmd_delete_user(cfg, conn, args):
    user = find_user(conn, args.username)
    if is_admin(conn, user) and user["is_active"] and admin_count(conn) <= 1:
        sys.exit("That's the only admin. Make someone else an admin first.")
    answer = input(f"Delete '{user['username']}' permanently? Type the username to confirm: ")
    if passwords.normalize_username(answer) != user["username"]:
        sys.exit("Not deleted.")
    conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    conn.commit()
    print(f"Deleted '{user['username']}'.")


def cmd_backup(cfg, conn, args):
    database.backup(conn, args.destination)
    print(f"Backed up to {args.destination}")


def cmd_prune(cfg, conn, args):
    s, e = database.prune(conn, runtime.load(conn)["event_retention_days"])
    conn.execute("VACUUM")
    print(f"Removed {s} expired session(s) and {e} old history entries.")


def main():
    parser = argparse.ArgumentParser(description="CoilGate admin tool")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, *, user=True):
        p = sub.add_parser(name, help=help_text)
        if user:
            p.add_argument("username")
        p.set_defaults(func=func)
        return p

    create = add("create-user", cmd_create_user, "create an account")
    create.add_argument("--group", help='group name, e.g. "Super User" (default: User)')
    create.add_argument("--role", choices=("admin", "user"), help=argparse.SUPPRESS)  # before groups
    add("set-password", cmd_set_password, "set a new password (signs out all sessions)")
    add("set-group", cmd_set_group, "move an account to another group").add_argument("group")
    add("set-role", cmd_set_group, "older name for set-group: admin or user").add_argument(
        "group", choices=("admin", "user"))
    add("list-users", cmd_list_users, "list accounts", user=False)
    add("list-groups", cmd_list_groups, "list groups and what they can do", user=False)
    add("unlock", cmd_unlock, "clear a failed-attempt lock")
    add("reset-2fa", cmd_reset_2fa, "turn off two-step verification (lost phone)")
    add("disable", cmd_set_active(False), "block an account from signing in")
    add("enable", cmd_set_active(True), "allow a disabled account again")
    add("sign-out", cmd_sign_out, "end all of a user's sessions")
    add("delete-user", cmd_delete_user, "delete an account")
    add("backup", cmd_backup, "copy the database safely, even while running",
        user=False).add_argument("destination")
    add("prune", cmd_prune, "remove expired sessions and old history", user=False)
    sub.add_parser("init-db", help="create the database file").set_defaults(
        func=lambda cfg, conn, args: print(f"Database ready at {cfg['database']}"))

    args = parser.parse_args()
    cfg = load_settings()
    conn = open_db(cfg)
    try:
        args.func(cfg, conn, args)
    except KeyboardInterrupt:
        print()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
