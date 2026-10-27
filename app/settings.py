# SPDX-License-Identifier: GPL-2.0-or-later
"""Runtime settings: stored in the database and edited on the Admin > Settings page."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    default: object
    kind: str            # "int", "bool" or "str"
    group: str
    label: str
    help: str = ""
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ""


SPECS: dict[str, Spec] = {
    "site_name": Spec(
        "CoilGate", "str", "General", "Site name",
        "Shown in the top bar, page titles and authenticator apps.", 1, 40),

    "min_password_length": Spec(
        15, "int", "Sign-in", "Minimum password length",
        "NIST recommends 15 when a password is the only factor, 8 with two-step verification.",
        8, 64, "characters"),
    "check_pwned_passwords": Spec(
        True, "bool", "Sign-in", "Reject passwords found in data breaches",
        "Uses Have I Been Pwned. Only 5 characters of the password's hash leave the Pi."),
    "max_failures_per_user": Spec(
        5, "int", "Sign-in", "Failed attempts before an account is locked",
        "Per username, within the window below.", 3, 50, "attempts"),
    "max_failures_per_ip": Spec(
        20, "int", "Sign-in", "Failed attempts before an IP address is locked",
        "Across all usernames, within the window below.", 5, 500, "attempts"),
    "failure_window_minutes": Spec(
        15, "int", "Sign-in", "Lock window",
        "How far back failures are counted, and how long a lock lasts.", 1, 1440, "minutes"),

    "session_idle_minutes": Spec(
        30, "int", "Sessions", "Sign out after inactivity", "", 5, 1440, "minutes"),
    "session_absolute_hours": Spec(
        12, "int", "Sessions", "Sign out after this long regardless",
        "Applies to sessions started after the change.", 1, 720, "hours"),

    "max_control_actions_per_minute": Spec(
        30, "int", "Controls", "Actions per user per minute",
        "Stops a script or a stolen session from hammering the relays.", 1, 600, "actions"),

    "rule_max_actions_per_minute": Spec(
        60, "int", "Rules", "Actions per rule per minute",
        "A rule that acts more often than this (a chattering contact, a faulty sensor, two "
        "rules fighting) pauses for a minute, then catches up.", 10, 600, "actions"),

    "event_retention_days": Spec(
        90, "int", "Records", "Keep activity and sign-in history for", "", 7, 3650, "days"),
}

GROUPS = ["General", "Sign-in", "Sessions", "Controls", "Rules", "Records"]

# Gatehouse config.toml keys and the settings they became. Its
# require_2fa_for_controls became a per-group setting (see groups.py).
LEGACY_MAP = {
    "app_name": "site_name",
    **{k: k for k in SPECS},
}


def defaults() -> dict:
    return {k: s.default for k, s in SPECS.items()}


def coerce(key: str, raw: object) -> object:
    """Validate a value for a setting. Raises ValueError with a readable message."""
    spec = SPECS[key]
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "on", "yes"}
    if spec.kind == "int":
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"{spec.label}: enter a whole number.") from None
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"{spec.label}: must be at least {spec.minimum}.")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"{spec.label}: must be at most {spec.maximum}.")
        return value
    value = " ".join(str(raw).split())
    if not spec.minimum <= len(value) <= spec.maximum:
        raise ValueError(f"{spec.label}: use {spec.minimum} to {spec.maximum} characters.")
    return value


def load(conn: sqlite3.Connection) -> dict:
    values = defaults()
    for row in conn.execute("SELECT key, value FROM settings"):
        key = row[0]
        if key in SPECS:
            try:
                values[key] = coerce(key, json.loads(row[1]))
            except (ValueError, json.JSONDecodeError):
                pass   # a bad stored value falls back to the default
    return values


def is_set(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone() is not None


def save(conn: sqlite3.Connection, values: dict) -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
    conn.commit()


def parse_form(form, keys=None) -> tuple[dict, list[str]]:
    """Read the settings form, limited to `keys` (the sections this person may
    change) when given. Unticked checkboxes are absent, meaning False. Other
    fields that are absent (a page opened before an update added them) are
    left as they are."""
    values, errors = {}, []
    for key, spec in SPECS.items():
        if keys is not None and key not in keys:
            continue
        if spec.kind != "bool" and key not in form:
            continue
        raw = form.get(key) if spec.kind != "bool" else (key in form)
        try:
            values[key] = coerce(key, raw if raw is not None else "")
        except ValueError as exc:
            errors.append(str(exc))
    return values, errors


def get_flag(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return bool(row and json.loads(row[0]))


def set_flag(conn: sqlite3.Connection, key: str) -> None:
    save(conn, {key: True})
