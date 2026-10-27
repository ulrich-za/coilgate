# SPDX-License-Identifier: GPL-2.0-or-later
"""Groups and privileges: Super User, view-only groups, per-group two-step,
and open pages refreshing when the setup changes."""
from __future__ import annotations

import argparse
import json
import sqlite3

import pyotp
import pytest

from conftest import (PASSWORD, add_point, add_user, db, group_id, group_of, io, login, post)

from app import groups
from app.db import SCHEMA_VERSION


def signed_in(app, username, role):
    add_user(app, username=username, role=role)
    client = app.test_client()
    assert login(client, username=username).status_code == 302
    return client


def uid(app, username):
    with db(app) as conn:
        return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def require_2fa(app, *builtins):
    with db(app) as conn:
        conn.execute("UPDATE user_groups SET require_2fa = (builtin IN (%s))"
                     % ",".join("?" * len(builtins)), builtins)


# ---------------------------------------------------------------- the built-in groups

def test_built_in_groups_and_their_defaults(app):
    with db(app) as conn:
        all_groups = groups.load_all(conn)
    by_key = {g.builtin: g for g in all_groups.values()}
    assert [g.name for g in all_groups.values()] == ["Admin", "Super User", "User"]
    su = by_key["superuser"]
    assert su.targets == {by_key["user"].id}                 # manages User accounts only
    a = su.access()
    for key in ("control", "io", "rules", "schedules", "activity", "settings_view"):
        assert a.can(key), key
    assert not a.can("groups")
    assert not a.can_change_settings("General")              # can't change the site name
    assert a.can_change_settings("Controls") and a.can_change_settings("Rules")
    assert by_key["user"].access().privileges == {"control"}
    assert by_key["admin"].access().can("groups")


@pytest.mark.parametrize("legacy, expected", [
    (None, {"admin": 1, "superuser": 0, "user": 1}),       # a new database
    ("true", {"admin": 1, "superuser": 0, "user": 1}),     # earlier site-wide setting on
    ("false", {"admin": 0, "superuser": 0, "user": 0}),    # ... or off
])
def test_built_in_groups_start_from_the_old_two_step_setting(app, legacy, expected):
    from app import db as database
    conn = database.connect(app.config["COILGATE"]["database"])
    try:
        conn.execute("DELETE FROM group_manages")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM user_groups")
        if legacy is not None:
            conn.execute("INSERT INTO settings (key, value) VALUES ('require_2fa', ?)", (legacy,))
        ids = groups.ensure_builtin(conn)
        rows = dict(conn.execute("SELECT builtin, require_2fa FROM user_groups").fetchall())
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'require_2fa'").fetchone() is None
    finally:
        conn.close()
    assert rows == expected and len(ids) == 3


def test_v6_roles_become_groups(make_app, tmp_path):
    """A v6 database: admins and users move into the Admin and User groups,
    and the site-wide two-step setting becomes theirs."""
    path = tmp_path / "coilgate.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
            must_change_password INTEGER NOT NULL DEFAULT 0, temp_password_expires_at INTEGER,
            totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0,
            totp_last_step INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL, password_changed_at INTEGER NOT NULL,
            last_login_at INTEGER, last_login_ip TEXT, schedule_id INTEGER,
            theme TEXT NOT NULL DEFAULT 'auto');
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings VALUES ('require_2fa', 'false');
        INSERT INTO users (username, password_hash, role, created_at, password_changed_at)
            VALUES ('boss', 'x', 'admin', 1, 1), ('anna', 'x', 'user', 1, 1);
        PRAGMA user_version = 6;
    """)
    conn.close()
    app = make_app()
    with db(app) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        assert "role" not in cols and "group_id" in cols
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'require_2fa'").fetchone() is None
    assert group_of(app, "boss") == "admin" and group_of(app, "anna") == "user"


# ---------------------------------------------------------------- Super User

def test_super_user_signs_in_without_two_step(app):
    require_2fa(app, "admin", "user")
    su = signed_in(app, "sam", "superuser")
    assert su.get("/controls").status_code == 200            # no detour to set up two-step
    page = su.get("/admin/io").get_data(as_text=True)
    for tab in ("Inputs and outputs", "Rules", "Schedules", "Accounts", "Settings", "Activity"):
        assert f">{tab}</a>" in page, tab
    assert ">Groups</a>" not in page
    assert su.get("/admin/groups").status_code == 404
    assert su.get("/admin").status_code == 302
    # A User in a group that needs two-step is sent to set it up.
    user = signed_in(app, "anna", "user")
    resp = user.get("/controls")
    assert resp.status_code == 302 and resp.location.endswith("/account/two-step")


def test_super_user_adds_and_removes_users_only(app):
    add_user(app, username="boss", role="admin")
    add_user(app, username="other", role="superuser")
    su = signed_in(app, "sam", "superuser")
    new = lambda name, role: post(su, "/admin/users/new",
                                  {"username": name, "group_id": str(group_id(app, role))})
    assert new("anna", "user").status_code == 200
    assert group_of(app, "anna") == "user"
    assert new("eve", "admin").status_code == 400
    assert new("mallory", "superuser").status_code == 400
    form = su.get("/admin/users/new").get_data(as_text=True)
    assert f'name="group_id" value="{group_id(app, "admin")}"' not in form
    assert f'name="group_id" value="{group_id(app, "user")}"' in form

    # Admins and other super users are out of reach, even by address.
    for name in ("boss", "other"):
        user_page = f"/admin/users/{uid(app, name)}"
        assert su.get(user_page).status_code == 404
        resp = post(su, user_page, {"do": "delete", "confirm": name}, page="/admin/users")
        assert resp.status_code == 404
    listing = su.get("/admin/users").get_data(as_text=True)
    assert "Can't manage" in listing and f'/admin/users/{uid(app, "anna")}"' in listing

    # Can't promote a user either.
    anna = f"/admin/users/{uid(app, 'anna')}"
    assert post(su, anna, {"do": "group", "group_id": str(group_id(app, "superuser"))}).status_code == 400
    assert group_of(app, "anna") == "user"
    assert post(su, anna, {"do": "delete", "confirm": "anna"}).status_code == 302
    with db(app) as conn:
        names = {r[0] for r in conn.execute("SELECT username FROM users")}
    assert names == {"boss", "other", "sam"}


def test_super_user_sees_all_settings_but_changes_only_some(app):
    su = signed_in(app, "sam", "superuser")
    page = su.get("/admin/settings").get_data(as_text=True)
    assert "Greyed-out sections can't be changed by your group, Super User" in page
    assert page.count('class="settings-group is-locked" disabled') == 4   # General, Sign-in, Sessions, Records
    assert 'name="site_name"' in page and "Locked" in page
    resp = post(su, "/admin/settings", {"site_name": "Hacked", "max_control_actions_per_minute": "12",
                                        "min_password_length": "8"})
    assert resp.status_code == 302
    with db(app) as conn:
        values = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM settings")}
    assert values.get("max_control_actions_per_minute") == 12
    assert "site_name" not in values and "min_password_length" not in values


def test_a_group_that_can_only_see_settings(app):
    admin = signed_in(app, "admin", "admin")
    su_id = group_id(app, "superuser")
    post(admin, f"/admin/groups/{su_id}", {"priv": ["settings_view"]}, page=f"/admin/groups/{su_id}")
    su = signed_in(app, "sam", "superuser")
    page = su.get("/admin/settings").get_data(as_text=True)
    assert "can see these settings but not change them" in page and "Save settings" not in page
    assert post(su, "/admin/settings", {"max_control_actions_per_minute": "5"}).status_code == 302
    with db(app) as conn:
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'max_control_actions_per_minute'").fetchone() is None
    # And lost the other privileges with the next page.
    assert su.get("/admin/io").status_code == 404
    assert su.get("/admin/users").status_code == 404


# ---------------------------------------------------------------- managing groups

def test_admin_adds_a_view_only_group(app):
    admin = signed_in(app, "admin", "admin")
    light = add_point(app)
    resp = post(admin, "/admin/groups/new", {"name": "Viewers"}, page="/admin/groups/new")
    assert resp.status_code == 302
    viewers = group_id(app, "Viewers")
    with db(app) as conn:
        row = conn.execute("SELECT require_2fa, privileges FROM user_groups WHERE id = ?",
                           (viewers,)).fetchone()
    assert row[0] == 0 and json.loads(row[1]) == []
    add_user(app, username="gran", role="Viewers")
    gran = app.test_client()
    login(gran, username="gran")
    page = gran.get("/controls").get_data(as_text=True)
    assert "can see the controls but not switch them" in page and "disabled" in page
    data = gran.post(f"/controls/{light}", headers={"Accept": "application/json"},
                     data={"csrf_token": _token(gran), "do": "on"}).get_json()
    assert data["ok"] is False and io(app).is_on(light) is False
    assert gran.get("/admin").status_code == 404
    assert "View only" in admin.get("/admin/users").get_data(as_text=True)


def _token(client):
    import re
    html = client.get("/controls").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_settings_privileges_imply_seeing_them_and_admin_accounts_stay_admin_only(app):
    admin = signed_in(app, "admin", "admin")
    post(admin, "/admin/groups/new", {"name": "Helpers", "priv": ["settings:Sessions"],
                                      "manages": [str(group_id(app, "admin")), str(group_id(app, "user"))]},
         page="/admin/groups/new")
    with db(app) as conn:
        g = groups.load_all(conn)[group_id(app, "Helpers")]
    assert g.privileges == {"settings_view", "settings:Sessions"}
    assert g.targets == {group_id(app, "user")}               # Admin was dropped


def test_admin_group_is_fixed_apart_from_two_step(app):
    admin = signed_in(app, "admin", "admin")
    admins = group_id(app, "admin")
    post(admin, f"/admin/groups/{admins}", {"name": "Bosses", "priv": [], "require_2fa": "on"},
         page=f"/admin/groups/{admins}")
    with db(app) as conn:
        g = groups.load_all(conn)[admins]
    assert g.name == "Admin" and g.require_2fa and g.access().can("io")


def test_groups_with_accounts_and_built_in_groups_stay(app):
    admin = signed_in(app, "admin", "admin")
    post(admin, "/admin/groups/new", {"name": "Family"}, page="/admin/groups/new")
    family = group_id(app, "Family")
    add_user(app, username="kid", role="Family")
    post(admin, f"/admin/groups/{family}/delete", {}, page=f"/admin/groups/{family}")
    assert group_id(app, "Family") == family
    with db(app) as conn:
        conn.execute("UPDATE users SET group_id = ? WHERE username = 'kid'", (group_id(app, "user"),))
    post(admin, f"/admin/groups/{family}/delete", {}, page=f"/admin/groups/{family}")
    with db(app) as conn:
        assert conn.execute("SELECT 1 FROM user_groups WHERE name = 'Family'").fetchone() is None
    su = group_id(app, "superuser")
    post(admin, f"/admin/groups/{su}/delete", {}, page=f"/admin/groups/{su}")
    assert group_id(app, "superuser") == su
    log = admin.get("/admin/activity?show=user").get_data(as_text=True)
    assert "Group added" in log and "Group removed" in log


def test_moving_someone_into_a_group_that_needs_two_step(app):
    admin = signed_in(app, "admin", "admin")
    sam = signed_in(app, "sam", "superuser")
    assert sam.get("/controls").status_code == 200
    users = group_id(app, "user")
    post(admin, f"/admin/groups/{users}", {"require_2fa": "on", "priv": ["control"]},
         page=f"/admin/groups/{users}")
    post(admin, f"/admin/users/{uid(app, 'sam')}", {"do": "group", "group_id": str(users)})
    resp = sam.get("/controls")
    assert resp.status_code == 302 and resp.location.endswith("/account/two-step")
    assert "Group changed" in admin.get("/admin/activity?show=user").get_data(as_text=True)


def test_super_user_can_turn_two_step_off_for_themselves(app):
    secret = pyotp.random_base32()
    add_user(app, username="sam", role="superuser", totp_enabled=1, totp_secret=secret)
    client = app.test_client()
    login(client, username="sam")
    post(client, "/login/verify", {"code": pyotp.TOTP(secret).now()}, page="/login/verify")
    page = client.get("/account/two-step").get_data(as_text=True)
    assert "Turn off two-step verification" in page


# ---------------------------------------------------------------- command line

def test_command_line_groups(app, capsys, monkeypatch):
    import manage
    add_user(app)
    conn = manage.open_db(app.config["COILGATE"])
    try:
        monkeypatch.setattr(manage, "prompt_new_password", lambda conn, name: PASSWORD)
        manage.cmd_create_user(app.config["COILGATE"], conn,
                               argparse.Namespace(username="sam", group="super user", role=None))
        manage.cmd_set_group(app.config["COILGATE"], conn,
                             argparse.Namespace(username="sam", group="User"))
        manage.cmd_list_groups(app.config["COILGATE"], conn, argparse.Namespace())
        with pytest.raises(SystemExit):
            manage.cmd_set_group(app.config["COILGATE"], conn,
                                 argparse.Namespace(username="sam", group="Nobody"))
    finally:
        conn.close()
    out = capsys.readouterr().out
    assert "Created 'sam' in the Super User group." in out and "now in the User group" in out
    assert "Super User (0 accounts, two-step optional)" in out
    assert group_of(app, "sam") == "user"


# ---------------------------------------------------------------- pages refresh for a new setup

def test_setup_counter_changes_with_the_setup_only(app):
    client = signed_in(app, "admin", "admin")
    light = add_point(app)
    first = client.get("/controls/state").get_json()["setup"]
    post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    assert client.get("/controls/state").get_json()["setup"] == first     # switching isn't setup
    add_point(app, name="Gate", pin=27)
    assert client.get("/controls/state").get_json()["setup"] == first + 1
    with db(app) as conn:
        conn.execute("UPDATE io_points SET name = 'Front gate' WHERE pin = 27")
    from app import load_points
    with db(app) as conn:
        io(app).sync(load_points(conn))
    setup = client.get("/controls/state").get_json()["setup"]
    assert setup == first + 2
    for page in ("/controls", "/pinout"):
        assert f'data-setup="{setup}"' in client.get(page).get_data(as_text=True), page
