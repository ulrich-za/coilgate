# SPDX-License-Identifier: GPL-2.0-or-later
"""Server-side sessions, brute-force throttling and the audit log."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

MFA_STAGE_SECONDS = 300          # time allowed to enter the 2FA code
MAX_MFA_FAILURES = 5             # per pending login, then start over
LAST_SEEN_WRITE_INTERVAL = 60    # throttle writes (SD-card friendly)

# Events that count as a failed guess for throttling purposes.
_GUESS_EVENTS = ("login", "mfa", "reauth")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# ---------------------------------------------------------------- audit log

def log_event(db: sqlite3.Connection, event: str, success: bool, *, user_id=None,
              username=None, ip=None, user_agent=None) -> None:
    db.execute(
        "INSERT INTO auth_events (ts, user_id, username, ip, event, success, user_agent)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), user_id, username, ip, event, int(success),
         (user_agent or "")[:200]),
    )
    db.commit()


# ---------------------------------------------------------------- throttling

def is_throttled(db: sqlite3.Connection, username: str, ip: str, cfg: dict) -> bool:
    """True when this username or this IP has too many recent failures.

    Applies to any username string, existing or not, so the lock itself
    doesn't reveal which accounts exist. A successful login (or an admin
    unlock) resets the per-username count; the per-IP count isn't reset by a
    success, so one valid account can't be used to launder a spraying attack.
    """
    now = int(time.time())
    window_start = now - cfg["failure_window_minutes"] * 60
    placeholders = ",".join("?" * len(_GUESS_EVENTS))

    # Row ids, not timestamps, mark the reset point: several attempts can land
    # in the same second as the success.
    reset_id = db.execute(
        "SELECT MAX(id) FROM auth_events WHERE username = ? AND"
        " ((event = 'login' AND success = 1) OR event = 'admin_unlock')",
        (username,),
    ).fetchone()[0] or 0

    user_failures = db.execute(
        f"SELECT COUNT(*) FROM auth_events WHERE username = ? AND id > ? AND ts > ?"
        f" AND success = 0 AND event IN ({placeholders})",
        (username, reset_id, window_start, *_GUESS_EVENTS),
    ).fetchone()[0]
    if user_failures >= cfg["max_failures_per_user"]:
        return True

    ip_failures = db.execute(
        f"SELECT COUNT(*) FROM auth_events WHERE ip = ? AND ts > ?"
        f" AND success = 0 AND event IN ({placeholders})",
        (ip, window_start, *_GUESS_EVENTS),
    ).fetchone()[0]
    return ip_failures >= cfg["max_failures_per_ip"]


# ---------------------------------------------------------------- sessions

def create_session(db: sqlite3.Connection, user_id: int, stage: str, cfg: dict,
                   ip: str | None, user_agent: str | None) -> str:
    """Create a session and return the raw token for the cookie."""
    token = secrets.token_urlsafe(32)          # 256 bits
    now = int(time.time())
    lifetime = MFA_STAGE_SECONDS if stage == "mfa" else cfg["session_absolute_hours"] * 3600
    db.execute(
        "INSERT INTO sessions (token_hash, public_id, user_id, stage, created_at,"
        " last_seen_at, expires_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_hash_token(token), secrets.token_hex(8), user_id, stage, now, now,
         now + lifetime, ip, (user_agent or "")[:200]),
    )
    db.commit()
    return token


def load_session(db: sqlite3.Connection, token: str | None, cfg: dict,
                 touch: bool = True) -> sqlite3.Row | None:
    """Look up a session by cookie token, enforcing idle and absolute timeouts.

    touch=False reads the session without counting it as activity, so a page
    polling for live state doesn't keep an unattended session alive forever.
    """
    if not token or len(token) > 100:
        return None
    token_hash = _hash_token(token)
    row = db.execute(
        "SELECT s.*, u.username, u.group_id, u.schedule_id, u.theme, u.must_change_password, u.is_active,"
        " u.totp_enabled, u.totp_secret, u.totp_last_step, u.last_login_at,"
        " u.last_login_ip, u.password_changed_at"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())
    idle_limit = cfg["session_idle_minutes"] * 60
    expired = (
        now >= row["expires_at"]
        or not row["is_active"]
        or (row["stage"] == "full" and now - row["last_seen_at"] > idle_limit)
    )
    if expired:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        db.commit()
        return None

    if touch and row["stage"] == "full" and now - row["last_seen_at"] >= LAST_SEEN_WRITE_INTERVAL:
        db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash))
        db.commit()
    return row


def delete_session(db: sqlite3.Connection, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
        db.commit()


def record_mfa_failure(db: sqlite3.Connection, token: str) -> int:
    token_hash = _hash_token(token)
    db.execute("UPDATE sessions SET mfa_failures = mfa_failures + 1 WHERE token_hash = ?",
               (token_hash,))
    db.commit()
    row = db.execute("SELECT mfa_failures FROM sessions WHERE token_hash = ?",
                     (token_hash,)).fetchone()
    return row["mfa_failures"] if row else MAX_MFA_FAILURES


def revoke_other_sessions(db: sqlite3.Connection, user_id: int, keep_token: str | None) -> int:
    keep = _hash_token(keep_token) if keep_token else ""
    count = db.execute(
        "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep)
    ).rowcount
    db.commit()
    return count


def revoke_by_public_id(db: sqlite3.Connection, user_id: int, public_id: str) -> bool:
    count = db.execute(
        "DELETE FROM sessions WHERE user_id = ? AND public_id = ?", (user_id, public_id)
    ).rowcount
    db.commit()
    return count > 0


def active_sessions(db: sqlite3.Connection, user_id: int, cfg: dict) -> list[sqlite3.Row]:
    now = int(time.time())
    idle_cutoff = now - cfg["session_idle_minutes"] * 60
    return db.execute(
        "SELECT public_id, token_hash, created_at, last_seen_at, ip, user_agent"
        " FROM sessions WHERE user_id = ? AND stage = 'full'"
        " AND expires_at > ? AND last_seen_at >= ? ORDER BY last_seen_at DESC",
        (user_id, now, idle_cutoff),
    ).fetchall()


def hash_token(token: str) -> str:
    return _hash_token(token)
