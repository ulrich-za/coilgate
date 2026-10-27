# SPDX-License-Identifier: GPL-2.0-or-later
"""Access schedules: a User account can be limited to a schedule; admins never are."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from conftest import add_point, add_user, db, group_id, io, login, post


def add_schedule(app, name, days):
    now = int(time.time())
    with db(app) as conn:
        cur = conn.execute("INSERT INTO schedules (name, builtin, created_at, updated_at) VALUES (?, 0, ?, ?)",
                           (name, now, now))
        conn.execute("INSERT INTO schedule_windows (schedule_id, days, start_min, end_min) VALUES (?, ?, 0, 1440)",
                     (cur.lastrowid, days))
        return cur.lastrowid


def not_today(app):
    """All day, but only on tomorrow's weekday: never active while the test runs."""
    tomorrow = (datetime.now() + timedelta(days=1)).weekday()
    return add_schedule(app, "Tomorrow only", 1 << tomorrow)


def every_day(app):
    return add_schedule(app, "Every day", 0b1111111)


def user_id(app, username):
    with db(app) as conn:
        return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def signed_in(app, username, role="user", **extra):
    add_user(app, username=username, role=role, **extra)
    client = app.test_client()
    login(client, username=username)
    return client


def test_user_outside_schedule_sees_but_cannot_switch(app):
    light = add_point(app)
    sid = not_today(app)
    client = signed_in(app, "guest", schedule_id=sid)
    page = client.get("/controls").get_data(as_text=True)
    assert "Your access follows the <strong>Tomorrow only</strong> schedule" in page
    assert "available again tomorrow at 00:00" in page
    assert "disabled" in page
    post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    assert not io(app).is_on(light)
    assert "available again tomorrow" in client.get("/account").get_data(as_text=True)


def test_user_inside_schedule_can_switch(app):
    light = add_point(app)
    client = signed_in(app, "guest", schedule_id=every_day(app))
    assert "Your access follows" not in client.get("/controls").get_data(as_text=True)
    post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    assert io(app).is_on(light)
    assert "available now" in client.get("/account").get_data(as_text=True)


def test_admin_is_never_limited(app):
    light = add_point(app)
    sid = not_today(app)
    client = signed_in(app, "admin", role="admin", schedule_id=sid)   # e.g. left over from a demotion
    post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    assert io(app).is_on(light)


def test_admin_sets_and_clears_a_users_schedule(app):
    add_user(app)                      # admin "admin"
    add_user(app, username="guest", role="user")
    admin = app.test_client()
    login(admin)
    sid = not_today(app)
    gid = user_id(app, "guest")
    page = f"/admin/users/{gid}"
    resp = post(admin, page, {"do": "schedule", "schedule_id": str(sid)})
    assert resp.status_code == 302
    with db(app) as conn:
        assert conn.execute("SELECT schedule_id FROM users WHERE id = ?", (gid,)).fetchone()[0] == sid
    assert "Tomorrow only" in admin.get("/admin/users").get_data(as_text=True)

    # Promote to admin: schedule cleared, and admins can't be given one.
    post(admin, page, {"do": "group", "group_id": str(group_id(app, "admin"))})
    with db(app) as conn:
        assert conn.execute("SELECT schedule_id FROM users WHERE id = ?", (gid,)).fetchone()[0] is None
    resp = post(admin, page, {"do": "schedule", "schedule_id": str(sid)})
    assert resp.status_code == 400 and "schedules apply to other groups" in resp.get_data(as_text=True)


def test_new_account_with_schedule(app):
    add_user(app)
    admin = app.test_client()
    login(admin)
    sid = every_day(app)
    resp = post(admin, "/admin/users/new", {"username": "cleaner", "group_id": str(group_id(app, "user")),
                                                "schedule_id": str(sid)})
    assert resp.status_code == 200
    with db(app) as conn:
        assert conn.execute("SELECT schedule_id FROM users WHERE username='cleaner'").fetchone()[0] == sid
    resp = post(admin, "/admin/users/new", {"username": "other", "group_id": str(group_id(app, "user")),
                                                "schedule_id": "999"})
    assert resp.status_code == 400 and "Choose an access schedule" in resp.get_data(as_text=True)


def test_schedule_in_use_cannot_be_deleted(app):
    add_user(app)
    sid = not_today(app)
    add_user(app, username="guest", role="user", schedule_id=sid)
    admin = app.test_client()
    login(admin)
    post(admin, f"/admin/schedules/{sid}/delete", {"confirm": "yes"}, page=f"/admin/schedules/{sid}")
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schedules WHERE id = ?", (sid,)).fetchone()[0] == 1
