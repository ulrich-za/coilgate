# SPDX-License-Identifier: GPL-2.0-or-later
"""Appearance: each account chooses light, dark or the device's setting."""
from __future__ import annotations

from conftest import add_user, db, login, post


def test_default_follows_the_device(client, app):
    add_user(app)
    login(client)
    page = client.get("/account").get_data(as_text=True)
    assert "data-theme" not in page.split("<head>")[0]
    assert '<meta name="color-scheme" content="light dark">' in page


def test_choosing_light_or_dark(client, app):
    add_user(app)
    login(client)
    resp = post(client, "/account/appearance", {"theme": "light"}, page="/account")
    assert resp.status_code == 302
    page = client.get("/controls").get_data(as_text=True)
    assert '<html lang="en" data-theme="light">' in page
    assert '<meta name="color-scheme" content="light">' in page
    post(client, "/account/appearance", {"theme": "dark"}, page="/account")
    assert '<html lang="en" data-theme="dark">' in client.get("/account").get_data(as_text=True)
    post(client, "/account/appearance", {"theme": "auto"}, page="/account")
    assert "data-theme" not in client.get("/account").get_data(as_text=True).split("<head>")[0]


def test_unknown_choice_is_ignored(client, app):
    add_user(app)
    login(client)
    post(client, "/account/appearance", {"theme": "neon"}, page="/account")
    with db(app) as conn:
        assert conn.execute("SELECT theme FROM users").fetchone()[0] == "auto"


def test_choice_is_per_account(app):
    add_user(app)
    add_user(app, username="guest", role="user")
    a, b = app.test_client(), app.test_client()
    login(a)
    login(b, username="guest")
    post(a, "/account/appearance", {"theme": "light"}, page="/account")
    assert "data-theme" not in b.get("/account").get_data(as_text=True).split("<head>")[0]


def test_settings_page_points_to_it(client, app):
    add_user(app)
    login(client)
    assert "Account &gt; Appearance" in client.get("/admin/settings").get_data(as_text=True)
