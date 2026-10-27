# SPDX-License-Identifier: GPL-2.0-or-later
"""Sign-in, sessions, CSRF, two-step verification, password rules."""
from __future__ import annotations

import re
import time

import pyotp
import pytest
from argon2 import PasswordHasher

from app import passwords
from conftest import (PASSWORD, add_user, auth_cookie, csrf, db, enable_2fa, login, logout)


# ---------------------------------------------------------------- basics

def test_root_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code == 302 and resp.location.endswith("/login")


def test_security_headers(client):
    h = client.get("/login").headers
    csp = h["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert h["X-Frame-Options"] == "DENY"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in h               # only over HTTPS


def test_pages_require_login(client):
    for path in ("/controls", "/account", "/admin/io"):
        resp = client.get(path)
        assert resp.status_code == 302 and "/login?next=" in resp.location


def test_head_requests_are_not_sign_in_attempts(app, client):
    for path in ("/login", "/login/verify", "/account/password"):
        client.head(path)
    assert client.head("/login").status_code == 200
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auth_events").fetchone()[0] == 0


# ---------------------------------------------------------------- CSRF

def test_post_without_csrf_token_is_rejected(app, client):
    add_user(app)
    client.get("/login")
    resp = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 400


def test_cross_site_post_is_rejected(app, client):
    add_user(app)
    assert login(client, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


# ---------------------------------------------------------------- sign in

def test_successful_login_lands_on_controls(app, client):
    add_user(app)
    resp = login(client)
    assert resp.status_code == 302 and resp.location.endswith("/controls")
    set_cookie = [v for k, v in resp.headers.items() if k == "Set-Cookie" and "coilgate_auth" in v][0]
    assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
    page = client.get("/controls")
    assert page.status_code == 200 and "<h1>Controls</h1>" in page.get_data(as_text=True)
    assert "Signed in as admin" in client.get("/account").get_data(as_text=True)


def test_username_is_case_insensitive(app, client):
    add_user(app)
    assert login(client, username="  ADMIN ").status_code == 302


def test_wrong_password_and_unknown_user_look_identical(app, client):
    add_user(app)
    wrong = login(client, password="not the password at all")
    unknown = login(client, username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert "Incorrect username or password." in wrong.get_data(as_text=True)
    assert "Incorrect username or password." in unknown.get_data(as_text=True)


def test_disabled_user_cannot_sign_in(app, client):
    add_user(app)
    with db(app) as conn:
        conn.execute("UPDATE users SET is_active = 0")
    assert login(client).status_code == 401


def test_session_token_rotates_on_login(app, client):
    add_user(app)
    client.set_cookie("coilgate_auth", "attacker-chosen-value")
    login(client)
    first = auth_cookie(client)
    assert first and first != "attacker-chosen-value"
    logout(client)
    login(client)
    assert auth_cookie(client) != first


def test_only_token_hashes_are_stored(app, client):
    add_user(app)
    login(client)
    token = auth_cookie(client)
    with db(app) as conn:
        stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")]
    assert token not in stored and len(stored[0]) == 64


def test_open_redirect_is_blocked(app, client):
    add_user(app)
    assert login(client, next_url="//evil.example/").location.endswith("/controls")
    logout(client)
    assert login(client, next_url="https://evil.example/").location.endswith("/controls")
    logout(client)
    assert login(client, next_url="/account/password").location.endswith("/account/password")


def test_old_hash_is_upgraded_on_login(app, client):
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    add_user(app, password_hash=weak.hash(PASSWORD))
    assert login(client).status_code == 302
    with db(app) as conn:
        new_hash = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert "m=65536" in new_hash and "argon2id" in new_hash


# ---------------------------------------------------------------- throttling

def test_account_locks_after_repeated_failures(app, client):
    add_user(app)
    for _ in range(5):
        assert login(client, password="wrong password guess").status_code == 401
    locked = login(client)
    assert locked.status_code == 429
    assert "Too many failed attempts" in locked.get_data(as_text=True)


def test_unknown_usernames_lock_too(app, client):
    for _ in range(5):
        login(client, username="ghost", password="whatever it is")
    assert login(client, username="ghost", password="whatever it is").status_code == 429


def test_admin_unlock_clears_user_lock(app, client):
    from app.sessions import log_event
    add_user(app)
    for _ in range(5):
        login(client, password="wrong password guess")
    with db(app) as conn:
        log_event(conn, "admin_unlock", True, username="admin")
    assert login(client).status_code == 302


def test_ip_limit_applies_across_usernames(make_app):
    app = make_app(max_failures_per_ip=6)
    client = app.test_client()
    add_user(app)
    for i in range(6):
        login(client, username=f"spray{i}", password="Summer2026Summer!")
    assert login(client).status_code == 429


# ---------------------------------------------------------------- sessions

def test_logout_invalidates_session_server_side(app, client):
    add_user(app)
    login(client)
    token = auth_cookie(client)
    logout(client)
    client.set_cookie("coilgate_auth", token)
    assert client.get("/controls").status_code == 302


def test_idle_timeout(app, client):
    add_user(app)
    login(client)
    with db(app) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = last_seen_at - 31 * 60")
    assert client.get("/controls").status_code == 302


def test_absolute_timeout(app, client):
    add_user(app)
    login(client)
    with db(app) as conn:
        conn.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
    assert client.get("/controls").status_code == 302


def test_live_state_polling_does_not_keep_session_alive(app, client):
    add_user(app)
    login(client)
    with db(app) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = last_seen_at - 120")
        before = conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]
    assert client.get("/controls/state").status_code == 200
    with db(app) as conn:
        assert conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0] == before
    client.get("/controls")                         # a real page view does count
    with db(app) as conn:
        assert conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0] > before


def test_live_state_when_signed_out_is_401(client):
    assert client.get("/controls/state").status_code == 401


def test_revoke_other_session(app):
    add_user(app)
    laptop, phone = app.test_client(), app.test_client()
    login(laptop)
    login(phone)
    resp = laptop.post("/sessions/revoke", data={"csrf_token": csrf(laptop, "/account"),
                                                 "session_id": "others"})
    assert resp.status_code == 302
    assert laptop.get("/controls").status_code == 200
    assert phone.get("/controls").status_code == 302


# ---------------------------------------------------------------- password change

def test_change_password(app):
    add_user(app)
    me, other = app.test_client(), app.test_client()
    login(me)
    login(other)
    token = csrf(me, "/account/password")
    new = "lantern orbit gravel pepper"

    bad_current = me.post("/account/password", data={
        "csrf_token": token, "current_password": "nope nope nope nope",
        "new_password": new, "confirm_password": new})
    assert bad_current.status_code == 400

    weak = me.post("/account/password", data={
        "csrf_token": token, "current_password": PASSWORD,
        "new_password": "passwordpassword", "confirm_password": "passwordpassword"})
    assert weak.status_code == 400

    same = me.post("/account/password", data={
        "csrf_token": token, "current_password": PASSWORD,
        "new_password": PASSWORD, "confirm_password": PASSWORD})
    assert same.status_code == 400 and "different from your current" in same.get_data(as_text=True)

    ok = me.post("/account/password", data={
        "csrf_token": token, "current_password": PASSWORD,
        "new_password": new, "confirm_password": new})
    assert ok.status_code == 302
    assert me.get("/controls").status_code == 200
    assert other.get("/controls").status_code == 302

    logout(me)
    assert login(me, password=new).status_code == 302


# ---------------------------------------------------------------- two-step verification

def test_two_step_flow(app, client):
    add_user(app)
    login(client)
    secret = enable_2fa(app, client)
    logout(client)

    resp = login(client)
    assert resp.status_code == 302 and resp.location.endswith("/login/verify")
    assert client.get("/controls").status_code == 302

    token = csrf(client, "/login/verify")
    assert client.post("/login/verify", data={"csrf_token": token, "code": "000000"}).status_code == 401

    code = pyotp.TOTP(secret).now()
    ok = client.post("/login/verify", data={"csrf_token": token, "code": code})
    assert ok.status_code == 302 and ok.location.endswith("/controls")

    logout(client)
    login(client)
    token = csrf(client, "/login/verify")
    replay = client.post("/login/verify", data={"csrf_token": token, "code": code})
    assert replay.status_code == 401


def test_too_many_wrong_codes_restarts_sign_in(app, client):
    add_user(app)
    login(client)
    enable_2fa(app, client)
    logout(client)
    login(client)
    token = csrf(client, "/login/verify")
    for _ in range(4):
        client.post("/login/verify", data={"csrf_token": token, "code": "123456"})
    last = client.post("/login/verify", data={"csrf_token": token, "code": "123456"})
    assert last.status_code == 302 and last.location.endswith("/login")


def test_pending_2fa_session_cannot_reach_pages(app, client):
    add_user(app)
    login(client)
    enable_2fa(app, client)
    logout(client)
    login(client)
    for path in ("/controls", "/account/password", "/admin/io"):
        assert client.get(path).status_code == 302


# ---------------------------------------------------------------- HTTPS mode

def test_secure_mode_cookies_and_hsts(make_app):
    app = make_app(secure_cookies=True)
    client = app.test_client()
    add_user(app)
    resp = client.get("/login", base_url="https://localhost")
    assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]
    token = re.search(r'name="csrf_token" value="([^"]+)"', resp.get_data(as_text=True)).group(1)
    resp = client.post("/login", base_url="https://localhost",
                       data={"csrf_token": token, "username": "admin", "password": PASSWORD})
    cookies = [v for k, v in resp.headers.items() if k == "Set-Cookie"]
    auth = [c for c in cookies if c.startswith("__Host-coilgate=")][0]
    assert "Secure" in auth and "Path=/" in auth and "Domain" not in auth


# ---------------------------------------------------------------- password rules

@pytest.mark.parametrize("pw", [
    "short", "passwordpassword", "Password12345678!", "abcdefghijklmnop",
    "123456789012345", "aaaaaaaaaaaaaaaa", "qwertyuiopasdfgh", "admin-is-my-name-here",
])
def test_policy_rejects_weak_passwords(pw):
    assert passwords.policy_problems(pw, "admin", "CoilGate", 15)


@pytest.mark.parametrize("pw", [
    "correct horse battery staple", "lantern orbit gravel pepper",
    "Tr0ub4dor&3-cactus-violin", "kQ8#vL2!pZ9@wX4$",
])
def test_policy_accepts_strong_passwords(pw):
    assert passwords.policy_problems(pw, "admin", "CoilGate", 15) == []


def test_temporary_passwords_are_long_and_readable():
    seen = {passwords.generate_temporary_password() for _ in range(50)}
    assert len(seen) == 50
    sample = seen.pop()
    assert re.fullmatch(r"[a-z2-9]{5}(-[a-z2-9]{5}){3}", sample)
    assert not set(sample) & set("01lio")


def test_pwned_check_parses_range_response(monkeypatch):
    import hashlib
    import io as stdio
    digest = hashlib.sha1(b"password123").hexdigest().upper()
    body = f"0018A45C4D1DEF81644B54AB7F969B88D65:3\r\n{digest[5:]}:2254650\r\nFFFF0000:0\r\n"

    class FakeResponse(stdio.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout):
        assert len(req.full_url.rsplit("/", 1)[1]) == 5       # only 5 hex chars leave
        return FakeResponse(body.encode())

    monkeypatch.setattr(passwords.urllib.request, "urlopen", fake_urlopen)
    assert passwords.pwned_count("password123") == 2254650
    assert passwords.pwned_count("something else entirely here") == 0
