# SPDX-License-Identifier: GPL-2.0-or-later
"""Upgrading from Gatehouse: database file, schema and config.toml settings."""
from __future__ import annotations

import sqlite3
import time

from app import passwords
from app.db import SCHEMA_VERSION
from conftest import PASSWORD, add_user, db, io, login

# The Gatehouse v2 schema, as shipped.
GATEHOUSE_V2 = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL, password_changed_at INTEGER NOT NULL,
    last_login_at INTEGER, last_login_ip TEXT);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('mfa', 'full')), created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    mfa_failures INTEGER NOT NULL DEFAULT 0, ip TEXT, user_agent TEXT);
CREATE TABLE auth_events (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, username TEXT, ip TEXT,
    event TEXT NOT NULL, success INTEGER NOT NULL, user_agent TEXT);
CREATE TABLE control_events (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, username TEXT, ip TEXT,
    output_id TEXT NOT NULL, action TEXT NOT NULL);
PRAGMA user_version = 2;
"""


def make_gatehouse_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(GATEHOUSE_V2)
    now = int(time.time())
    conn.execute("INSERT INTO users (username, password_hash, created_at, password_changed_at)"
                 " VALUES ('admin', ?, ?, ?)", (passwords.hash_password(PASSWORD), now, now))
    conn.execute("INSERT INTO control_events (ts, user_id, username, ip, output_id, action)"
                 " VALUES (?, 1, 'admin', '127.0.0.1', 'light', 'on')", (now,))
    conn.commit()
    conn.close()


def test_gatehouse_database_is_adopted_and_upgraded(make_app, tmp_path):
    make_gatehouse_db(tmp_path / "gatehouse.db")
    app = make_app()                               # expects tmp_path/coilgate.db
    assert (tmp_path / "coilgate.db").exists() and not (tmp_path / "gatehouse.db").exists()
    with db(app) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT g.builtin FROM users u JOIN user_groups g ON g.id = u.group_id"
                            " WHERE u.username = 'admin'").fetchone()[0] == "admin"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "control_events" not in tables
        moved = conn.execute("SELECT category, action, target FROM activity").fetchone()
        assert tuple(moved) == ("control", "on", "light")
    client = app.test_client()
    assert login(client).status_code == 302
    assert client.get("/admin/io").status_code == 200       # the owner is an admin


def test_fresh_database_is_current(app):
    with db(app) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_gatehouse_config_settings_move_into_the_database(make_app, tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('''
app_name = "Home"
session_idle_minutes = 45
require_2fa_for_controls = false

[[outputs]]
id = "light"
name = "Porch light"
pin = 17

[[outputs]]
id = "gate"
name = "Gate"
pin = 27
mode = "pulse"
pulse_seconds = 2.0
''', encoding="utf-8")
    monkeypatch.setenv("COILGATE_CONFIG", str(cfg))
    app = make_app(require_2fa=True)   # test default; legacy value shouldn't override an explicit one
    with db(app) as conn:
        values = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert values["site_name"] == '"Home"' and values["session_idle_minutes"] == "45"
    names = [(p.name, p.pin, p.mode, p.pulse_seconds) for p in io(app).points("output")]
    assert names == [("Porch light", 17, "toggle", 1.0), ("Gate", 27, "pulse", 2.0)]

    # Delete them in the app; a restart with the same config.toml mustn't bring them back.
    with db(app) as conn:
        conn.execute("DELETE FROM io_points")
    again = make_app()
    assert io(again).points() == []


def test_v3_inputs_and_outputs_survive_the_sensor_upgrade(make_app, tmp_path):
    """A CoilGate v3 database (before sensors) keeps its points and gains the new columns."""
    app = make_app()
    with db(app) as conn:
        # Recreate the v3 io_points table with one output in it.
        conn.execute("DROP TABLE rules")
        conn.execute("DROP TABLE io_points")
        conn.execute("""CREATE TABLE io_points (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('output','input')),
            name TEXT NOT NULL, pin INTEGER NOT NULL UNIQUE CHECK (pin BETWEEN 2 AND 27),
            active_high INTEGER NOT NULL DEFAULT 1, mode TEXT NOT NULL DEFAULT 'toggle',
            pulse_seconds REAL NOT NULL DEFAULT 1.0, wiring TEXT NOT NULL DEFAULT 'to_ground',
            bounce_ms INTEGER NOT NULL DEFAULT 50, on_label TEXT NOT NULL DEFAULT '',
            off_label TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        conn.execute("INSERT INTO io_points (kind, name, pin, mode, pulse_seconds, created_at, updated_at)"
                     " VALUES ('output', 'Gate', 27, 'pulse', 2.5, 1, 1)")
        conn.execute("PRAGMA user_version = 3")
    upgraded = make_app()
    with db(upgraded) as conn:
        row = conn.execute("SELECT name, pin, mode, pulse_seconds, sensor_type, poll_seconds FROM io_points").fetchone()
        assert tuple(row) == ("Gate", 27, "pulse", 2.5, None, 30)
        assert conn.execute("SELECT name FROM schedules WHERE builtin = 1").fetchone()[0] == "Always (24/7)"
    assert io(upgraded).get(1).name == "Gate"


def test_v4_accounts_gain_an_appearance_setting(make_app):
    app = make_app()
    add_user(app)
    with db(app) as conn:
        conn.execute("ALTER TABLE users DROP COLUMN theme")
        conn.execute("PRAGMA user_version = 4")
    upgraded = make_app()
    with db(upgraded) as conn:
        assert conn.execute("SELECT theme FROM users").fetchone()[0] == "auto"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    client = upgraded.test_client()
    assert login(client).status_code == 302
