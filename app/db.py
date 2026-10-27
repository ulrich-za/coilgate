# SPDX-License-Identifier: GPL-2.0-or-later
"""SQLite access. Plain sqlite3 from the standard library, parameterised queries only."""
from __future__ import annotations

import logging
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

from flask import current_app, g

from .config import ensure_private_dir

log = logging.getLogger("coilgate")

SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                       INTEGER PRIMARY KEY,
    username                 TEXT    NOT NULL UNIQUE,
    password_hash            TEXT    NOT NULL,
    group_id                 INTEGER REFERENCES user_groups(id),
    must_change_password     INTEGER NOT NULL DEFAULT 0,
    temp_password_expires_at INTEGER,
    totp_secret              TEXT,
    totp_enabled             INTEGER NOT NULL DEFAULT 0,
    totp_last_step           INTEGER NOT NULL DEFAULT 0,
    is_active                INTEGER NOT NULL DEFAULT 1,
    created_at               INTEGER NOT NULL,
    password_changed_at      INTEGER NOT NULL,
    last_login_at            INTEGER,
    last_login_ip            TEXT,
    schedule_id              INTEGER REFERENCES schedules(id),
    theme                    TEXT    NOT NULL DEFAULT 'auto'
);

-- Account groups and what they may do (see groups.py). Built-in groups have
-- a builtin key; privileges is a JSON list. group_manages lists the groups
-- whose accounts a group's members may add, change and remove.
CREATE TABLE IF NOT EXISTS user_groups (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    builtin     TEXT    UNIQUE CHECK (builtin IS NULL OR builtin IN ('admin', 'superuser', 'user')),
    require_2fa INTEGER NOT NULL DEFAULT 1,
    privileges  TEXT    NOT NULL DEFAULT '[]',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS group_manages (
    group_id  INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, target_id)
);

-- Server-side sessions. The browser holds a random token; only its SHA-256
-- is stored, so a copy of the database cannot be used to hijack sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT    PRIMARY KEY,
    public_id    TEXT    NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage        TEXT    NOT NULL CHECK (stage IN ('mfa', 'full')),
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    mfa_failures INTEGER NOT NULL DEFAULT 0,
    ip           TEXT,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Sign-in audit log, also used for brute-force throttling.
CREATE TABLE IF NOT EXISTS auth_events (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username   TEXT,
    ip         TEXT,
    event      TEXT    NOT NULL,
    success    INTEGER NOT NULL,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_username ON auth_events(username, ts);
CREATE INDEX IF NOT EXISTS idx_events_ip       ON auth_events(ip, ts);
CREATE INDEX IF NOT EXISTS idx_events_user     ON auth_events(user_id, ts);

-- Inputs, outputs and sensor readings, configured in Admin > Inputs and outputs.
-- Sensors on 1-Wire or I2C share pins, so pin is optional; digital points
-- and DHT sensors still each need their own (see the unique index).
CREATE TABLE IF NOT EXISTS io_points (
    id            INTEGER PRIMARY KEY,
    kind          TEXT    NOT NULL CHECK (kind IN ('output', 'input', 'sensor')),
    name          TEXT    NOT NULL,
    pin           INTEGER CHECK (pin IS NULL OR pin BETWEEN 2 AND 27),
    active_high   INTEGER NOT NULL DEFAULT 1,
    mode          TEXT    NOT NULL DEFAULT 'toggle' CHECK (mode IN ('toggle', 'pulse')),
    pulse_seconds REAL    NOT NULL DEFAULT 1.0,
    wiring        TEXT    NOT NULL DEFAULT 'to_ground'
                  CHECK (wiring IN ('to_ground', 'to_3v3', 'active_high', 'active_low')),
    bounce_ms     INTEGER NOT NULL DEFAULT 50,
    sensor_type   TEXT    CHECK (sensor_type IS NULL OR sensor_type IN ('ds18b20', 'bme280', 'dht22')),
    quantity      TEXT    CHECK (quantity IS NULL OR quantity IN ('temperature', 'humidity', 'pressure')),
    address       TEXT,
    poll_seconds  INTEGER NOT NULL DEFAULT 30,
    calibration   REAL    NOT NULL DEFAULT 0,
    on_label      TEXT    NOT NULL DEFAULT '',
    off_label     TEXT    NOT NULL DEFAULT '',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_io_pin ON io_points(pin) WHERE pin IS NOT NULL;

-- Schedules: named sets of weekly time windows. One built-in "Always (24/7)".
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    builtin    INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_windows (
    id          INTEGER PRIMARY KEY,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    days        INTEGER NOT NULL CHECK (days BETWEEN 1 AND 127),
    start_min   INTEGER NOT NULL CHECK (start_min BETWEEN 0 AND 1439),
    end_min     INTEGER NOT NULL CHECK (end_min BETWEEN 1 AND 1440)
);

-- Rules: when <trigger> (optionally only during a schedule), do <action>.
CREATE TABLE IF NOT EXISTS rules (
    id                  INTEGER PRIMARY KEY,
    name                TEXT    NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    trigger_type        TEXT    NOT NULL CHECK (trigger_type IN ('input', 'sensor', 'schedule', 'rule')),
    source_id           INTEGER REFERENCES io_points(id),
    source_rule_id      INTEGER REFERENCES rules(id),
    input_state         TEXT    CHECK (input_state IS NULL OR input_state IN ('active', 'inactive')),
    comparator          TEXT    CHECK (comparator IS NULL OR comparator IN ('above', 'below')),
    threshold           REAL,
    reset_threshold     REAL,
    trigger_schedule_id INTEGER REFERENCES schedules(id),
    hold_seconds        INTEGER NOT NULL DEFAULT 0,
    schedule_id         INTEGER NOT NULL REFERENCES schedules(id),
    output_id           INTEGER REFERENCES io_points(id),
    target_rule_id      INTEGER REFERENCES rules(id),
    action              TEXT    NOT NULL CHECK (action IN ('on', 'off', 'pulse', 'toggle',
                                                           'rule_on', 'rule_off', 'rule_toggle')),
    undo                INTEGER NOT NULL DEFAULT 0,
    last_fired_at       INTEGER,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    -- A rule acts on exactly one thing: an output or another rule.
    CHECK ((output_id IS NULL) != (target_rule_id IS NULL))
);

-- Everything that isn't a sign-in: switching outputs, input changes,
-- configuration, account and settings changes.
CREATE TABLE IF NOT EXISTS activity (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    category  TEXT    NOT NULL,
    action    TEXT    NOT NULL,
    target    TEXT,
    detail    TEXT,
    io_id     INTEGER,
    user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username  TEXT,
    ip        TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts   ON activity(ts);
CREATE INDEX IF NOT EXISTS idx_activity_cat  ON activity(category, ts);
CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(user_id, ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    db_file = Path(path)
    if not db_file.parent.exists():
        ensure_private_dir(db_file.parent)
    conn = sqlite3.connect(path, timeout=10)
    if sys.platform != "win32":
        # Owner-only. SQLite gives the -wal/-shm side files the same mode.
        os.chmod(db_file, stat.S_IRUSR | stat.S_IWUSR)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL lets readers and the writer work concurrently; NORMAL sync is safe
    # with WAL and spares the Pi's SD card a lot of fsyncs.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def adopt_gatehouse_database(path: str) -> None:
    """If coilgate.db doesn't exist yet but a Gatehouse database sits next to
    it, rename it so accounts carry over from the earlier version."""
    target = Path(path)
    legacy = target.with_name("gatehouse.db")
    if target.name != "coilgate.db" or target.exists() or not legacy.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        old = Path(str(legacy) + suffix)
        if old.exists():
            old.rename(str(target) + suffix)
    log.info("Renamed %s to %s (from Gatehouse)", legacy.name, target.name)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{version} is newer than this code (v{SCHEMA_VERSION}).")

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    had_users = "users" in tables

    # v3 -> v4: io_points gains sensors and optional pins. SQLite can't change
    # column constraints in place, so the table is rebuilt (before anything
    # that references it exists).
    if "io_points" in tables and "sensor_type" not in _columns(conn, "io_points"):
        _rebuild_io_points(conn)
    # v5 -> v6: rules can be triggered by, and act on, other rules.
    if "rules" in tables and "target_rule_id" not in _columns(conn, "rules"):
        _rebuild_rules(conn)

    conn.executescript(SCHEMA)

    # v1/v2 (Gatehouse) -> v3: roles, temporary passwords, activity log.
    cols = _columns(conn, "users")
    gatehouse = had_users and version < 3
    if gatehouse and "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
                     " CHECK (role IN ('admin', 'user'))")
    if "must_change_password" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    if "temp_password_expires_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN temp_password_expires_at INTEGER")
    if gatehouse:
        # Accounts from Gatehouse were made at the command line by the owner.
        conn.execute("UPDATE users SET role = 'admin'")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "control_events" in tables:
            conn.execute(
                "INSERT INTO activity (ts, category, action, target, user_id, username, ip)"
                " SELECT ts, 'control', action, output_id, user_id, username, ip"
                " FROM control_events")
            conn.execute("DROP TABLE control_events")

    if "schedule_id" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN schedule_id INTEGER REFERENCES schedules(id)")
    # v4 -> v5: each account chooses light, dark or the device's setting.
    if "theme" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'auto'")

    # v6 -> v7: the admin/user role becomes a group with privileges.
    from .groups import ADMIN, USER, ensure_builtin
    ids = ensure_builtin(conn)
    if "group_id" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN group_id INTEGER REFERENCES user_groups(id)")
    if "role" in _columns(conn, "users"):
        conn.execute("UPDATE users SET group_id = CASE WHEN role = 'admin' THEN ? ELSE ? END"
                     " WHERE group_id IS NULL", (ids[ADMIN], ids[USER]))
        conn.commit()
        try:
            conn.execute("ALTER TABLE users DROP COLUMN role")
        except sqlite3.OperationalError:
            log.info("Kept the old users.role column (SQLite before 3.35); it's no longer used")
        log.info("Moved accounts into the Admin and User groups")
    conn.execute("UPDATE users SET group_id = ? WHERE group_id IS NULL", (ids[USER],))

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()

    from .schedules import ensure_always
    ensure_always(conn)


_IO_COLUMNS_V3 = ("id, kind, name, pin, active_high, mode, pulse_seconds, wiring, bounce_ms,"
                  " on_label, off_label, sort_order, created_at, updated_at")


def _rebuild_io_points(conn: sqlite3.Connection) -> None:
    create = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS io_points"):]
    create = create[:create.index(");") + 2].replace(
        "CREATE TABLE IF NOT EXISTS io_points", "CREATE TABLE io_points_v4")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(create)
        conn.execute(f"INSERT INTO io_points_v4 ({_IO_COLUMNS_V3}) SELECT {_IO_COLUMNS_V3} FROM io_points")
        conn.execute("DROP TABLE io_points")
        conn.execute("ALTER TABLE io_points_v4 RENAME TO io_points")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    log.info("Upgraded the inputs and outputs table for sensors")


_RULE_COLUMNS_V5 = (
    "id, name, enabled, trigger_type, source_id, input_state, comparator, threshold,"
    " reset_threshold, trigger_schedule_id, hold_seconds, schedule_id, output_id, action,"
    " undo, last_fired_at, created_at, updated_at")


def _rebuild_rules(conn: sqlite3.Connection) -> None:
    create = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS rules"):]
    create = create[:create.index("\n);") + 3].replace(
        "CREATE TABLE IF NOT EXISTS rules", "CREATE TABLE rules_v6").replace(
        "REFERENCES rules(id)", "REFERENCES rules_v6(id)")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(create)
        conn.execute(f"INSERT INTO rules_v6 ({_RULE_COLUMNS_V5}) SELECT {_RULE_COLUMNS_V5} FROM rules")
        conn.execute("DROP TABLE rules")
        conn.execute("ALTER TABLE rules_v6 RENAME TO rules")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    log.info("Upgraded the rules table for rules that trigger other rules")


def get_db() -> sqlite3.Connection:
    """Per-request connection, closed automatically at the end of the request."""
    if "db" not in g:
        g.db = connect(current_app.config["COILGATE"]["database"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def prune(conn: sqlite3.Connection, retention_days: int) -> tuple[int, int]:
    """Remove expired sessions and old history. Returns (sessions, events) removed."""
    now = int(time.time())
    s = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,)).rowcount
    cutoff = now - retention_days * 86400
    e = conn.execute("DELETE FROM auth_events WHERE ts < ?", (cutoff,)).rowcount
    e += conn.execute("DELETE FROM activity WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return s, e


def backup(conn: sqlite3.Connection, dest: str) -> None:
    """Consistent copy of a live database (safe while the app is running)."""
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")  # single self-contained file
    finally:
        target.close()
