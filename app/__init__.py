# SPDX-License-Identifier: GPL-2.0-or-later
"""CoilGate: secure, self-hosted control of a Raspberry Pi's inputs and outputs."""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for

from . import activity
from . import db as database
from . import groups
from . import settings as runtime
from .config import INSTANCE_DIR, PRODUCT_NAME, ensure_private_dir, load_secret_key, load_settings
from .hardware import IOManager, IOPoint, describe_pulse, format_duration
from .sessions import load_session

log = logging.getLogger("coilgate")

CSP = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "connect-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
])

# Endpoints reachable while an account still has to finish setting up.
SETUP_ENDPOINTS = {"static", "main.logout", "main.change_password", "main.two_step"}


def auth_cookie_name(cfg: dict) -> str:
    # The __Host- prefix makes the browser refuse the cookie unless it is
    # Secure, has Path=/ and no Domain, so it can't be set by a sibling host.
    return "__Host-coilgate" if cfg["secure_cookies"] else "coilgate_auth"


def load_points(conn) -> list[IOPoint]:
    rows = conn.execute("SELECT * FROM io_points ORDER BY sort_order, id").fetchall()
    return [IOPoint.from_row(r) for r in rows]


def _apply_startup_settings(conn, cfg: dict) -> None:
    """Copy Gatehouse-era config.toml settings into the database (once), and
    apply settings passed to create_app (used by the tests)."""
    seeds = {}
    for old_key, value in cfg["legacy"].items():
        new_key = runtime.LEGACY_MAP.get(old_key)
        if new_key and not runtime.is_set(conn, new_key):
            try:
                seeds[new_key] = runtime.coerce(new_key, value)
            except ValueError as exc:
                log.warning("Ignoring %s from config.toml: %s", old_key, exc)
    if seeds:
        runtime.save(conn, seeds)
        log.warning("Moved %s from config.toml into the database. Change them in "
                    "Admin > Settings now, and remove them from config.toml.",
                    ", ".join(sorted(seeds)))

    overrides = {k: runtime.coerce(k, v) for k, v in cfg["runtime_overrides"].items()
                 if k in runtime.SPECS}
    if overrides:
        runtime.save(conn, overrides)

    # Two-step verification is required per group. A value passed to
    # create_app (the tests) applies to every group; Gatehouse's setting in
    # config.toml applies once, to Admin and User.
    if "require_2fa" in cfg["runtime_overrides"]:
        conn.execute("UPDATE user_groups SET require_2fa = ?",
                     (int(bool(cfg["runtime_overrides"]["require_2fa"])),))
        conn.commit()
    elif "require_2fa_for_controls" in cfg["legacy"] and \
            not runtime.get_flag(conn, "_imported_legacy_2fa"):
        conn.execute("UPDATE user_groups SET require_2fa = ? WHERE builtin IN ('admin', 'user')",
                     (int(bool(cfg["legacy"]["require_2fa_for_controls"])),))
        conn.commit()
        runtime.set_flag(conn, "_imported_legacy_2fa")

    outputs = cfg["legacy"].get("outputs") or []
    if outputs and not runtime.get_flag(conn, "_imported_config_outputs"):
        now = int(time.time())
        for i, o in enumerate(outputs):
            try:
                conn.execute(
                    "INSERT INTO io_points (kind, name, pin, active_high, mode, pulse_seconds,"
                    " sort_order, created_at, updated_at) VALUES ('output', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(o["name"])[:60], int(o["pin"]), int(bool(o.get("active_high", True))),
                     o.get("mode", "toggle"), float(o.get("pulse_seconds", 1.0)), i, now, now))
            except Exception as exc:
                log.warning("Couldn't import output %r from config.toml: %s", o, exc)
        conn.commit()
        runtime.set_flag(conn, "_imported_config_outputs")
        log.warning("Imported [[outputs]] from config.toml. They're now managed in "
                    "Admin > Inputs and outputs; remove them from config.toml.")


def create_app(overrides: dict | None = None) -> Flask:
    cfg = load_settings(overrides)
    app = Flask(__name__)
    ensure_private_dir(INSTANCE_DIR)

    app.config.update(
        COILGATE=cfg,
        SECRET_KEY=load_secret_key(),
        # Flask's own signed cookie only carries the CSRF token, flash messages
        # and a pending sign-in redirect. Authentication lives server-side.
        SESSION_COOKIE_NAME="__Host-coilgate-csrf" if cfg["secure_cookies"] else "coilgate_csrf",
        SESSION_COOKIE_SECURE=cfg["secure_cookies"],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=32 * 1024,
        TEMPLATES_AUTO_RELOAD=False,
    )

    # ------------------------------------------------------------ database
    database.adopt_gatehouse_database(cfg["database"])
    conn = database.connect(cfg["database"])
    database.init_schema(conn)
    _apply_startup_settings(conn, cfg)
    database.prune(conn, runtime.load(conn)["event_retention_days"])
    points = load_points(conn)
    conn.close()
    app.teardown_appcontext(database.close_db)

    # ------------------------------------------------------------ hardware
    recent_input_events: dict[int, deque] = defaultdict(lambda: deque(maxlen=30))
    input_lock = threading.Lock()

    def record_input_change(point: IOPoint, active: bool) -> None:
        # Runs on gpiozero's thread, so it uses its own connection. A wildly
        # chattering input is capped at 30 logged changes a minute.
        now = time.monotonic()
        with input_lock:
            stamps = recent_input_events[point.id]
            if len(stamps) == stamps.maxlen and now - stamps[0] < 60:
                return
            stamps.append(now)
        c = database.connect(cfg["database"])
        try:
            activity.log(c, "input", "active" if active else "inactive", target=point.name,
                         detail=point.active_text if active else point.inactive_text,
                         io_id=point.id)
        finally:
            c.close()

    io = IOManager(cfg["gpio_mode"], on_input_change=record_input_change)
    io.sync(points)
    app.extensions["coilgate_io"] = io

    # ------------------------------------------------------------ rules
    from .rules import RuleEngine
    engine = RuleEngine(cfg["database"], io)
    engine.reload()
    if cfg["rule_engine"]:
        engine.start()
    else:
        log.warning("rule_engine = false in config.toml: rules are paused.")
    io.listeners.append(engine.wake)         # inputs are acted on at once
    app.extensions["coilgate_rules"] = engine

    # ------------------------------------------------------------ per request
    @app.before_request
    def load_context():
        g.cfg = {**cfg, **runtime.load(database.get_db())}
        g.user = None       # fully signed-in session row
        g.access = groups.NO_ACCESS   # what g.user's group may do
        g.pending = None    # password accepted, waiting for the 2FA code
        g.auth_token = request.cookies.get(auth_cookie_name(cfg))
        if g.auth_token:
            touch = request.endpoint != "main.controls_state"
            row = load_session(database.get_db(), g.auth_token, g.cfg, touch=touch)
            if row is not None:
                if row["stage"] == "full":
                    g.user = row
                    g.access = groups.access_for(database.get_db(), row["group_id"])
                else:
                    g.pending = row

    @app.before_request
    def csrf_protect():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        # Fetch Metadata: modern browsers say where a request came from.
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            abort(403)
        expected = session.get("csrf")
        submitted = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected, submitted):
            abort(400)

    @app.before_request
    def finish_account_setup():
        """New accounts choose their own password first, then set up two-step
        verification if their group requires it, before anything else."""
        if g.user is None or request.endpoint in ("static", "main.logout"):
            return None
        if g.user["must_change_password"]:
            needed = "main.change_password"
        elif g.access.require_2fa and not g.user["totp_enabled"]:
            needed = "main.two_step"
        else:
            return None
        if request.endpoint == needed:
            return None
        if request.endpoint == "main.controls_state":
            return jsonify(error="account setup required"), 403
        return redirect(url_for(needed))

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.context_processor
    def template_globals():
        from .views.common import admin_reachable
        user = g.get("user")
        access = g.get("access", groups.NO_ACCESS)
        return {
            "csrf_token": csrf_token,
            "app_name": g.cfg["site_name"] if "cfg" in g else PRODUCT_NAME,
            "product_name": PRODUCT_NAME,
            "cfg": g.get("cfg", cfg),
            "access": access,
            "can": access.can,
            "is_admin": bool(user and access.is_admin),
            "show_admin": bool(user and access.admin_area and admin_reachable()),
            "setup_pending": bool(user and (user["must_change_password"] or (
                access.require_2fa and not user["totp_enabled"]))),
        }

    @app.template_filter("when")
    def format_when(ts: int | None) -> str:
        if not ts:
            return "never"
        return datetime.fromtimestamp(ts).strftime("%d %b %Y, %H:%M")

    @app.template_filter("ago")
    def format_ago(ts: int | None) -> str:
        if not ts:
            return "never"
        seconds = int(datetime.now().timestamp()) - ts
        for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
            if seconds >= size:
                n = seconds // size
                return f"{n} {unit}{'s' if n != 1 else ''} ago"
        return "just now"

    from .activity import describe_device
    app.add_template_filter(describe_device, "device")

    app.add_template_filter(describe_pulse, "pulse")
    app.add_template_filter(format_duration, "duration")

    @app.after_request
    def security_headers(resp):
        h = resp.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        if cfg["secure_cookies"]:
            h["Strict-Transport-Security"] = "max-age=31536000"
        if not request.path.startswith("/static/"):
            h["Cache-Control"] = "no-store"
        return resp

    errors = {
        400: ("This form expired", "Go back, reload the page and try again."),
        403: ("Request blocked", "This request didn't come from this site, so it was refused."),
        404: ("Page not found", "Check the address, or go back to the start."),
        405: ("Not allowed", "That page doesn't accept this kind of request."),
        413: ("Request too large", "The form sent more data than this site accepts."),
        500: ("Something went wrong", "The error has been logged. Try again in a moment."),
    }
    for code, (title, detail) in errors.items():
        def handler(_e, code=code, title=title, detail=detail):
            # The Controls page's script asks for JSON; give it a message it can show.
            if request.path.endswith("/state") or \
                    request.headers.get("Accept", "").startswith("application/json"):
                message = ("This page has expired. Reload it and try again." if code == 400
                           else f"{title}. {detail}")
                return jsonify(error=title, ok=False, level="error", message=message), code
            return render_template("error.html", title=title, detail=detail, code=code), code
        app.register_error_handler(code, handler)

    from .views import bp
    app.register_blueprint(bp)
    return app
