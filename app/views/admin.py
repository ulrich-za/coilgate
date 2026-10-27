# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin area: accounts, inputs and outputs, settings, activity log."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

from .. import activity, groups, passwords
from .. import sessions as sess
from .. import settings as runtime
from ..db import get_db
from .. import schedules as sched
from ..hardware import (MAX_PULSE_SECONDS, MIN_PULSE_SECONDS, PHYSICAL_PIN, PIN_NOTES,
                        VALID_PINS, WIRING, default_pull, describe_pulse)
from ..sensors import (QUANTITIES, SENSOR_TYPES, SensorError, discover_dht, discover_ds18b20,
                       format_value, parse_i2c_address, read_ds18b20)
from .common import (admin_area_required, audit, bp, cfg, get_user, io, reload_rules,
                     requires, resync_io)

TEMP_PASSWORD_HOURS = 72
PULSE_UNITS = {"s": 1, "min": 60, "h": 3600}


@bp.get("/admin")
@admin_area_required
def admin_home():
    for key, endpoint in groups.ADMIN_TABS.items():
        if g.access.can(key):
            return redirect(url_for(endpoint))
    abort(404)


# ======================================================================= accounts
# Who may manage which account is decided by groups: an account's group must
# be one the signed-in person's group manages (admins manage every group).

def active_admin_count(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM users u JOIN user_groups gr ON gr.id = u.group_id"
        " WHERE gr.builtin = 'admin' AND u.is_active = 1").fetchone()[0]


def issue_temp_password(db, user_id: int) -> str:
    temp = passwords.generate_temporary_password()
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1,"
        " temp_password_expires_at = ?, password_changed_at = ? WHERE id = ?",
        (passwords.hash_password(temp), int(time.time()) + TEMP_PASSWORD_HOURS * 3600,
         int(time.time()), user_id))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    db.commit()
    return temp


def assignable_groups(all_groups: dict) -> list:
    """The groups the signed-in person may put accounts in."""
    return [gr for gr in all_groups.values() if g.access.can_manage(gr.id)]


def group_summaries(all_groups: dict) -> dict[int, str]:
    return {gid: "; ".join(groups.summary(gr, all_groups)) + "." for gid, gr in all_groups.items()}


@bp.get("/admin/users")
@requires("accounts")
def admin_users():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("admin_users.html", users=users, schedules=sched.load_all(db),
                           groups=groups.load_all(db))


def schedule_choices():
    return list(sched.load_all(get_db()).values())


def parse_schedule_choice(value) -> int | None:
    """None means Always. Returns -1 for an unknown schedule."""
    schedules = sched.load_all(get_db())
    if value in (None, ""):
        return None
    try:
        sid = int(value)
    except (TypeError, ValueError):
        return -1
    s = schedules.get(sid)
    if s is None:
        return -1
    return None if s.builtin else s.id


def parse_group_choice(value, choices: list):
    try:
        gid = int(value)
    except (TypeError, ValueError):
        return None
    return next((gr for gr in choices if gr.id == gid), None)


@bp.route("/admin/users/new", methods=["GET", "POST"])
@requires("accounts")
def admin_user_new():
    db = get_db()
    all_groups = groups.load_all(db)
    choices = assignable_groups(all_groups)
    ids = groups.builtin_ids(db)
    default = next((gr for gr in choices if gr.id == ids.get(groups.USER)), choices[0])

    def form(error=None, username="", group=default, schedule_id=None, status=200):
        return render_template("admin_user_new.html", error=error, username=username,
                               group_id=group.id, group_choices=choices,
                               summaries=group_summaries(all_groups),
                               schedules=schedule_choices(), schedule_id=schedule_id), status

    if request.method != "POST":
        return form()

    username = passwords.normalize_username(request.form.get("username", ""))
    group = parse_group_choice(request.form.get("group_id"), choices)
    error = None
    if not passwords.USERNAME_RE.match(username):
        error = "Usernames are 2 to 32 characters: lowercase letters, digits, dots, dashes or underscores."
    elif group is None:
        error = "Choose a group."
    elif get_user(db, username=username):
        error = f"There's already an account called {username}."
    schedule_id = parse_schedule_choice(request.form.get("schedule_id"))
    if schedule_id == -1:
        error = error or "Choose an access schedule."
    if group is not None and group.is_admin:
        schedule_id = None
    if error:
        return form(error, username, group or default, schedule_id, 400)

    now = int(time.time())
    temp = passwords.generate_temporary_password()
    db.execute(
        "INSERT INTO users (username, password_hash, group_id, schedule_id, must_change_password,"
        " temp_password_expires_at, created_at, password_changed_at)"
        " VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
        (username, passwords.hash_password(temp), group.id, schedule_id,
         now + TEMP_PASSWORD_HOURS * 3600, now, now))
    db.commit()
    audit("user", "created", target=username, detail=f"Group: {group.name}")
    return render_template("admin_user_password.html", username=username, temp=temp,
                           hours=TEMP_PASSWORD_HOURS, new_account=True,
                           needs_2fa=group.require_2fa)


@bp.route("/admin/users/<int:user_id>", methods=["GET", "POST"])
@requires("accounts")
def admin_user_edit(user_id: int):
    db = get_db()
    user = get_user(db, user_id=user_id)
    # Accounts in groups they don't manage are out of reach, even by address.
    if user is None or not g.access.can_manage(user["group_id"]):
        abort(404)
    is_self = user["id"] == g.user["user_id"]
    all_groups = groups.load_all(db)
    group = all_groups.get(user["group_id"])
    choices = assignable_groups(all_groups)
    schedules = schedule_choices()

    def page(error=None, status=200):
        return render_template("admin_user_edit.html", user=user, is_self=is_self, error=error,
                               schedules=schedules, group=group, group_choices=choices,
                               summaries=group_summaries(all_groups)), status

    if request.method != "POST":
        return page()

    action = request.form.get("do", "")
    last_admin = (group is not None and group.is_admin and user["is_active"]
                  and active_admin_count(db) <= 1)

    def refuse(message: str):
        return page(message, 400)

    if action == "group":
        new = parse_group_choice(request.form.get("group_id"), choices)
        if new is None:
            return refuse("Choose a group.")
        if group is not None and new.id == group.id:
            return redirect(url_for("main.admin_user_edit", user_id=user_id))
        if is_self:
            return refuse("You can't change your own group. Ask an admin.")
        if last_admin and not new.is_admin:
            return refuse("This is the only admin. Make someone else an admin first.")
        # Admins aren't limited by schedules, so moving to Admin clears any schedule.
        db.execute("UPDATE users SET group_id = ?, schedule_id = CASE WHEN ? THEN NULL"
                   " ELSE schedule_id END WHERE id = ?", (new.id, int(new.is_admin), user_id))
        # Their sessions continue with the new group's privileges from the next page.
        db.commit()
        audit("user", "group", target=user["username"], detail=f"Now in {new.name}")
        flash(f"{user['username']} is now in the {new.name} group.", "success")

    elif action == "schedule":
        if group is not None and group.is_admin:
            return refuse("Admins can use the controls at any time; schedules apply to other groups.")
        schedule_id = parse_schedule_choice(request.form.get("schedule_id"))
        if schedule_id == -1:
            return refuse("Choose an access schedule.")
        db.execute("UPDATE users SET schedule_id = ? WHERE id = ?", (schedule_id, user_id))
        db.commit()
        name = next((x.name for x in schedules if x.id == schedule_id), sched.ALWAYS_NAME)
        audit("user", "schedule", target=user["username"], detail=f"Access: {name}")
        flash(f"{user['username']} can now use the controls: {name}.", "success")

    elif action in ("disable", "enable"):
        if is_self:
            return refuse("You can't disable your own account.")
        if action == "disable" and last_admin:
            return refuse("This is the only admin. Make someone else an admin first.")
        db.execute("UPDATE users SET is_active = ? WHERE id = ?",
                   (int(action == "enable"), user_id))
        if action == "disable":
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.commit()
        audit("user", f"{action}d", target=user["username"])
        flash(f"{user['username']} is {action}d.", "success")

    elif action == "reset_password":
        if is_self:
            return refuse("Change your own password from your account page.")
        temp = issue_temp_password(db, user_id)
        audit("user", "password_reset", target=user["username"])
        return render_template("admin_user_password.html", username=user["username"],
                               temp=temp, hours=TEMP_PASSWORD_HOURS, new_account=False,
                               needs_2fa=False)

    elif action == "reset_2fa":
        db.execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL WHERE id = ?",
                   (user_id,))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.commit()
        audit("user", "totp_reset", target=user["username"])
        if is_self:
            flash("Your two-step verification was reset. Set it up again.", "success")
            return redirect(url_for("main.two_step"))
        flash(f"Two-step verification reset for {user['username']}. They'll set it up again "
              "at their next sign-in.", "success")

    elif action == "sign_out":
        keep = g.auth_token if is_self else None
        n = sess.revoke_other_sessions(db, user_id, keep)
        audit("user", "signed_out", target=user["username"])
        flash(f"Signed out {n} session{'s' if n != 1 else ''}.", "success")

    elif action == "delete":
        if is_self:
            return refuse("You can't delete your own account.")
        if last_admin:
            return refuse("This is the only admin. Make someone else an admin first.")
        if passwords.normalize_username(request.form.get("confirm", "")) != user["username"]:
            return refuse(f"Type {user['username']} to confirm deleting the account.")
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        audit("user", "deleted", target=user["username"])
        flash(f"Deleted {user['username']}.", "success")
        return redirect(url_for("main.admin_users"))
    else:
        abort(400)

    return redirect(url_for("main.admin_user_edit", user_id=user_id))


# ============================================================ inputs and outputs

def reserved_pins() -> dict[int, str]:
    """Pins a sensor bus is using: the Pi's I²C pins once a BME280 is set up."""
    db = get_db()
    reserved = {}
    if db.execute("SELECT 1 FROM io_points WHERE sensor_type = 'bme280' LIMIT 1").fetchone():
        reserved.update({2: "I²C for the BME280", 3: "I²C for the BME280"})
    return reserved


def pin_options(current_pin: int | None):
    used = {r["pin"]: r["name"] for r in get_db().execute(
        "SELECT pin, name FROM io_points WHERE pin IS NOT NULL")}
    used = {**reserved_pins(), **used}
    one_wire = get_db().execute(
        "SELECT 1 FROM io_points WHERE sensor_type = 'ds18b20' LIMIT 1").fetchone()
    options = []
    for pin in VALID_PINS:
        taken_by = used.get(pin) if pin != current_pin else None
        note = PIN_NOTES.get(pin, "")
        if pin == 4 and one_wire:
            note = "1-Wire for DS18B20 probes, unless you moved it"
        options.append({"pin": pin, "physical": PHYSICAL_PIN[pin], "note": note,
                        "taken_by": taken_by})
    return options


def split_pulse(seconds: float) -> tuple[str, str]:
    for unit in ("h", "min"):
        size = PULSE_UNITS[unit]
        if seconds >= size and seconds % size == 0:
            return f"{seconds / size:g}", unit
    return f"{seconds:g}", "s"


DS18B20_RE = re.compile(r"^28-[0-9a-f]{12}$")
IIO_RE = re.compile(r"^iio:device\d+$")
SIMULATED_PROBES = ["28-00000a1b2c3d", "28-00000d4e5f60"]
SIMULATED_DHT = [("iio:device0", "dht11")]


def _check_pin(pin, point_id, errors, required=True):
    if pin not in VALID_PINS:
        if required:
            errors.append("Choose a GPIO pin.")
        return
    clash = get_db().execute("SELECT name FROM io_points WHERE pin = ? AND id != ?",
                             (pin, point_id or 0)).fetchone()
    if clash:
        errors.append(f"GPIO {pin} is already used by {clash['name']}.")
    elif pin in reserved_pins():
        errors.append(f"GPIO {pin} is used by {reserved_pins()[pin]}.")


def parse_io_form(form, kind: str, point_id: int | None,
                  sensor_type: str | None = None) -> tuple[dict, list[str], list[str]]:
    """Validate the input/output/sensor form. Returns (values, errors, warnings)."""
    errors, warnings = [], []
    values: dict = {"kind": kind}

    name = " ".join(form.get("name", "").split())
    if not 1 <= len(name) <= 60:
        errors.append("Give it a name of up to 60 characters.")
    values["name"] = name

    if kind == "sensor":
        spec = SENSOR_TYPES[sensor_type]
        values.update(sensor_type=sensor_type, on_label="", off_label="", pin=None)
        quantity = form.get("quantity", spec["quantities"][0])
        if quantity not in spec["quantities"]:
            errors.append(f"A {spec['label']} can't measure that.")
        values["quantity"] = quantity
        address = form.get("address", "").strip().lower()
        if sensor_type == "ds18b20":
            if not DS18B20_RE.match(address):
                errors.append("Choose a probe, or type its ID (28- followed by 12 characters).")
        elif sensor_type == "bme280":
            if parse_i2c_address(address) not in (0x76, 0x77):
                errors.append("Choose the sensor's I²C address: 0x76 or 0x77.")
            address = f"0x{parse_i2c_address(address) or 0:02x}"
            for row in get_db().execute("SELECT name, pin FROM io_points WHERE pin IN (2, 3)"):
                errors.append(f"{row['name']} is on GPIO {row['pin']}, which the BME280 needs "
                              "for I²C. Move it to another pin first.")
        else:
            if not IIO_RE.match(address):
                errors.append("Choose the DHT device (it appears once the overlay is set up).")
            try:
                values["pin"] = int(form.get("pin", ""))
            except ValueError:
                values["pin"] = None
            _check_pin(values["pin"], point_id, errors)
        values["address"] = address
        try:
            poll = int(form.get("poll_seconds", "30"))
        except ValueError:
            poll = -1
        if not spec["min_poll"] <= poll <= 3600:
            errors.append(f"Read it every {spec['min_poll']} to 3600 seconds.")
        values["poll_seconds"] = poll
        calibration = form.get("calibration", "0").replace(",", ".").strip() or "0"
        try:
            values["calibration"] = float(calibration)
        except ValueError:
            values["calibration"] = 0.0
            errors.append("The correction must be a number, such as -0.5.")
        if not -100 <= values["calibration"] <= 100:
            errors.append("Keep the correction between -100 and 100.")
        return values, errors, warnings

    try:
        pin = int(form.get("pin", ""))
    except ValueError:
        pin = -1
    _check_pin(pin, point_id, errors)
    values["pin"] = pin

    for field, label in (("on_label", "active"), ("off_label", "inactive")):
        text = " ".join(form.get(field, "").split())
        if len(text) > 24:
            errors.append(f"Keep the {label} label to 24 characters.")
        values[field] = text

    if kind == "output":
        trigger = form.get("trigger", "high")
        if trigger not in ("high", "low"):
            errors.append("Choose how the relay board is triggered.")
        values["active_high"] = int(trigger != "low")
        mode = form.get("mode", "toggle")
        if mode not in ("toggle", "pulse"):
            errors.append("Choose on/off or pulse.")
        values["mode"] = mode
        seconds = 1.0
        if mode == "pulse":
            try:
                amount = float(form.get("pulse_value", ""))
                unit = PULSE_UNITS[form.get("pulse_unit", "s")]
                seconds = amount * unit
            except (ValueError, KeyError):
                seconds = -1
            # Validate the exact value before rounding, so 0.05 s isn't rounded up to pass.
            if not MIN_PULSE_SECONDS <= seconds <= MAX_PULSE_SECONDS:
                errors.append("Pulse length must be between 0.1 seconds and 24 hours.")
            seconds = round(seconds, 1)
        values["pulse_seconds"] = seconds
        if point_id and mode in ("toggle", "pulse"):
            clash = get_db().execute(
                "SELECT name FROM rules WHERE output_id = ? AND ((? = 'pulse' AND action != 'pulse')"
                " OR (? = 'toggle' AND action = 'pulse')) ORDER BY name",
                (point_id, mode, mode)).fetchall()
            if clash:
                errors.append("These rules switch it in a way that wouldn't fit any more: "
                              + ", ".join(r["name"] for r in clash) + ". Change them first.")
        if trigger == "low" and pin in VALID_PINS and default_pull(pin) == "down":
            warnings.append(
                f"GPIO {pin} sits low while the Pi boots, which an active-low relay board reads "
                "as on. Pick GPIO 4, 5 or 6, or set the board to trigger on high.")
    else:
        wiring = form.get("wiring", "to_ground")
        if wiring not in WIRING:
            errors.append("Choose how the input is wired.")
        values["wiring"] = wiring
        try:
            bounce = int(form.get("bounce_ms", "50"))
        except ValueError:
            bounce = -1
        if not 0 <= bounce <= 1000:
            errors.append("Debounce must be between 0 and 1000 ms.")
        values["bounce_ms"] = bounce
        if wiring == "to_3v3" and pin in (2, 3):
            errors.append(f"GPIO {pin} has a fixed pull-up, so a contact to 3.3 V can't be "
                          "detected there. Use another pin, or wire the contact to ground.")
    return values, errors, warnings


def io_summary(values: dict) -> str:
    if values["kind"] == "output":
        kind = (f"pulse {describe_pulse(values['pulse_seconds'])}" if values["mode"] == "pulse"
                else "on/off")
        return f"GPIO {values['pin']}, {kind}, triggers on {'high' if values['active_high'] else 'low'}"
    if values["kind"] == "sensor":
        where = values["address"] + (f" on GPIO {values['pin']}" if values.get("pin") else "")
        return (f"{SENSOR_TYPES[values['sensor_type']]['label']}, "
                f"{QUANTITIES[values['quantity']]['label'].lower()}, {where}")
    return f"GPIO {values['pin']}, {WIRING[values['wiring']]['label'].lower()}"


def sensor_discovery(sensor_type: str) -> list[dict]:
    """Devices the Pi can see right now, with a reading where that's quick."""
    manager = io()
    found = []
    if sensor_type == "ds18b20":
        ids = SIMULATED_PROBES if manager.simulated else discover_ds18b20()
        for pid in ids:
            reading = ""
            if manager.simulated:
                reading = "simulated"
            else:
                try:
                    reading = format_value(read_ds18b20(pid), "temperature")
                except SensorError as exc:
                    reading = str(exc)
            found.append({"value": pid, "label": pid, "reading": reading})
    elif sensor_type == "dht22":
        devices = SIMULATED_DHT if manager.simulated else discover_dht()
        found = [{"value": d, "label": f"{d} ({name})", "reading": ""} for d, name in devices]
    return found


def render_io_form(kind: str, values: dict, point_id: int | None, errors=(), status=200):
    pulse_value, pulse_unit = split_pulse(float(values.get("pulse_seconds") or 1.0))
    sensor_type = values.get("sensor_type")
    return render_template(
        "admin_io_form.html", kind=kind, v=values, point_id=point_id, errors=list(errors),
        pins=pin_options(values.get("pin") if point_id else None), wiring=WIRING,
        pulse_value=pulse_value, pulse_unit=pulse_unit,
        max_hours=MAX_PULSE_SECONDS // 3600,
        sensor=SENSOR_TYPES.get(sensor_type), quantities=QUANTITIES,
        discovered=sensor_discovery(sensor_type) if kind == "sensor" else [],
        simulated=io().simulated, used_by=rules_using(point_id) if point_id else [],
    ), status


def rules_using(point_id: int) -> list[str]:
    return [r["name"] for r in get_db().execute(
        "SELECT name FROM rules WHERE source_id = ? OR output_id = ? ORDER BY name",
        (point_id, point_id))]


@bp.get("/admin/io")
@requires("io")
def admin_io():
    manager = io()
    return render_template("admin_io.html", outputs=manager.points("output"),
                           inputs=manager.points("input"), sensors=manager.points("sensor"),
                           io=manager, wiring=WIRING, sensor_types=SENSOR_TYPES,
                           quantities=QUANTITIES)


@bp.get("/admin/io/new/sensor")
@requires("io")
def admin_sensor_pick():
    return render_template("admin_sensor_pick.html", sensor_types=SENSOR_TYPES,
                           quantities=QUANTITIES)


def _insert_point(values: dict) -> int:
    db = get_db()
    now = int(time.time())
    order = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM io_points WHERE kind = ?",
                       (values["kind"],)).fetchone()[0]
    cur = db.execute(
        "INSERT INTO io_points (kind, name, pin, active_high, mode, pulse_seconds, wiring,"
        " bounce_ms, sensor_type, quantity, address, poll_seconds, calibration,"
        " on_label, off_label, sort_order, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (values["kind"], values["name"], values.get("pin"), values.get("active_high", 1),
         values.get("mode", "toggle"), values.get("pulse_seconds", 1.0),
         values.get("wiring", "to_ground"), values.get("bounce_ms", 50),
         values.get("sensor_type"), values.get("quantity"), values.get("address"),
         values.get("poll_seconds", 30), values.get("calibration", 0.0),
         values.get("on_label", ""), values.get("off_label", ""), order, now, now))
    db.commit()
    return cur.lastrowid


@bp.route("/admin/io/new/<kind>", methods=["GET", "POST"])
@bp.route("/admin/io/new/sensor/<sensor_type>", methods=["GET", "POST"], defaults={"kind": "sensor"})
@requires("io")
def admin_io_new(kind: str, sensor_type: str | None = None):
    if kind not in ("output", "input", "sensor") or (kind == "sensor" and sensor_type not in SENSOR_TYPES):
        abort(404)
    if request.method != "POST":
        defaults = {"kind": kind, "name": "", "pin": None, "active_high": 1, "mode": "toggle",
                    "pulse_seconds": 1.0, "wiring": "to_ground", "bounce_ms": 50,
                    "on_label": "", "off_label": "", "sensor_type": sensor_type,
                    "quantity": SENSOR_TYPES[sensor_type]["quantities"][0] if sensor_type else None,
                    "address": "0x76" if sensor_type == "bme280" else "",
                    "poll_seconds": 30, "calibration": 0.0}
        return render_io_form(kind, defaults, None)

    values, errors, warnings = parse_io_form(request.form, kind, None, sensor_type)
    if errors:
        return render_io_form(kind, values, None, errors, 400)
    point_id = _insert_point(values)
    resync_io()
    audit("io", "created", target=values["name"], detail=io_summary(values), io_id=point_id)
    report_save(point_id, values["name"], "added", warnings)
    return redirect(url_for("main.admin_io"))


def report_save(point_id: int, name: str, verb: str, warnings: list[str]) -> None:
    flash(f"{name} {verb}.", "success")
    for w in warnings:
        flash(w, "info")
    if error := io().errors.get(point_id):
        flash(f"Saved, but the pin couldn't be opened: {error}", "error")


@bp.route("/admin/io/<int:point_id>", methods=["GET", "POST"])
@requires("io")
def admin_io_edit(point_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM io_points WHERE id = ?", (point_id,)).fetchone()
    if row is None:
        abort(404)
    kind = row["kind"]
    if request.method != "POST":
        return render_io_form(kind, dict(row), point_id)

    values, errors, warnings = parse_io_form(request.form, kind, point_id, row["sensor_type"])
    if errors:
        return render_io_form(kind, {**dict(row), **values}, point_id, errors, 400)

    merged = {**dict(row), **values}
    db.execute(
        "UPDATE io_points SET name = ?, pin = ?, active_high = ?, mode = ?, pulse_seconds = ?,"
        " wiring = ?, bounce_ms = ?, quantity = ?, address = ?, poll_seconds = ?, calibration = ?,"
        " on_label = ?, off_label = ?, updated_at = ? WHERE id = ?",
        (merged["name"], merged["pin"], merged["active_high"], merged["mode"],
         merged["pulse_seconds"], merged["wiring"], merged["bounce_ms"], merged["quantity"],
         merged["address"], merged["poll_seconds"], merged["calibration"],
         merged["on_label"], merged["off_label"], int(time.time()), point_id))
    db.commit()
    resync_io()
    reload_rules()
    audit("io", "updated", target=values["name"], detail=io_summary(merged), io_id=point_id)
    report_save(point_id, values["name"], "saved", warnings)
    return redirect(url_for("main.admin_io"))


@bp.post("/admin/io/<int:point_id>/delete")
@requires("io")
def admin_io_delete(point_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM io_points WHERE id = ?", (point_id,)).fetchone()
    if row is None:
        abort(404)
    used_by = rules_using(point_id)
    if used_by:
        flash(f"{row['name']} is used by these rules, so it wasn't removed: "
              f"{', '.join(used_by)}. Change or remove them first.", "error")
        return redirect(url_for("main.admin_io_edit", point_id=point_id))
    if request.form.get("confirm") != "yes":
        flash("Tick the box to confirm removing it.", "error")
        return redirect(url_for("main.admin_io_edit", point_id=point_id))
    db.execute("DELETE FROM io_points WHERE id = ?", (point_id,))
    db.commit()
    resync_io()
    detail = f"GPIO {row['pin']}" if row["pin"] is not None else (row["address"] or "")
    audit("io", "deleted", target=row["name"], detail=detail, io_id=point_id)
    flash(f"{row['name']} removed." + (" Its pin is released." if row["pin"] is not None else ""),
          "success")
    return redirect(url_for("main.admin_io"))


@bp.post("/admin/io/<int:point_id>/move")
@requires("io")
def admin_io_move(point_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM io_points WHERE id = ?", (point_id,)).fetchone()
    if row is None:
        abort(404)
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM io_points WHERE kind = ? ORDER BY sort_order, id", (row["kind"],))]
    i = ids.index(point_id)
    j = i - 1 if request.form.get("direction") == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for order, pid in enumerate(ids):
            db.execute("UPDATE io_points SET sort_order = ? WHERE id = ?", (order, pid))
        db.commit()
        resync_io()
    return redirect(url_for("main.admin_io"))


# ======================================================================= settings

@bp.route("/admin/settings", methods=["GET", "POST"])
@requires("settings_view")
def admin_settings():
    db = get_db()
    # Everyone who can open Settings sees all of it; sections their group
    # can't change are shown disabled, and ignored if sent anyway.
    editable = [s for s in runtime.GROUPS if g.access.can_change_settings(s)]

    def page(values, errors, status=200):
        return render_template("admin_settings.html", values=values, errors=errors,
                               specs=runtime.SPECS, groups=runtime.GROUPS, editable=editable,
                               sections=groups.SETTINGS_SECTIONS), status

    if request.method != "POST":
        return page(runtime.load(db), [])
    if not editable:
        flash("Your group can see the settings but not change them.", "error")
        return redirect(url_for("main.admin_settings"))

    keys = [k for k, spec in runtime.SPECS.items() if spec.group in editable]
    values, errors = runtime.parse_form(request.form, keys)
    if errors:
        return page({**runtime.load(db), **values}, errors, 400)

    before = runtime.load(db)
    changed = {k: v for k, v in values.items() if before.get(k) != v}
    if changed:
        runtime.save(db, changed)
        reload_rules()                       # the rule engine reads its limit from here
        audit("settings", "updated",
              detail="; ".join(f"{runtime.SPECS[k].label}: {v}" for k, v in changed.items()))
        flash("Settings saved.", "success")
    else:
        flash("Nothing changed.", "info")
    return redirect(url_for("main.admin_settings"))


# ======================================================================= activity

ACTIVITY_FILTERS = {
    "all": "Everything",
    "control": "Controls",
    "automation": "Rules ran",
    "input": "Inputs",
    "io": "Setup",
    "rule": "Rule changes",
    "schedule": "Schedules",
    "user": "Accounts",
    "settings": "Settings",
    "export": "Downloads",
    "signin": "Sign-ins",
}

SIGNIN_EVENTS = activity.SIGNIN_EVENTS


@bp.get("/admin/activity")
@requires("activity")
def admin_activity():
    show = request.args.get("show", "all")
    if show not in ACTIVITY_FILTERS:
        show = "all"
    db = get_db()
    if show == "signin":
        rows = db.execute(
            "SELECT * FROM auth_events WHERE event != 'password_ok' ORDER BY ts DESC, id DESC LIMIT 200"
        ).fetchall()
    else:
        rows = activity.recent(db, None if show == "all" else (show,), limit=200)
    today = date.today()
    retention = cfg()["event_retention_days"]
    return render_template("admin_activity.html", rows=rows, show=show,
                           filters=ACTIVITY_FILTERS, describe=activity.describe,
                           categories=activity.CATEGORIES, signin_events=SIGNIN_EVENTS,
                           today=today.isoformat(),
                           week_ago=(today - timedelta(days=6)).isoformat(),
                           earliest=(today - timedelta(days=retention)).isoformat())


def _parse_day(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        return None


@bp.get("/admin/activity/download")
@requires("activity")
def admin_activity_download():
    """The activity log (or sign-in history) between two dates, as CSV or PDF."""
    from .. import export
    show = request.args.get("show", "all")
    fmt = request.args.get("format", "")
    first, last = _parse_day(request.args.get("start")), _parse_day(request.args.get("end"))

    def refuse(message: str):
        flash(message, "error")
        return redirect(url_for("main.admin_activity", show=show if show in ACTIVITY_FILTERS else "all"))

    if show not in ACTIVITY_FILTERS or fmt not in ("csv", "pdf"):
        return refuse("Choose what to download.")
    if first is None or last is None:
        return refuse("Choose both dates for the download.")
    if last < first:
        return refuse("The end date is before the start date.")

    start = int(datetime.combine(first, dt_time.min).timestamp())
    end = int(datetime.combine(last + timedelta(days=1), dt_time.min).timestamp())
    db = get_db()
    if show == "signin":
        title, header, widths = "Sign-in history", export.SIGNIN_HEADER, export.SIGNIN_WIDTHS
        rows = export.signin_rows(db, start, end)
    else:
        title, header, widths = "Activity log", export.ACTIVITY_HEADER, export.ACTIVITY_WIDTHS
        rows = export.activity_rows(db, show, start, end)

    def day(d: date) -> str:
        return f"{d.day} {d:%b %Y}"
    period = day(first) if first == last else f"{day(first)} to {day(last)}"
    scope = f"{ACTIVITY_FILTERS[show]}, {period}"
    name = f"coilgate-{'signins' if show == 'signin' else 'activity'}" \
           f"{'' if show in ('all', 'signin') else '-' + show}-{first:%Y-%m-%d}-to-{last:%Y-%m-%d}"

    if fmt == "csv":
        body, mimetype = export.to_csv(header, rows), "text/csv; charset=utf-8"
    else:
        try:
            body = export.to_pdf(site=cfg()["site_name"], title=title, scope=scope,
                                 generated_by=g.user["username"], header=header,
                                 widths=widths, rows=rows)
        except export.ExportTooLarge as exc:
            return refuse(str(exc))
        except ImportError:
            return refuse("PDF downloads need the fpdf2 package. Run "
                          "pip install -r requirements.txt, then restart CoilGate.")
        mimetype = "application/pdf"

    audit("export", fmt, target=title, detail=f"{scope}, {len(rows):,} entries")
    resp = Response(body, mimetype=mimetype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}.{fmt}"'
    return resp
