# SPDX-License-Identifier: GPL-2.0-or-later
"""Account groups: what the accounts in each group may do.

Every account belongs to one group. Three are built in:

  Admin       can do everything, including managing groups. Only whether it
              needs two-step verification can be changed.
  Super User  runs the place day to day: setup, rules, schedules, the activity
              log and the accounts in the User group. By default it can't
              manage admins or other super users, or change the site name or
              security settings, and doesn't need two-step verification.
  User        uses the controls.

Admins can change Super User and User, and add groups of their own (for
example "Viewers", who can see the controls but not switch anything).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from . import settings as runtime

ADMIN, SUPERUSER, USER = "admin", "superuser", "user"
BUILTIN_ORDER = (ADMIN, SUPERUSER, USER)
BUILTIN_NAMES = {ADMIN: "Admin", SUPERUSER: "Super User", USER: "User"}
NAME_MAX = 40


@dataclass(frozen=True)
class Privilege:
    key: str
    label: str
    help: str
    section: str


# What each settings section covers, for the privileges that unlock them.
SETTINGS_SECTIONS = {
    "General": ("Site name", "The name in the top bar, page titles and authenticator apps."),
    "Sign-in": ("Sign-in security", "Password length, the breached-password check and lockouts."),
    "Sessions": ("Sessions", "When people are signed out."),
    "Controls": ("Controls limit", "How many actions a person may take per minute."),
    "Rules": ("Rules limit", "How often a rule may act before it pauses."),
    "Records": ("Record keeping", "How long activity and sign-in history are kept."),
}
assert list(SETTINGS_SECTIONS) == runtime.GROUPS, "every settings section needs a privilege"

PRIVILEGES: list[Privilege] = [
    Privilege("control", "Use the controls",
              "Switch outputs on the Controls page, within their access schedule. Without "
              "this they can still see the controls, the activity panel and the pinout.",
              "Controls"),
    Privilege("io", "Inputs, outputs and sensors", "Add, change, reorder and remove them.",
              "Setup"),
    Privilege("rules", "Rules", "Add, change, pause and remove rules.", "Setup"),
    Privilege("schedules", "Schedules", "Add, change and remove schedules.", "Setup"),
    Privilege("activity", "Activity log",
              "See Admin > Activity, including sign-ins and IP addresses, and download it.",
              "Records"),
    Privilege("settings_view", "See all settings",
              "Open Admin > Settings. Sections they can't change are shown greyed out.",
              "Settings"),
    *[Privilege(f"settings:{name}", label, text, "Settings")
      for name, (label, text) in SETTINGS_SECTIONS.items()],
]
PRIVILEGE_KEYS = [p.key for p in PRIVILEGES]
SECTIONS = ["Controls", "Setup", "Records", "Settings"]

DEFAULT_PRIVILEGES = {
    SUPERUSER: ["control", "io", "rules", "schedules", "activity", "settings_view",
                "settings:Controls", "settings:Rules"],
    USER: ["control"],
}


def clean_privileges(keys) -> list[str]:
    """Known privileges only, in a fixed order. Changing a settings section
    implies seeing the settings."""
    wanted = set(keys)
    if any(k.startswith("settings:") for k in wanted):
        wanted.add("settings_view")
    return [k for k in PRIVILEGE_KEYS if k in wanted]


# ---------------------------------------------------------------- what an account may do

@dataclass(frozen=True)
class Access:
    group_id: int
    group_name: str
    builtin: str | None
    require_2fa: bool
    privileges: frozenset
    targets: frozenset            # ids of the groups whose accounts they manage

    @property
    def is_admin(self) -> bool:
        return self.builtin == ADMIN

    def can(self, key: str) -> bool:
        if self.is_admin:
            return True
        if key == "accounts":
            return bool(self.targets)
        return key in self.privileges          # "groups" is never granted

    def can_manage(self, group_id: int | None) -> bool:
        """May they add, change and remove accounts in this group?"""
        return self.is_admin or (group_id is not None and group_id in self.targets)

    def can_change_settings(self, section: str) -> bool:
        return self.can(f"settings:{section}")

    @property
    def admin_area(self) -> bool:
        return any(self.can(k) for k in ADMIN_TABS)


# The Admin area's tabs, in order, with the privilege each needs.
ADMIN_TABS = {
    "io": "main.admin_io",
    "rules": "main.admin_rules",
    "schedules": "main.admin_schedules",
    "accounts": "main.admin_users",
    "groups": "main.admin_groups",
    "settings_view": "main.admin_settings",
    "activity": "main.admin_activity",
}

NO_ACCESS = Access(0, "", None, True, frozenset(), frozenset())


@dataclass(frozen=True)
class Group:
    id: int
    name: str
    builtin: str | None
    require_2fa: bool
    privileges: frozenset
    targets: frozenset
    members: int = 0

    @property
    def is_admin(self) -> bool:
        return self.builtin == ADMIN

    def access(self) -> Access:
        return Access(self.id, self.name, self.builtin, self.require_2fa,
                      self.privileges, self.targets)


def _privileges(text: str) -> frozenset:
    try:
        return frozenset(clean_privileges(json.loads(text or "[]")))
    except (ValueError, TypeError):
        return frozenset()


def load_all(conn: sqlite3.Connection) -> dict[int, Group]:
    """Every group: the built-in ones first, then the rest by name."""
    targets: dict[int, set] = {}
    for r in conn.execute("SELECT group_id, target_id FROM group_manages"):
        targets.setdefault(r[0], set()).add(r[1])
    members = dict(conn.execute(
        "SELECT group_id, COUNT(*) FROM users GROUP BY group_id").fetchall())
    rows = conn.execute("SELECT * FROM user_groups").fetchall()

    def order(r):
        b = r["builtin"]
        return (BUILTIN_ORDER.index(b) if b in BUILTIN_ORDER else 9, r["name"].lower())

    return {r["id"]: Group(r["id"], r["name"], r["builtin"], bool(r["require_2fa"]),
                           _privileges(r["privileges"]), frozenset(targets.get(r["id"], ())),
                           members.get(r["id"], 0))
            for r in sorted(rows, key=order)}


def access_for(conn: sqlite3.Connection, group_id: int | None) -> Access:
    row = conn.execute("SELECT * FROM user_groups WHERE id = ?", (group_id,)).fetchone() \
        if group_id is not None else None
    if row is None:
        return NO_ACCESS
    targets = frozenset(r[0] for r in conn.execute(
        "SELECT target_id FROM group_manages WHERE group_id = ?", (row["id"],)))
    return Access(row["id"], row["name"], row["builtin"], bool(row["require_2fa"]),
                  _privileges(row["privileges"]), targets)


def builtin_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT builtin, id FROM user_groups WHERE builtin IS NOT NULL")}


def ensure_builtin(conn: sqlite3.Connection) -> dict[str, int]:
    """Create any missing built-in group. The first time, Admin and User take
    the site-wide two-step setting from earlier versions; Super User doesn't
    need two-step verification."""
    ids = builtin_ids(conn)
    missing = [b for b in BUILTIN_ORDER if b not in ids]
    if not missing:
        return ids
    legacy = conn.execute("SELECT value FROM settings WHERE key = 'require_2fa'").fetchone()
    try:
        required = bool(json.loads(legacy[0])) if legacy else True
    except ValueError:
        required = True
    now = int(time.time())
    for b in missing:
        name = BUILTIN_NAMES[b]
        # A group someone made earlier with the same name keeps it.
        if conn.execute("SELECT 1 FROM user_groups WHERE name = ?", (name,)).fetchone():
            name = f"{name} (built in)"
        cur = conn.execute(
            "INSERT INTO user_groups (name, builtin, require_2fa, privileges, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, b, int(required and b != SUPERUSER),
             json.dumps(DEFAULT_PRIVILEGES.get(b, [])), now, now))
        ids[b] = cur.lastrowid
    if SUPERUSER in missing:
        conn.execute("INSERT OR IGNORE INTO group_manages (group_id, target_id) VALUES (?, ?)",
                     (ids[SUPERUSER], ids[USER]))
    conn.execute("DELETE FROM settings WHERE key = 'require_2fa'")   # now set per group
    conn.commit()
    return ids


def save(conn: sqlite3.Connection, group_id: int | None, *, name: str, require_2fa: bool,
         privileges, targets) -> int:
    """Create or update a group. The Admin group's name and privileges are
    fixed, and only Admin may manage admin accounts."""
    now = int(time.time())
    groups = load_all(conn)
    admin_id = next(g.id for g in groups.values() if g.is_admin)
    existing = groups.get(group_id) if group_id else None
    privs = json.dumps(clean_privileges(privileges))
    targets = {t for t in targets if t in groups and t != admin_id}
    if existing is not None and existing.is_admin:
        conn.execute("UPDATE user_groups SET require_2fa = ?, updated_at = ? WHERE id = ?",
                     (int(require_2fa), now, group_id))
        conn.commit()
        return group_id
    if existing is not None:
        if existing.builtin:
            name = existing.name
        conn.execute("UPDATE user_groups SET name = ?, require_2fa = ?, privileges = ?,"
                     " updated_at = ? WHERE id = ?",
                     (name, int(require_2fa), privs, now, group_id))
    else:
        group_id = conn.execute(
            "INSERT INTO user_groups (name, require_2fa, privileges, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)", (name, int(require_2fa), privs, now, now)).lastrowid
    conn.execute("DELETE FROM group_manages WHERE group_id = ?", (group_id,))
    conn.executemany("INSERT INTO group_manages (group_id, target_id) VALUES (?, ?)",
                     [(group_id, t) for t in sorted(targets)])
    conn.commit()
    return group_id


def summary(group: Group, groups: dict[int, Group]) -> list[str]:
    """Short phrases describing what a group can do, for the groups list."""
    if group.is_admin:
        return ["Everything, including groups"]
    a = group.access()
    parts = []
    parts.append("Uses the controls" if a.can("control") else "Sees the controls")
    setup = [p.label.lower() for p in PRIVILEGES if p.section == "Setup" and a.can(p.key)]
    if setup:
        parts.append("Manages " + _join(setup))
    if group.targets:
        names = [groups[t].name for t in groups if t in group.targets]
        parts.append("Manages accounts in " + _join(names))
    if a.can("activity"):
        parts.append("Sees the activity log")
    if a.can("settings_view"):
        changeable = [SETTINGS_SECTIONS[s][0].lower() for s in SETTINGS_SECTIONS
                      if a.can_change_settings(s)]
        parts.append("Sees settings" + (", changes " + _join(changeable) if changeable else ""))
    return parts


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
