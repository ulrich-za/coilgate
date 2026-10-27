# SPDX-License-Identifier: GPL-2.0-or-later
"""Your account: sessions, sign-in history, password and two-step verification."""
from __future__ import annotations

import time

from flask import flash, g, redirect, render_template, request, session, url_for

from .. import passwords, twofactor
from .. import sessions as sess
from ..db import get_db
from .common import bp, cfg, client_ip, get_user, login_required, set_auth_cookie, user_agent


@bp.get("/dashboard")
def dashboard():
    # Gatehouse's landing page; kept so old bookmarks still work.
    return redirect(url_for("main.controls"))


@bp.get("/account")
@login_required
def account():
    db = get_db()
    user_id = g.user["user_id"]
    sessions_list = sess.active_sessions(db, user_id, cfg())
    events = db.execute(
        "SELECT ts, ip, event, success, user_agent FROM auth_events"
        " WHERE user_id = ? AND event IN ('login', 'mfa', 'reauth', 'password_changed',"
        " 'totp_enabled', 'totp_disabled')"
        " ORDER BY ts DESC LIMIT 8",
        (user_id,),
    ).fetchall()
    previous = session.get("previous_login") or [None, None]
    from .. import schedules as sched
    access = None
    if not g.access.is_admin and g.user["schedule_id"]:
        s = sched.load_all(db).get(g.user["schedule_id"])
        if s is not None:
            when = s.next_start()
            access = {"name": s.name, "active": s.is_active(),
                      "next": sched.describe_when(when) if when else None}
    return render_template(
        "account.html",
        access=access,
        themes=THEMES,
        sessions=sessions_list,
        events=events,
        current_hash=sess.hash_token(g.auth_token),
        previous_login_at=previous[0],
        previous_login_ip=previous[1],
    )


THEMES = {"auto": "Auto", "light": "Light", "dark": "Dark"}


@bp.post("/account/appearance")
@login_required
def appearance():
    theme = request.form.get("theme", "")
    if theme in THEMES:
        db = get_db()
        db.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, g.user["user_id"]))
        db.commit()
        flash(f"Appearance set to {THEMES[theme].lower()}." if theme != "auto"
              else "Appearance now follows your device's setting.", "success")
    return redirect(url_for("main.account") + "#appearance-heading")


@bp.post("/sessions/revoke")
@login_required
def revoke_session():
    db = get_db()
    public_id = request.form.get("session_id", "")
    if public_id == "others":
        n = sess.revoke_other_sessions(db, g.user["user_id"], g.auth_token)
        flash(f"Signed out {n} other session{'s' if n != 1 else ''}.", "success")
    elif sess.revoke_by_public_id(db, g.user["user_id"], public_id):
        flash("Session signed out.", "success")
    return redirect(url_for("main.account"))


def check_reauth(password: str) -> bool:
    """Confirm the current password for sensitive changes, with throttling."""
    db = get_db()
    user = get_user(db, user_id=g.user["user_id"])
    if sess.is_throttled(db, user["username"], client_ip(), cfg()):
        return False
    ok, _ = passwords.verify_password(user["password_hash"], password)
    if not ok:
        sess.log_event(db, "reauth", False, user_id=user["id"], username=user["username"],
                       ip=client_ip(), user_agent=user_agent())
    return ok


def rotate_current_session(resp):
    """New token for this browser after a credential change; old one dies."""
    db = get_db()
    sess.delete_session(db, g.auth_token)
    token = sess.create_session(db, g.user["user_id"], "full", cfg(), client_ip(), user_agent())
    return set_auth_cookie(resp, token, "full")


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    c = cfg()
    forced = bool(g.user["must_change_password"])
    if request.method != "POST":
        return render_template("password.html", problems=[], forced=forced)

    db = get_db()
    user = get_user(db, user_id=g.user["user_id"])
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    # Someone who just signed in with a temporary password has proved it
    # seconds ago; everyone else confirms their current password.
    if not forced and not check_reauth(request.form.get("current_password", "")):
        return render_template("password.html", forced=forced,
                               problems=["Your current password is incorrect."]), 400

    problems = passwords.policy_problems(new, user["username"], c["site_name"],
                                         c["min_password_length"])
    if new != confirm:
        problems.append("The new passwords don't match.")
    if not problems and passwords.verify_password(user["password_hash"], new)[0]:
        problems.append("Choose a password different from your current one.")
    if not problems and c["check_pwned_passwords"]:
        count = passwords.pwned_count(new)
        if count:
            problems.append(f"This password has appeared in {count:,} data breaches. Choose another.")
    if problems:
        return render_template("password.html", problems=problems, forced=forced), 400

    db.execute("UPDATE users SET password_hash = ?, password_changed_at = ?,"
               " must_change_password = 0, temp_password_expires_at = NULL WHERE id = ?",
               (passwords.hash_password(new), int(time.time()), user["id"]))
    db.commit()
    revoked = sess.revoke_other_sessions(db, user["id"], g.auth_token)
    sess.log_event(db, "password_changed", True, user_id=user["id"],
                   username=user["username"], ip=client_ip(), user_agent=user_agent())
    if forced:
        flash("Password set.", "success")
    else:
        note = (f" {revoked} other session{'s were' if revoked != 1 else ' was'} signed out."
                if revoked else "")
        flash("Password changed." + note, "success")
    return rotate_current_session(redirect(url_for("main.controls" if forced else "main.account")))


@bp.route("/account/two-step", methods=["GET", "POST"])
@login_required
def two_step():
    db = get_db()
    c = cfg()
    user = get_user(db, user_id=g.user["user_id"])
    required = g.access.require_2fa

    if user["totp_enabled"]:
        return render_template("two_step.html", enabled=True, required=required, error=None)

    secret = user["totp_secret"]
    if not secret:
        secret = twofactor.new_secret()
        db.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (secret, user["id"]))
        db.commit()

    error = None
    if request.method == "POST":
        step = twofactor.verify(secret, request.form.get("code", ""), user["totp_last_step"])
        if step is None:
            error = ("That code didn't match. Check the time on your phone is set "
                     "automatically, then try the newest code.")
        else:
            db.execute("UPDATE users SET totp_enabled = 1, totp_last_step = ? WHERE id = ?",
                       (step, user["id"]))
            db.commit()
            sess.revoke_other_sessions(db, user["id"], g.auth_token)
            sess.log_event(db, "totp_enabled", True, user_id=user["id"],
                           username=user["username"], ip=client_ip(), user_agent=user_agent())
            flash("Two-step verification is on. You'll need a code from your app at each "
                  "sign-in.", "success")
            return rotate_current_session(redirect(url_for("main.controls")))

    uri = twofactor.provisioning_uri(secret, user["username"], c["site_name"])
    return render_template("two_step.html", enabled=False, required=required, error=error,
                           qr=twofactor.qr_data_uri(uri), secret=twofactor.grouped(secret)), \
        (400 if error else 200)


@bp.post("/account/two-step/disable")
@login_required
def two_step_disable():
    db = get_db()
    user = get_user(db, user_id=g.user["user_id"])
    if not user["totp_enabled"]:
        return redirect(url_for("main.two_step"))
    if g.access.require_2fa:
        flash(f"Your group, {g.access.group_name}, needs two-step verification, so it can't be "
              "turned off. If you've lost your phone, an admin can reset it.", "error")
        return redirect(url_for("main.two_step"))

    step = twofactor.verify(user["totp_secret"], request.form.get("code", ""),
                            user["totp_last_step"])
    if not check_reauth(request.form.get("current_password", "")) or step is None:
        return render_template("two_step.html", enabled=True, required=False,
                               error="Your password or code was incorrect."), 400

    db.execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL WHERE id = ?",
               (user["id"],))
    db.commit()
    sess.revoke_other_sessions(db, user["id"], g.auth_token)
    sess.log_event(db, "totp_disabled", True, user_id=user["id"], username=user["username"],
                   ip=client_ip(), user_agent=user_agent())
    flash("Two-step verification is off.", "success")
    return rotate_current_session(redirect(url_for("main.account")))
