# SPDX-License-Identifier: GPL-2.0-or-later
"""Signing in and out."""
from __future__ import annotations

import time

from flask import flash, g, redirect, render_template, request, session, url_for

from .. import passwords, twofactor
from .. import sessions as sess
from ..db import get_db, prune
from .common import (bp, cfg, clear_auth_cookie, client_ip, get_user, safe_next,
                     set_auth_cookie, user_agent)


def start_full_session(user, next_url: str):
    """Issue a brand-new session (never reuse a pre-login one: session fixation)."""
    db = get_db()
    sess.delete_session(db, g.auth_token)
    session.clear()                       # also rotates the CSRF token
    token = sess.create_session(db, user["id"], "full", cfg(), client_ip(), user_agent())
    db.execute("UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
               (int(time.time()), client_ip(), user["id"]))
    db.commit()
    sess.log_event(db, "login", True, user_id=user["id"], username=user["username"],
                   ip=client_ip(), user_agent=user_agent())
    prune(db, cfg()["event_retention_days"])
    session["previous_login"] = [user["last_login_at"], user["last_login_ip"]]
    return set_auth_cookie(redirect(safe_next(next_url)), token, "full")


@bp.get("/")
def index():
    return redirect(url_for("main.controls" if g.user else "main.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("main.controls"))
    next_url = request.values.get("next", "")

    if request.method != "POST":
        return render_template("login.html", state="idle", next=next_url, username="")

    db = get_db()
    c = cfg()
    username = passwords.normalize_username(request.form.get("username", ""))[:64]
    password = request.form.get("password", "")
    ip = client_ip()

    if sess.is_throttled(db, username, ip, c):
        sess.log_event(db, "login_throttled", False, username=username, ip=ip,
                       user_agent=user_agent())
        return render_template("login.html", state="locked", next=next_url,
                               username=username), 429

    user = get_user(db, username=username) if username else None
    usable = user is not None and user["is_active"]
    ok, needs_rehash = passwords.verify_password(user["password_hash"] if usable else None,
                                                 password)
    if not ok:
        sess.log_event(db, "login", False, user_id=user["id"] if user else None,
                       username=username, ip=ip, user_agent=user_agent())
        return render_template("login.html", state="denied", next=next_url,
                               username=username), 401

    # A temporary password from an admin only works for a limited time.
    expires = user["temp_password_expires_at"]
    if user["must_change_password"] and expires and time.time() > expires:
        sess.log_event(db, "temp_password_expired", False, user_id=user["id"],
                       username=username, ip=ip, user_agent=user_agent())
        return render_template("login.html", state="expired", next=next_url,
                               username=username), 401

    if needs_rehash:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (passwords.hash_password(password), user["id"]))
        db.commit()

    if user["totp_enabled"]:
        sess.delete_session(db, g.auth_token)
        session.clear()
        session["next"] = next_url
        token = sess.create_session(db, user["id"], "mfa", c, ip, user_agent())
        sess.log_event(db, "password_ok", True, user_id=user["id"], username=username,
                       ip=ip, user_agent=user_agent())
        return set_auth_cookie(redirect(url_for("main.login_2fa")), token, "mfa")

    return start_full_session(user, next_url)


@bp.route("/login/verify", methods=["GET", "POST"])
def login_2fa():
    if g.user is not None:
        return redirect(url_for("main.controls"))
    pending = g.pending
    if pending is None:
        flash("Your sign-in timed out. Enter your password again.", "info")
        return redirect(url_for("main.login"))

    if request.method != "POST":
        return render_template("login_2fa.html", state="waiting")

    db = get_db()
    c = cfg()
    ip = client_ip()
    if sess.is_throttled(db, pending["username"], ip, c):
        sess.delete_session(db, g.auth_token)
        sess.log_event(db, "login_throttled", False, user_id=pending["user_id"],
                       username=pending["username"], ip=ip, user_agent=user_agent())
        flash("Too many failed attempts. Wait a few minutes before trying again.", "error")
        return clear_auth_cookie(redirect(url_for("main.login")))

    step = twofactor.verify(pending["totp_secret"], request.form.get("code", ""),
                            pending["totp_last_step"])
    if step is None:
        sess.log_event(db, "mfa", False, user_id=pending["user_id"],
                       username=pending["username"], ip=ip, user_agent=user_agent())
        failures = sess.record_mfa_failure(db, g.auth_token)
        if failures >= sess.MAX_MFA_FAILURES:
            sess.delete_session(db, g.auth_token)
            flash("Too many incorrect codes. Sign in again.", "error")
            return clear_auth_cookie(redirect(url_for("main.login")))
        return render_template("login_2fa.html", state="denied"), 401

    # Record the used step only if nobody else used it first (replay guard).
    updated = db.execute(
        "UPDATE users SET totp_last_step = ? WHERE id = ? AND totp_last_step < ?",
        (step, pending["user_id"], step),
    ).rowcount
    db.commit()
    if not updated:
        return render_template("login_2fa.html", state="denied"), 401

    user = get_user(db, user_id=pending["user_id"])
    return start_full_session(user, session.get("next", ""))


@bp.post("/logout")
def logout():
    db = get_db()
    if g.user is not None:
        sess.log_event(db, "logout", True, user_id=g.user["user_id"],
                       username=g.user["username"], ip=client_ip(), user_agent=user_agent())
    sess.delete_session(db, g.auth_token)
    session.clear()
    flash("You're signed out.", "info")
    return clear_auth_cookie(redirect(url_for("main.login")))
