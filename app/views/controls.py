# SPDX-License-Identifier: GPL-2.0-or-later
"""The Controls page: switch outputs, watch inputs and sensors.

The page works as plain HTML forms. With JavaScript (static/live.js), the same
forms are sent with fetch and answered in JSON, so the page doesn't reload or
jump, and the activity panel updates in place.
"""
from __future__ import annotations

from datetime import datetime

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .. import activity
from .. import schedules as sched
from ..db import get_db
from ..hardware import PHYSICAL_PIN, HardwareConfigError, describe_pulse
from .common import access_block, audit, bp, cfg, io, login_required


LOG_CATEGORIES = ("control", "input", "automation")
LOG_ROWS = 50
# How long /controls/state holds a request open waiting for a change. Below
# Cloudflare's 100-second limit for an idle response, with room to spare.
WAIT_SECONDS = 25


def short_when(ts: int) -> str:
    """Time of day (with seconds) for today, date and time for anything older."""
    moment = datetime.fromtimestamp(ts)
    if moment.date() == datetime.now().date():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%d %b %H:%M")


def log_entries(db, since: int = 0, limit: int = LOG_ROWS) -> list[dict]:
    """Newest first: what the activity panel on the Controls page shows."""
    marks = ",".join("?" * len(LOG_CATEGORIES))
    rows = db.execute(
        f"SELECT * FROM activity WHERE category IN ({marks}) AND id > ?"
        " ORDER BY id DESC LIMIT ?", (*LOG_CATEGORIES, since, limit)).fetchall()
    return [{
        "id": r["id"],
        "when": short_when(r["ts"]),
        "what": r["target"] or "",
        "change": activity.changed_to(r),
        "by": activity.done_by(r),
        "ip": r["ip"] or "",
    } for r in rows]


def wants_json() -> bool:
    return request.headers.get("Accept", "").startswith("application/json")


def _since() -> int:
    try:
        return max(0, int(request.values.get("since", "0")))
    except ValueError:
        return 0


def respond(message: str | None, level: str = "success", point_id: int | None = None):
    """Answer a Controls form: JSON for the page's script, otherwise a flash
    message and a redirect back to the same row."""
    if wants_json():
        manager = io()
        return jsonify(ok=level != "error", level=level, message=message, version=manager.version,
                       setup=manager.setup, points=manager.snapshot(),
                       activity=log_entries(get_db(), _since()))
    if message:
        flash(message, level)
    anchor = f"io-{point_id}" if point_id else None
    return redirect(url_for("main.controls", _anchor=anchor))


@bp.get("/controls")
@login_required
def controls():
    manager = io()
    db = get_db()
    automated = {r["output_id"]: r["n"] for r in db.execute(
        "SELECT output_id, COUNT(*) AS n FROM rules WHERE enabled = 1 GROUP BY output_id")}
    blocked = access_block()
    next_start = None
    if blocked:
        when = blocked.next_start()
        next_start = sched.describe_when(when) if when else None
    outputs, inputs, sensors = (manager.points(k) for k in ("output", "input", "sensor"))
    from .. import pinout
    return render_template(
        "controls.html",
        pin_rows=pinout.rows(manager.points()),
        physical=PHYSICAL_PIN,
        outputs=outputs,
        inputs=inputs,
        sensors=sensors,
        has_points=bool(outputs or inputs or sensors),
        io=manager,
        log=log_entries(db),
        version=manager.version,
        setup=manager.setup,
        simulated=manager.simulated,
        automated=automated,
        blocked=blocked,
        next_start=next_start,
        view_only=not g.access.can("control"),
        locked=bool(blocked) or not g.access.can("control"),
    )


@bp.get("/controls/state")
def controls_state():
    """Live state for the Controls page. Doesn't count as activity, so an open
    tab won't keep a session alive.

    With ?wait=1&v=<version>, the request waits until something changes (an
    input, an output, a reading) or WAIT_SECONDS pass, so the page hears about
    changes within a fraction of a second without asking over and over."""
    if g.user is None:
        return jsonify(error="signed out"), 401
    manager = io()
    if request.args.get("wait") == "1":
        try:
            seen = int(request.args.get("v", ""))
        except ValueError:
            seen = None
        if seen == manager.version:
            limit = current_app.config.get("COILGATE_WAIT_SECONDS", WAIT_SECONDS)
            # A running pulse shows a countdown, so answer sooner while one runs.
            remaining = [manager.pulse_remaining(p.id) for p in manager.points("output")
                         if manager.is_pulsing(p.id)]
            if remaining:
                limit = min(limit, 1 if min(remaining) < 90 else 5)
            manager.wait_for_change(seen, limit)
    # The Pinout page has no activity panel, so it asks for points only.
    entries = [] if request.args.get("activity") == "0" else log_entries(get_db(), _since())
    return jsonify(version=manager.version, setup=manager.setup, points=manager.snapshot(),
                   simulated=manager.simulated, activity=entries)


@bp.post("/controls/<int:point_id>")
@login_required
def control_action(point_id: int):
    manager = io()
    point = manager.get(point_id)
    if point is None or point.kind != "output":
        abort(404)

    if not g.access.can("control"):
        return respond(f"Your group, {g.access.group_name}, can see the controls but not "
                       "switch them.", "error", point.id)
    if blocked := access_block():
        return respond(f"Your access follows the {blocked.name} schedule, so the controls "
                       "aren't available right now.", "error", point.id)

    db = get_db()
    if activity.control_actions_last_minute(db, g.user["user_id"]) >= cfg()["max_control_actions_per_minute"]:
        return respond("Too many actions in the last minute. Wait a moment and try again.",
                       "error", point.id)

    action = request.form.get("do", "")
    try:
        if point.mode == "toggle" and action in ("on", "off"):
            manager.set(point.id, action == "on")
            message = f"{point.name} turned {action}."
        elif point.mode == "pulse" and action == "pulse":
            if not manager.pulse(point.id):
                return respond(f"{point.name} is already active.", "info", point.id)
            message = f"{point.name} activated for {describe_pulse(point.pulse_seconds)}."
        elif point.mode == "pulse" and action == "stop":
            manager.stop(point.id)
            message = f"{point.name} stopped."
        else:
            return respond(f"That action isn't available for {point.name}.", "error", point.id)
    except HardwareConfigError as exc:
        return respond(f"{point.name} isn't available: {exc}", "error", point.id)

    audit("control", action, target=point.name, io_id=point.id)
    return respond(message, "success", point.id)


@bp.post("/controls/<int:point_id>/simulate")
@login_required
def simulate_input(point_id: int):
    """Flip a simulated input (only when there's no GPIO hardware)."""
    manager = io()
    point = manager.get(point_id)
    if not manager.simulated or point is None or point.kind != "input":
        abort(404)
    if access_block() or not g.access.can("control"):
        return respond(None, "info", point.id)
    manager.simulate_input(point.id, not manager.is_on(point.id))
    return respond(f"{point.name}: simulated a change to {manager.state_text(point)}.",
                   "success", point.id)


@bp.post("/controls/<int:point_id>/simulate-reading")
@login_required
def simulate_reading(point_id: int):
    """Set a simulated sensor reading (only when there's no GPIO hardware)."""
    manager = io()
    point = manager.get(point_id)
    if not manager.simulated or point is None or point.kind != "sensor":
        abort(404)
    if access_block() or not g.access.can("control"):
        return respond(None, "info", point.id)
    try:
        value = float(request.form.get("value", "").replace(",", ".").strip())
    except ValueError:
        return respond("Enter a number for the simulated reading.", "error", point.id)
    if not -1000 <= value <= 10000:
        return respond("That reading is out of range.", "error", point.id)
    manager.simulate_reading(point.id, value)
    return respond(f"{point.name}: simulated reading {manager.state_text(point)}.",
                   "success", point.id)
