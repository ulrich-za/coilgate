# SPDX-License-Identifier: GPL-2.0-or-later
"""Activity log: outputs switched, input changes, configuration, accounts, settings."""
from __future__ import annotations

import sqlite3
import time

CATEGORIES = {
    "control": "Controls",
    "automation": "Rules",
    "rule": "Rule setup",
    "schedule": "Schedules",
    "input": "Inputs",
    "io": "Input and output setup",
    "user": "Accounts",
    "settings": "Settings",
    "export": "Downloads",
}

SIGNIN_EVENTS = {
    "login": "Sign-in", "mfa": "Verification code", "reauth": "Password confirmation",
    "logout": "Signed out", "login_throttled": "Blocked (too many attempts)",
    "password_changed": "Password changed", "totp_enabled": "Two-step turned on",
    "totp_disabled": "Two-step turned off", "temp_password_expired": "Expired temporary password",
}

ACTION_TEXT = {
    ("control", "on"): "Turned on",
    ("control", "off"): "Turned off",
    ("control", "pulse"): "Pulsed",
    ("control", "stop"): "Stopped pulse",
    ("input", "active"): "Changed",
    ("input", "inactive"): "Changed",
    ("io", "created"): "Added",
    ("io", "updated"): "Changed",
    ("io", "deleted"): "Removed",
    ("user", "created"): "Account created",
    ("user", "role"): "Role changed",
    ("user", "group"): "Group changed",
    ("user", "group_created"): "Group added",
    ("user", "group_updated"): "Group's privileges changed",
    ("user", "group_deleted"): "Group removed",
    ("user", "disabled"): "Account disabled",
    ("user", "enabled"): "Account enabled",
    ("user", "password_reset"): "Temporary password issued",
    ("user", "totp_reset"): "Two-step verification reset",
    ("user", "signed_out"): "Signed out everywhere",
    ("user", "deleted"): "Account deleted",
    ("settings", "updated"): "Settings changed",
    ("user", "schedule"): "Access schedule changed",
    ("automation", "on"): "Turned on",
    ("automation", "off"): "Turned off",
    ("automation", "pulse"): "Pulsed",
    ("automation", "rule_on"): "Rule switched on",
    ("automation", "rule_off"): "Rule switched off",
    ("automation", "failed"): "Couldn't run",
    ("automation", "paused"): "Paused for a minute (acted too often)",
    ("automation", "resumed"): "Resumed",
    ("rule", "created"): "Rule added",
    ("rule", "updated"): "Rule changed",
    ("rule", "deleted"): "Rule removed",
    ("rule", "enabled"): "Rule switched on",
    ("rule", "disabled"): "Rule switched off",
    ("rule", "resumed"): "Rule resumed early",
    ("schedule", "created"): "Schedule added",
    ("schedule", "updated"): "Schedule changed",
    ("schedule", "deleted"): "Schedule removed",
    ("export", "csv"): "Downloaded as CSV",
    ("export", "pdf"): "Downloaded as PDF",
}


def log(conn: sqlite3.Connection, category: str, action: str, *, target: str | None = None,
        detail: str | None = None, io_id: int | None = None, user_id: int | None = None,
        username: str | None = None, ip: str | None = None) -> None:
    conn.execute(
        "INSERT INTO activity (ts, category, action, target, detail, io_id, user_id, username, ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), category, action, target, (detail or "")[:300] or None,
         io_id, user_id, username, ip),
    )
    conn.commit()


def describe(row) -> str:
    return ACTION_TEXT.get((row["category"], row["action"]), row["action"].replace("_", " ").capitalize())


def changed_to(row) -> str:
    """The "Change" text: an input's new state, otherwise what was done."""
    return (row["detail"] or "") if row["category"] == "input" else describe(row)


def done_by(row) -> str:
    """Who or what did it: a person, a rule, or the input itself."""
    if row["category"] == "automation":
        return row["detail"] or "Rule"
    return row["username"] or ("Input" if row["category"] == "input" else "")


def extra_detail(row) -> str:
    """Detail not already shown as the change or as who did it."""
    if row["category"] in ("input", "automation", "control"):
        return ""
    return row["detail"] or ""


def describe_device(ua: str | None) -> str:
    ua = ua or ""
    browser = next((b for b in ("Edg", "Firefox", "Chrome", "Safari") if b in ua), None)
    browser = {"Edg": "Edge"}.get(browser, browser) or "Unknown browser"
    system = next((name for key, name in (
        ("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"),
        ("iPad", "iPad"), ("Mac OS", "macOS"), ("Linux", "Linux"),
    ) if key in ua), "unknown system")
    return f"{browser} on {system}"


def recent(conn: sqlite3.Connection, categories: tuple[str, ...] | None = None,
           limit: int = 20) -> list[sqlite3.Row]:
    if categories:
        marks = ",".join("?" * len(categories))
        return conn.execute(
            f"SELECT * FROM activity WHERE category IN ({marks}) ORDER BY ts DESC, id DESC LIMIT ?",
            (*categories, limit)).fetchall()
    return conn.execute("SELECT * FROM activity ORDER BY ts DESC, id DESC LIMIT ?",
                        (limit,)).fetchall()


def control_actions_last_minute(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM activity WHERE category = 'control' AND user_id = ? AND ts > ?",
        (user_id, int(time.time()) - 60)).fetchone()[0]
