# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared test fixtures. Run the suite with:  python -m pytest -q"""
from __future__ import annotations

import os
import re
import sqlite3
import time

import pyotp
import pytest

os.environ.setdefault("COILGATE_SECRET_KEY", "test-secret-key-not-for-production")
os.environ["COILGATE_CONFIG"] = os.devnull   # ignore any local config.toml

from app import create_app, load_points   # noqa: E402
from app import passwords                  # noqa: E402

PASSWORD = "correct horse battery staple"


@pytest.fixture
def make_app(tmp_path):
    def factory(**overrides):
        settings = {
            "database": str(tmp_path / "coilgate.db"),
            "gpio_mode": "simulate",
            "check_pwned_passwords": False,
            "require_2fa": False,
            "rule_engine": False,      # tests step the engine themselves
        }
        settings.update(overrides)
        app = create_app(settings)
        app.config["TESTING"] = True
        return app
    return factory


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def db(app) -> sqlite3.Connection:
    conn = sqlite3.connect(app.config["COILGATE"]["database"])
    conn.row_factory = sqlite3.Row
    return conn


def group_id(app, name: str) -> int:
    """A group's id by built-in key (admin, superuser, user) or by name."""
    with db(app) as conn:
        row = conn.execute("SELECT id FROM user_groups WHERE builtin = ? OR name = ?",
                           (name, name)).fetchone()
    assert row, f"no group {name}"
    return row[0]


def group_of(app, username: str) -> str:
    """The built-in key (or, for other groups, the name) of an account's group."""
    with db(app) as conn:
        row = conn.execute("SELECT g.builtin, g.name FROM users u JOIN user_groups g"
                           " ON g.id = u.group_id WHERE u.username = ?", (username,)).fetchone()
    return row[0] or row[1]


def add_user(app, username="admin", password=PASSWORD, role="admin", password_hash=None, **extra):
    """role is a built-in group key (admin, superuser, user) or a group name."""
    now = int(time.time())
    cols = {"username": username, "password_hash": password_hash or passwords.hash_password(password),
            "group_id": group_id(app, role), "created_at": now, "password_changed_at": now, **extra}
    with db(app) as conn:
        conn.execute(f"INSERT INTO users ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(cols.values()))


def csrf(client, path="/login") -> str:
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, username="admin", password=PASSWORD, next_url="", headers=None):
    token = csrf(client)
    return client.post("/login", data={"csrf_token": token, "username": username,
                                       "password": password, "next": next_url},
                       headers=headers or {})


def logout(client):
    client.post("/logout", data={"csrf_token": csrf(client, "/account")})


def post(client, path, data=None, page=None):
    """POST a form with a fresh CSRF token taken from `page` (default: path)."""
    token = csrf(client, page or path)
    return client.post(path, data={"csrf_token": token, **(data or {})})


def auth_cookie(client, name="coilgate_auth"):
    c = client.get_cookie(name)
    return c.value if c else None


def enable_2fa(app, client) -> str:
    client.get("/account/two-step")
    with db(app) as conn:
        secret = conn.execute("SELECT totp_secret FROM users WHERE username='admin'").fetchone()[0]
    token = csrf(client, "/account/two-step")
    # Use the previous step's code so a following sign-in can use the current one.
    code = pyotp.TOTP(secret).at(int(time.time()) - 30)
    resp = client.post("/account/two-step", data={"csrf_token": token, "code": code})
    assert resp.status_code == 302
    return secret


def add_point(app, kind="output", name="Porch light", pin=17, **fields):
    """Insert an input/output directly and apply it, as the admin page would."""
    now = int(time.time())
    cols = {"kind": kind, "name": name, "pin": pin, "created_at": now, "updated_at": now, **fields}
    with db(app) as conn:
        cur = conn.execute(
            f"INSERT INTO io_points ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(cols.values()))
        point_id = cur.lastrowid
    with db(app) as conn:
        app.extensions["coilgate_io"].sync(load_points(conn))
    return point_id


def io(app):
    return app.extensions["coilgate_io"]
