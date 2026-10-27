# SPDX-License-Identifier: GPL-2.0-or-later
"""Helpers shared by the view modules."""
from __future__ import annotations

import ipaddress
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, g, redirect, request, url_for

from .. import activity, auth_cookie_name, load_points
from ..db import get_db
from ..sessions import MFA_STAGE_SECONDS

bp = Blueprint("main", __name__)


def cfg() -> dict:
    return g.cfg


def client_ip() -> str:
    return request.remote_addr or "unknown"


def user_agent() -> str:
    return request.user_agent.string[:200]


def io():
    return current_app.extensions["coilgate_io"]


def resync_io() -> None:
    io().sync(load_points(get_db()))


def admin_reachable() -> bool:
    """Is the Admin area allowed from this client's address?"""
    networks = g.cfg["admin_networks"] if "cfg" in g else []
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(client_ip())
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in networks if net.version == addr.version)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("main.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def requires(privilege: str):
    """For pages in the Admin area: only accounts whose group has this
    privilege (see groups.py), and only from allowed networks. Everyone else
    gets a 404, so the Admin area doesn't advertise itself."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("main.login", next=request.full_path.rstrip("?")))
            if not g.access.can(privilege) or not admin_reachable():
                abort(404)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def admin_area_required(view):
    """Any account that can use some part of the Admin area."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("main.login", next=request.full_path.rstrip("?")))
        if not g.access.admin_area or not admin_reachable():
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def safe_next(target: str | None) -> str:
    """Only allow redirects to paths on this site (no open redirects)."""
    default = url_for("main.controls")
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return default
    return target


def set_auth_cookie(resp, token: str, stage: str):
    c = cfg()
    max_age = MFA_STAGE_SECONDS if stage == "mfa" else c["session_absolute_hours"] * 3600
    resp.set_cookie(auth_cookie_name(c), token, max_age=max_age, path="/",
                    secure=c["secure_cookies"], httponly=True, samesite="Lax")
    return resp


def clear_auth_cookie(resp):
    c = cfg()
    resp.delete_cookie(auth_cookie_name(c), path="/", secure=c["secure_cookies"],
                       httponly=True, samesite="Lax")
    return resp


def get_user(db, *, username=None, user_id=None):
    if user_id is not None:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def audit(category: str, action: str, *, target=None, detail=None, io_id=None) -> None:
    """Record an action by the signed-in user."""
    activity.log(get_db(), category, action, target=target, detail=detail, io_id=io_id,
                 user_id=g.user["user_id"] if g.user else None,
                 username=g.user["username"] if g.user else None, ip=client_ip())


def rule_engine():
    return current_app.extensions["coilgate_rules"]


def reload_rules() -> None:
    rule_engine().reload()


def access_block():
    """The schedule stopping this account from operating controls right now,
    or None. Admins are never limited, so nobody can lock out the last admin."""
    from .. import schedules as sched
    user = g.user
    if user is None or g.access.is_admin or not user["schedule_id"]:
        return None
    schedule = sched.load_all(get_db()).get(user["schedule_id"])
    if schedule is None or schedule.is_active():
        return None
    return schedule
