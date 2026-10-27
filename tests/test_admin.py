# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin area: access, accounts, inputs and outputs, settings, activity."""
from __future__ import annotations

import re
import time

import pyotp
import pytest

from conftest import (PASSWORD, add_point, add_user, csrf, db, group_id, group_of, io, login,
                      logout, post)


def admin_client(app):
    add_user(app)
    client = app.test_client()
    login(client)
    return client


# ---------------------------------------------------------------- access

ADMIN_PAGES = ["/admin/io", "/admin/io/new/output", "/admin/users", "/admin/users/new",
               "/admin/settings", "/admin/activity"]


def test_users_cannot_see_admin(app):
    add_user(app, username="anna", role="user")
    client = app.test_client()
    login(client, username="anna")
    for path in ADMIN_PAGES:
        assert client.get(path).status_code == 404, path
    assert ">Admin<" not in client.get("/controls").get_data(as_text=True)


def test_admins_can_see_admin(app):
    client = admin_client(app)
    for path in ADMIN_PAGES:
        assert client.get(path).status_code == 200, path
    assert ">Admin<" in client.get("/controls").get_data(as_text=True)


def test_admin_area_restricted_by_network(make_app):
    app = make_app(admin_networks=["192.168.1.0/24"])
    client = admin_client(app)                       # test client comes from 127.0.0.1
    assert client.get("/admin/io").status_code == 404
    assert ">Admin<" not in client.get("/controls").get_data(as_text=True)
    assert client.get("/admin/io", environ_base={"REMOTE_ADDR": "192.168.1.20"}).status_code == 200


def test_bad_admin_network_stops_startup(make_app):
    with pytest.raises(SystemExit):
        make_app(admin_networks=["not-a-network"])


# ---------------------------------------------------------------- accounts

def create_account(client, username="anna", role="user") -> str:
    app = client.application
    resp = post(client, "/admin/users/new", {"username": username, "group_id": str(group_id(app, role))})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    return re.search(r'class="secret">([a-z2-9-]+)<', html).group(1)


def test_new_account_flow(make_app):
    app = make_app(require_2fa=True)
    add_user(app, totp_enabled=1, totp_secret=pyotp.random_base32())   # an admin with 2FA
    admin = app.test_client()
    # sign the admin in through two-step
    login(admin)
    with db(app) as conn:
        secret = conn.execute("SELECT totp_secret FROM users WHERE username='admin'").fetchone()[0]
    post(admin, "/login/verify", {"code": pyotp.TOTP(secret).now()}, page="/login/verify")
    temp = create_account(admin)

    with db(app) as conn:
        row = conn.execute("SELECT must_change_password, temp_password_expires_at"
                           " FROM users WHERE username='anna'").fetchone()
    assert group_of(app, "anna") == "user" and row["must_change_password"] == 1
    assert row["temp_password_expires_at"] > time.time() + 71 * 3600

    anna = app.test_client()
    assert login(anna, username="anna", password=temp).status_code == 302
    # Step 1: every page leads to choosing a password
    for path in ("/controls", "/account", "/account/two-step"):
        resp = anna.get(path)
        assert resp.status_code == 302 and resp.location.endswith("/account/password"), path
    page = anna.get("/account/password").get_data(as_text=True)
    assert "Choose your password" in page and "current_password" not in page
    new = "violet harbour lantern quietly"
    resp = post(anna, "/account/password", {"new_password": new, "confirm_password": new})
    assert resp.status_code == 302
    # Step 2: two-step verification is required on this site
    resp = anna.get("/controls")
    assert resp.status_code == 302 and resp.location.endswith("/account/two-step")
    assert anna.get("/controls/state").status_code == 403
    anna.get("/account/two-step")
    with db(app) as conn:
        secret = conn.execute("SELECT totp_secret FROM users WHERE username='anna'").fetchone()[0]
    post(anna, "/account/two-step", {"code": pyotp.TOTP(secret).now()})
    assert anna.get("/controls").status_code == 200
    # The temporary password no longer works
    logout(anna)
    assert login(anna, username="anna", password=temp).status_code == 401


def test_expired_temporary_password(app):
    client = admin_client(app)
    temp = create_account(client)
    with db(app) as conn:
        conn.execute("UPDATE users SET temp_password_expires_at = ? WHERE username='anna'",
                     (int(time.time()) - 1,))
    anna = app.test_client()
    resp = login(anna, username="anna", password=temp)
    assert resp.status_code == 401 and "temporary password has expired" in resp.get_data(as_text=True)


def test_duplicate_and_invalid_usernames(app):
    client = admin_client(app)
    user = str(group_id(app, "user"))
    assert post(client, "/admin/users/new", {"username": "admin", "group_id": user}).status_code == 400
    assert post(client, "/admin/users/new", {"username": "Bad Name!", "group_id": user}).status_code == 400
    assert post(client, "/admin/users/new", {"username": "fine", "group_id": "999"}).status_code == 400


def user_id(app, username):
    with db(app) as conn:
        return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def test_reset_password_signs_user_out(app):
    client = admin_client(app)
    add_user(app, username="anna", role="user")
    anna = app.test_client()
    login(anna, username="anna")
    uid = user_id(app, "anna")
    resp = post(client, f"/admin/users/{uid}", {"do": "reset_password"})
    assert resp.status_code == 200 and "New temporary password" in resp.get_data(as_text=True)
    assert anna.get("/controls").status_code == 302
    assert login(anna, username="anna").status_code == 401        # old password gone


def test_role_change_and_disable(app):
    client = admin_client(app)
    add_user(app, username="anna", role="user")
    uid = user_id(app, "anna")
    post(client, f"/admin/users/{uid}", {"do": "group", "group_id": str(group_id(app, "admin"))})
    assert group_of(app, "anna") == "admin"
    post(client, f"/admin/users/{uid}", {"do": "disable"})
    anna = app.test_client()
    assert login(anna, username="anna").status_code == 401


def test_admin_cannot_lock_themselves_out(app):
    client = admin_client(app)
    me = user_id(app, "admin")
    for data in ({"do": "group", "group_id": str(group_id(app, "user"))}, {"do": "disable"},
                 {"do": "delete", "confirm": "admin"}):
        resp = post(client, f"/admin/users/{me}", data)
        assert resp.status_code == 400, data
    with db(app) as conn:
        row = conn.execute("SELECT is_active FROM users WHERE id=?", (me,)).fetchone()
    assert tuple(row) == (1,) and group_of(app, "admin") == "admin"


def test_demoting_another_admin_is_allowed(app):
    client = admin_client(app)
    add_user(app, username="boss", role="admin")
    boss = user_id(app, "boss")
    assert post(client, f"/admin/users/{boss}",
                {"do": "group", "group_id": str(group_id(app, "user"))}).status_code == 302
    assert group_of(app, "boss") == "user"


def test_command_line_protects_the_last_admin(app):
    import argparse
    import manage
    add_user(app)
    conn = manage.open_db(app.config["COILGATE"])
    try:
        for func, args in ((manage.cmd_set_group, {"username": "admin", "group": "user"}),
                           (manage.cmd_set_active(False), {"username": "admin"})):
            with pytest.raises(SystemExit) as exc:
                func(app.config["COILGATE"], conn, argparse.Namespace(**args))
            assert "only admin" in str(exc.value)
    finally:
        conn.close()


def test_delete_requires_typing_the_username(app):
    client = admin_client(app)
    add_user(app, username="anna", role="user")
    uid = user_id(app, "anna")
    assert post(client, f"/admin/users/{uid}", {"do": "delete", "confirm": "nope"}).status_code == 400
    assert post(client, f"/admin/users/{uid}", {"do": "delete", "confirm": "anna"}).status_code == 302
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username='anna'").fetchone()[0] == 0


def test_reset_two_step_for_someone(app):
    client = admin_client(app)
    add_user(app, username="anna", role="user", totp_enabled=1, totp_secret=pyotp.random_base32())
    uid = user_id(app, "anna")
    post(client, f"/admin/users/{uid}", {"do": "reset_2fa"})
    with db(app) as conn:
        row = conn.execute("SELECT totp_enabled, totp_secret FROM users WHERE id=?", (uid,)).fetchone()
    assert tuple(row) == (0, None)


# ---------------------------------------------------------------- inputs and outputs

def new_output(client, **fields):
    data = {"name": "Porch light", "pin": "17", "mode": "toggle", "trigger": "high",
            "pulse_value": "1", "pulse_unit": "s", "on_label": "", "off_label": "", **fields}
    return post(client, "/admin/io/new/output", data)


def test_add_output_from_admin(app):
    client = admin_client(app)
    resp = new_output(client, name="Garden pump", pin="23", mode="pulse", pulse_value="5",
                      pulse_unit="min")
    assert resp.status_code == 302
    with db(app) as conn:
        row = conn.execute("SELECT * FROM io_points").fetchone()
    assert (row["name"], row["pin"], row["mode"], row["pulse_seconds"]) == ("Garden pump", 23, "pulse", 300)
    page = client.get("/controls").get_data(as_text=True)
    assert "Garden pump" in page and "5 minutes pulse" in page
    assert io(app).get(row["id"]).pulse_seconds == 300


def test_pulse_length_limits(app):
    client = admin_client(app)
    assert new_output(client, mode="pulse", pulse_value="25", pulse_unit="h").status_code == 400
    assert new_output(client, mode="pulse", pulse_value="0.05", pulse_unit="s").status_code == 400
    assert new_output(client, mode="pulse", pulse_value="24", pulse_unit="h").status_code == 302


def test_pin_conflicts_are_refused(app):
    client = admin_client(app)
    new_output(client, name="Light", pin="17")
    resp = new_output(client, name="Other", pin="17")
    assert resp.status_code == 400 and "already used by Light" in resp.get_data(as_text=True)


def test_renaming_keeps_an_output_on(app):
    client = admin_client(app)
    pid = add_point(app)
    post(client, f"/controls/{pid}", {"do": "on"}, page="/controls")
    resp = post(client, f"/admin/io/{pid}", {"name": "Porch lamp", "pin": "17", "mode": "toggle",
                                             "trigger": "high", "pulse_value": "1", "pulse_unit": "s",
                                             "on_label": "Lit", "off_label": "Dark"})
    assert resp.status_code == 302
    assert io(app).is_on(pid) and io(app).get(pid).name == "Porch lamp"
    assert ">Lit<" in client.get("/controls").get_data(as_text=True)


def test_moving_an_output_to_another_pin(app):
    client = admin_client(app)
    pid = add_point(app)
    post(client, f"/admin/io/{pid}", {"name": "Porch light", "pin": "27", "mode": "toggle",
                                      "trigger": "high", "pulse_value": "1", "pulse_unit": "s"})
    assert io(app).get(pid).pin == 27 and io(app).available(pid)
    assert new_output(client, name="Reuse old pin", pin="17").status_code == 302


def test_active_low_on_pull_down_pin_warns(app):
    client = admin_client(app)
    new_output(client, pin="17", trigger="low")
    page = client.get("/admin/io").get_data(as_text=True)
    assert "sits low while the Pi boots" in page


def test_add_input_from_admin(app):
    client = admin_client(app)
    resp = post(client, "/admin/io/new/input", {"name": "Front door", "pin": "5",
                                                 "wiring": "to_ground", "bounce_ms": "50",
                                                 "on_label": "Shut", "off_label": "Open"})
    assert resp.status_code == 302
    assert "Front door" in client.get("/controls").get_data(as_text=True)


def test_contact_to_3v3_refused_on_i2c_pins(app):
    client = admin_client(app)
    resp = post(client, "/admin/io/new/input", {"name": "Button", "pin": "2", "wiring": "to_3v3",
                                                 "bounce_ms": "50"})
    assert resp.status_code == 400 and "fixed pull-up" in resp.get_data(as_text=True)


def test_delete_needs_confirmation_and_releases_pin(app):
    client = admin_client(app)
    pid = add_point(app)
    post(client, f"/admin/io/{pid}/delete", {}, page=f"/admin/io/{pid}")
    assert io(app).get(pid) is not None
    post(client, f"/admin/io/{pid}/delete", {"confirm": "yes"}, page=f"/admin/io/{pid}")
    assert io(app).get(pid) is None
    assert new_output(client, name="Reuse", pin="17").status_code == 302    # pin is free again


def test_reorder(app):
    client = admin_client(app)
    a = add_point(app, name="First", pin=17, sort_order=0)
    b = add_point(app, name="Second", pin=27, sort_order=1)
    post(client, f"/admin/io/{b}/move", {"direction": "up"}, page="/admin/io")
    assert [p.name for p in io(app).points("output")] == ["Second", "First"]
    page = client.get("/controls").get_data(as_text=True)
    assert page.index("<h3>Second</h3>") < page.index("<h3>First</h3>")


def test_setup_changes_are_logged(app):
    client = admin_client(app)
    new_output(client, name="Gate", pin="27", mode="pulse", pulse_value="2", pulse_unit="s")
    with db(app) as conn:
        row = conn.execute("SELECT action, target, detail, username FROM activity WHERE category='io'").fetchone()
    assert tuple(row) == ("created", "Gate", "GPIO 27, pulse 2 s, triggers on high", "admin")


# ---------------------------------------------------------------- settings

SETTINGS_FORM = {
    "site_name": "CoilGate", "min_password_length": "15",
    "check_pwned_passwords": "on", "max_failures_per_user": "5", "max_failures_per_ip": "20",
    "failure_window_minutes": "15", "session_idle_minutes": "30", "session_absolute_hours": "12",
    "max_control_actions_per_minute": "30", "event_retention_days": "90",
}


def test_settings_save_and_take_effect(app):
    client = admin_client(app)
    form = {**SETTINGS_FORM, "site_name": "Home", "max_failures_per_user": "3"}
    assert post(client, "/admin/settings", form).status_code == 302
    assert '</span>Home' in client.get("/controls").get_data(as_text=True)
    # Lockout now after 3 failures
    other = app.test_client()
    for _ in range(3):
        login(other, password="wrong guess here again")
    assert login(other).status_code == 429
    with db(app) as conn:
        detail = conn.execute("SELECT detail FROM activity WHERE category='settings'").fetchone()[0]
    assert "Site name: Home" in detail and "Failed attempts before an account is locked: 3" in detail


def test_settings_validation(app):
    client = admin_client(app)
    resp = post(client, "/admin/settings", {**SETTINGS_FORM, "session_idle_minutes": "1"})
    assert resp.status_code == 400 and "must be at least 5" in resp.get_data(as_text=True)


def test_requiring_two_step_for_a_group_applies_at_once(app):
    client = admin_client(app)
    admins = group_id(app, "admin")
    post(client, f"/admin/groups/{admins}", {"require_2fa": "on"}, page=f"/admin/groups/{admins}")
    resp = client.get("/controls")
    assert resp.status_code == 302 and resp.location.endswith("/account/two-step")


def test_two_step_cannot_be_disabled_when_required(make_app):
    app = make_app(require_2fa=True)
    secret = pyotp.random_base32()
    add_user(app, totp_enabled=1, totp_secret=secret)
    client = app.test_client()
    login(client)
    post(client, "/login/verify", {"code": pyotp.TOTP(secret).now()}, page="/login/verify")
    post(client, "/account/two-step/disable", {"current_password": PASSWORD, "code": "123456"},
         page="/account/two-step")
    with db(app) as conn:
        assert conn.execute("SELECT totp_enabled FROM users").fetchone()[0] == 1


# ---------------------------------------------------------------- activity

def test_activity_page_filters(app):
    client = admin_client(app)
    pid = add_point(app)
    post(client, f"/controls/{pid}", {"do": "on"}, page="/controls")
    create_account(client, "anna")
    everything = client.get("/admin/activity").get_data(as_text=True)
    assert "Turned on" in everything and "Account created" in everything
    accounts = client.get("/admin/activity?show=user").get_data(as_text=True)
    assert "Account created" in accounts and "Turned on" not in accounts
    signins = client.get("/admin/activity?show=signin").get_data(as_text=True)
    assert "Sign-in" in signins
