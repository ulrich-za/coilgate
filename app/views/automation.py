# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin area: rules and schedules."""
from __future__ import annotations

import time
from datetime import datetime

from flask import abort, flash, make_response, redirect, render_template, request, url_for

from .. import rules as rules_mod
from .. import schedules as sched
from ..db import get_db
from ..hardware import describe_pulse
from ..sensors import QUANTITIES, format_value
from .common import audit, bp, io, reload_rules, requires, rule_engine

HOLD_UNITS = {"s": 1, "min": 60}
TRIGGERS = {
    "input": ("An input changes", "A door contact, button or other on/off input reaches a state."),
    "sensor": ("A sensor reading crosses a value", "A temperature, humidity or pressure reading goes above or below a value."),
    "schedule": ("A schedule is active", "Something should happen during set times, such as lights on in the evening."),
    "rule": ("Another rule is active", "Chain rules: start when another rule's condition is met, or stops being met."),
}
ACTION_WORDS = {"on": "Turn on", "off": "Turn off", "toggle": "Toggle", "pulse": "Pulse"}
RULE_ACTION_WORDS = {"rule_on": "Switch on rule", "rule_off": "Switch off rule",
                     "rule_toggle": "Toggle rule"}


# ======================================================================= rules

def all_rules() -> dict[int, rules_mod.Rule]:
    return {r["id"]: rules_mod.Rule.from_row(r)
            for r in get_db().execute("SELECT * FROM rules ORDER BY name")}


def choices(rule_id: int | None = None) -> dict:
    """Everything the rule builders offer. `rule_id` (when editing) is left out
    of the rule lists, since a rule can't trigger or switch itself."""
    points = io().points()
    inputs = [p for p in points if p.kind == "input"]
    sensors = [p for p in points if p.kind == "sensor"]
    outputs = [p for p in points if p.kind == "output"]
    input_options = []
    for p in inputs:
        input_options.append((f"{p.id}:active", f"{p.name} is {p.active_text}"))
        input_options.append((f"{p.id}:inactive", f"{p.name} is {p.inactive_text}"))
    action_options = []
    for p in outputs:
        if p.mode == "pulse":
            action_options.append((f"{p.id}:pulse", f"Pulse {p.name} ({describe_pulse(p.pulse_seconds)})"))
        else:
            for action in ("on", "off", "toggle"):
                action_options.append((f"{p.id}:{action}", f"{ACTION_WORDS[action]} {p.name}"))
    rules = {rid: r for rid, r in all_rules().items() if rid != rule_id}
    rule_options, rule_action_options = [], []
    for r in rules.values():
        rule_options.append((f"{r.id}:active", f"{r.name} is active"))
        rule_options.append((f"{r.id}:inactive", f"{r.name} is not active"))
        for action, words in RULE_ACTION_WORDS.items():
            rule_action_options.append((f"r{r.id}:{action}", f"{words} {r.name}"))
    schedules = sched.load_all(get_db())
    return {
        "inputs": inputs, "sensors": sensors, "outputs": outputs, "rules": rules,
        "input_options": input_options, "action_options": action_options,
        "rule_options": rule_options, "rule_action_options": rule_action_options,
        "schedules": list(schedules.values()),
        "trigger_schedules": [s for s in schedules.values() if not s.builtin],
        "always_id": next(s.id for s in schedules.values() if s.builtin),
    }


def _split(value: str) -> tuple[int | None, str]:
    try:
        pid, _, rest = value.partition(":")
        return int(pid), rest
    except ValueError:
        return None, ""


def _float(text: str) -> float | None:
    try:
        return float(str(text).replace(",", ".").strip())
    except ValueError:
        return None


def parse_rule_form(form, trigger_type: str, c: dict,
                    rule_id: int | None = None) -> tuple[dict, list[str]]:
    errors: list[str] = []
    points = {p.id: p for p in c["inputs"] + c["sensors"] + c["outputs"]}
    schedule_ids = {s.id: s for s in c["schedules"]}
    v: dict = {"trigger_type": trigger_type, "source_id": None, "source_rule_id": None,
               "input_state": None, "comparator": None, "threshold": None,
               "reset_threshold": None, "trigger_schedule_id": None, "hold_seconds": 0,
               "output_id": None, "target_rule_id": None}

    if trigger_type == "input":
        pid, state = _split(form.get("source", ""))
        p = points.get(pid)
        if p is None or p.kind != "input" or state not in ("active", "inactive"):
            errors.append("Choose the input and the state that starts the rule.")
        v.update(source_id=pid, input_state=state)
    elif trigger_type == "sensor":
        pid, _ = _split(form.get("source", "") + ":")
        p = points.get(pid)
        if p is None or p.kind != "sensor":
            errors.append("Choose a sensor reading.")
        comparator = form.get("comparator", "above")
        if comparator not in ("above", "below"):
            errors.append("Choose above or below.")
        threshold = _float(form.get("threshold", ""))
        if threshold is None:
            errors.append("Enter the value that starts the rule.")
        v.update(source_id=pid, comparator=comparator, threshold=threshold)
    elif trigger_type == "rule":
        rid, state = _split(form.get("source", ""))
        if rid not in c["rules"] or state not in ("active", "inactive"):
            errors.append("Choose the rule that starts this one, and whether it's active or not.")
        else:
            loop = rules_mod.trigger_loop({**all_rules()}, rule_id, rid)
            if loop:
                errors.append("That would make a loop: " + " → ".join(loop) + ".")
        v.update(source_rule_id=rid, input_state=state)
    else:
        sid = _float(form.get("trigger_schedule", ""))
        s = schedule_ids.get(int(sid)) if sid is not None else None
        if s is None or s.builtin:
            errors.append("Choose the schedule that starts the rule. (Make one under Schedules first.)")
        v["trigger_schedule_id"] = s.id if s else None

    if trigger_type != "schedule":
        amount = _float(form.get("hold_value", "0") or "0")
        unit = HOLD_UNITS.get(form.get("hold_unit", "s"))
        if amount is None or unit is None or amount < 0 or amount * unit > 86400:
            errors.append("The waiting time must be between 0 and 24 hours.")
        else:
            v["hold_seconds"] = int(round(amount * unit))

    limit = _float(form.get("schedule_id", c["always_id"]))
    v["schedule_id"] = int(limit) if limit is not None and int(limit) in schedule_ids else c["always_id"]

    target = form.get("target", "")
    if target.startswith("r"):
        rid, action = _split(target[1:])
        if rid not in c["rules"] or action not in rules_mod.RULE_ACTIONS:
            errors.append("Choose what the rule does.")
        v.update(target_rule_id=rid, action=action)
    else:
        oid, action = _split(target)
        out = points.get(oid)
        if out is None or out.kind != "output":
            errors.append("Choose what the rule does.")
        elif (out.mode == "pulse") != (action == "pulse") or action not in rules_mod.OUTPUT_ACTIONS:
            errors.append("That action doesn't suit that output.")
        v.update(output_id=oid, action=action)

    v["undo"] = int("undo" in form and action in rules_mod.OPPOSITE)
    v["enabled"] = int("enabled" in form)

    if trigger_type == "sensor" and not errors:
        reset = _float(form.get("reset_threshold", "")) if form.get("reset_threshold", "").strip() else None
        if v["undo"]:
            if reset is None:
                reset = v["threshold"] - 1 if v["comparator"] == "above" else v["threshold"] + 1
            if v["comparator"] == "above" and reset >= v["threshold"]:
                errors.append("The 'turn it back' value must be lower than the trigger value.")
            if v["comparator"] == "below" and reset <= v["threshold"]:
                errors.append("The 'turn it back' value must be higher than the trigger value.")
        v["reset_threshold"] = reset

    name = " ".join(form.get("name", "").split())
    if len(name) > 80:
        errors.append("Keep the name to 80 characters.")
    v["name"] = name or auto_name(v, points, {s.id: s for s in c["schedules"]}, c["rules"])
    return v, errors


def auto_name(v: dict, points: dict, schedules: dict, rules: dict | None = None) -> str:
    rules = rules or {}
    action = v.get("action") or ""
    if action in rules_mod.RULE_ACTIONS:
        target = rules.get(v.get("target_rule_id"))
    else:
        target = points.get(v.get("output_id"))
    doing = rules_mod.action_phrase(action, target.name) if target else "action"
    src = points.get(v.get("source_id"))
    if v["trigger_type"] == "input" and src:
        when = f"{src.name} {src.active_text if v['input_state'] == 'active' else src.inactive_text}"
    elif v["trigger_type"] == "sensor" and src and v.get("threshold") is not None:
        when = f"{src.name} {v['comparator']} {format_value(v['threshold'], src.quantity)}"
    elif v["trigger_type"] == "rule" and rules.get(v.get("source_rule_id")):
        src_rule = rules[v["source_rule_id"]]
        when = f"{src_rule.name} {'active' if v['input_state'] == 'active' else 'not active'}"
    else:
        s = schedules.get(v.get("trigger_schedule_id"))
        when = s.name if s else "Schedule"
    name = f"{when}: {doing}"
    if len(name) > 80:
        name = name[:79].rsplit(" ", 1)[0].rstrip(":,") + "…"
    return name


RULE_COLUMNS = ("name", "enabled", "trigger_type", "source_id", "source_rule_id", "input_state",
                "comparator", "threshold", "reset_threshold", "trigger_schedule_id",
                "hold_seconds", "schedule_id", "output_id", "target_rule_id", "action", "undo")


def save_rule(v: dict, rule_id: int | None = None) -> int:
    db = get_db()
    now = int(time.time())
    values = [v[c] for c in RULE_COLUMNS]
    if rule_id is None:
        cur = db.execute(
            f"INSERT INTO rules ({', '.join(RULE_COLUMNS)}, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(RULE_COLUMNS))}, ?, ?)", (*values, now, now))
        rule_id = cur.lastrowid
    else:
        db.execute(f"UPDATE rules SET {', '.join(c + ' = ?' for c in RULE_COLUMNS)}, updated_at = ?"
                   " WHERE id = ?", (*values, now, rule_id))
    db.commit()
    reload_rules()
    return rule_id


def form_values(trigger_type: str, v: dict) -> dict:
    """How a rule's stored values appear in the builders' fields."""
    hold = int(v.get("hold_seconds") or 0)
    hold_value, hold_unit = (f"{hold // 60}", "min") if hold and hold % 60 == 0 else (f"{hold}", "s")
    source_value = ""
    if trigger_type == "input" and v.get("source_id"):
        source_value = f"{v['source_id']}:{v['input_state']}"
    elif trigger_type == "sensor" and v.get("source_id"):
        source_value = str(v["source_id"])
    elif trigger_type == "rule" and v.get("source_rule_id"):
        source_value = f"{v['source_rule_id']}:{v['input_state']}"
    if v.get("target_rule_id"):
        target_value = f"r{v['target_rule_id']}:{v['action']}"
    elif v.get("output_id"):
        target_value = f"{v['output_id']}:{v['action']}"
    else:
        target_value = ""
    fmt = lambda x: "" if x is None else f"{x:g}"
    return {"hold_value": hold_value, "hold_unit": hold_unit, "source_value": source_value,
            "target_value": target_value, "threshold": fmt(v.get("threshold")),
            "reset_threshold": fmt(v.get("reset_threshold"))}


def builder_view() -> str:
    """Sentence or blocks: from ?view=, else the browser's last choice."""
    view = request.args.get("view") or request.form.get("view") or request.cookies.get(VIEW_COOKIE)
    return view if view in ("sentence", "blocks") else "sentence"


VIEW_COOKIE = "coilgate_rule_builder"


def remember_view(resp, view: str):
    if request.args.get("view") in ("sentence", "blocks"):
        resp = make_response(resp)
        resp.set_cookie(VIEW_COOKIE, view, max_age=365 * 86400, path="/admin", samesite="Lax",
                        secure=request.is_secure, httponly=True)
    return resp


def render_rule_form(trigger_type: str, v: dict, rule_id: int | None, errors=(), status=200):
    c = choices(rule_id)
    return render_template(
        "admin_rule_form.html", trigger_type=trigger_type, trigger=TRIGGERS[trigger_type],
        v=v, rule_id=rule_id, errors=list(errors), c=c, quantities=QUANTITIES,
        used_by=rule_users(get_db(), rule_id) if rule_id else [],
        **form_values(trigger_type, v),
    ), status


def render_blocks(v: dict, rule_id: int | None, errors=(), status=200):
    c = choices(rule_id)
    trigger_type = v.get("trigger_type") or ""
    return render_template(
        "admin_rule_blocks.html", v=v, rule_id=rule_id, errors=list(errors), c=c,
        quantities=QUANTITIES, trigger_type=trigger_type, triggers=TRIGGERS,
        action_words=ACTION_WORDS, rule_action_words=RULE_ACTION_WORDS, blocks=True,
        used_by=rule_users(get_db(), rule_id) if rule_id else [],
        **form_values(trigger_type, v),
    ), status


def rule_users(db, rule_id: int) -> list[str]:
    """Names of the rules that are triggered by, or switch, this rule."""
    return [r["name"] for r in db.execute(
        "SELECT name FROM rules WHERE (source_rule_id = ? OR target_rule_id = ?) AND id != ?"
        " ORDER BY name", (rule_id, rule_id, rule_id))]


def _sentence(rule_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        return ""
    return rules_mod.sentence(rules_mod.Rule.from_row(row), {p.id: p for p in io().points()},
                              sched.load_all(db), all_rules())


@bp.get("/admin/rules")
@requires("rules")
def admin_rules():
    db = get_db()
    rows = db.execute("SELECT * FROM rules ORDER BY enabled DESC, name").fetchall()
    points = {p.id: p for p in io().points()}
    schedules = sched.load_all(db)
    rules = all_rules()
    engine = rule_engine()
    items = []
    for row in rows:
        rule = rules_mod.Rule.from_row(row)
        items.append({"rule": rule, "row": row, "state": engine.status(rule.id),
                      "paused": int(engine.paused_for(rule.id) + 0.999),
                      "parts": rules_mod.describe(rule, points, schedules, rules)})
    has_outputs = any(p.kind == "output" for p in points.values())
    return render_template("admin_rules.html", items=items, has_outputs=has_outputs,
                           view=builder_view(), max_actions=engine.max_actions)


def default_values(trigger_type: str, c: dict) -> dict:
    return {"trigger_type": trigger_type, "enabled": 1, "undo": int(trigger_type not in ("input", "")),
            "schedule_id": c["always_id"], "comparator": "above", "hold_seconds": 0, "name": ""}


def _created(v: dict, rule_id: int):
    audit("rule", "created", target=v["name"], detail=_sentence(rule_id))
    flash(f"Rule “{v['name']}” added." + ("" if v["enabled"] else " It's switched off for now."),
          "success")
    return redirect(url_for("main.admin_rules"))


@bp.route("/admin/rules/new", methods=["GET", "POST"])
@requires("rules")
def admin_rule_pick():
    """New rule: the sentence builder starts by choosing a trigger; the blocks
    builder has everything on one page and posts here."""
    c = choices()
    if request.method == "POST":
        trigger_type = request.form.get("trigger_type", "")
        if trigger_type not in TRIGGERS:
            v = {**default_values("", c), **{k: request.form.get(k) for k in ("name",)}}
            return render_blocks(v, None, ["Drop something into “When” to say what starts the rule."], 400)
        v, errors = parse_rule_form(request.form, trigger_type, c)
        if errors:
            return render_blocks(v, None, errors, 400)
        return _created(v, save_rule(v))
    if builder_view() == "blocks":
        return remember_view(render_blocks(default_values("", c), None), "blocks")
    available = {"input": bool(c["inputs"]), "sensor": bool(c["sensors"]),
                 "schedule": bool(c["trigger_schedules"]), "rule": bool(c["rules"])}
    return remember_view(render_template(
        "admin_rule_pick.html", triggers=TRIGGERS, available=available,
        has_outputs=bool(c["outputs"])), "sentence")


@bp.route("/admin/rules/new/<trigger_type>", methods=["GET", "POST"])
@requires("rules")
def admin_rule_new(trigger_type: str):
    if trigger_type not in TRIGGERS:
        abort(404)
    c = choices()
    if request.method != "POST":
        return render_rule_form(trigger_type, default_values(trigger_type, c), None)
    v, errors = parse_rule_form(request.form, trigger_type, c)
    if errors:
        return render_rule_form(trigger_type, v, None, errors, 400)
    return _created(v, save_rule(v))


@bp.route("/admin/rules/<int:rule_id>", methods=["GET", "POST"])
@requires("rules")
def admin_rule_edit(rule_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        abort(404)
    view = builder_view()
    if request.method != "POST":
        page = render_blocks(dict(row), rule_id) if view == "blocks" \
            else render_rule_form(row["trigger_type"], dict(row), rule_id)
        return remember_view(page, view)
    # The blocks builder can change what starts a rule; the sentence builder can't.
    trigger_type = request.form.get("trigger_type") or row["trigger_type"]
    if trigger_type not in TRIGGERS:
        trigger_type = row["trigger_type"]
    v, errors = parse_rule_form(request.form, trigger_type, choices(rule_id), rule_id)
    if errors:
        merged = {**dict(row), **v}
        if request.form.get("view") == "blocks":
            return render_blocks(merged, rule_id, errors, 400)
        return render_rule_form(trigger_type, merged, rule_id, errors, 400)
    save_rule(v, rule_id)
    audit("rule", "updated", target=v["name"], detail=_sentence(rule_id))
    flash(f"Rule “{v['name']}” saved.", "success")
    return redirect(url_for("main.admin_rules"))


@bp.post("/admin/rules/<int:rule_id>/toggle")
@requires("rules")
def admin_rule_toggle(rule_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        abort(404)
    enabled = 0 if row["enabled"] else 1
    db.execute("UPDATE rules SET enabled = ?, updated_at = ? WHERE id = ?",
               (enabled, int(time.time()), rule_id))
    db.commit()
    reload_rules()
    audit("rule", "enabled" if enabled else "disabled", target=row["name"])
    flash(f"Rule “{row['name']}” switched {'on' if enabled else 'off'}.", "success")
    return redirect(url_for("main.admin_rules"))


@bp.post("/admin/rules/<int:rule_id>/resume")
@requires("rules")
def admin_rule_resume(rule_id: int):
    row = get_db().execute("SELECT name FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        abort(404)
    if rule_engine().resume(rule_id):
        audit("rule", "resumed", target=row["name"])
        flash(f"Rule “{row['name']}” resumed.", "success")
    return redirect(url_for("main.admin_rules"))


@bp.post("/admin/rules/<int:rule_id>/delete")
@requires("rules")
def admin_rule_delete(rule_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        abort(404)
    if request.form.get("confirm") != "yes":
        flash("Tick the box to confirm removing the rule.", "error")
        return redirect(url_for("main.admin_rule_edit", rule_id=rule_id))
    if users := rule_users(db, rule_id):
        flash(f"Other rules use “{row['name']}”: {', '.join(users)}. Change or remove them first.",
              "error")
        return redirect(url_for("main.admin_rule_edit", rule_id=rule_id))
    db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    db.commit()
    reload_rules()
    audit("rule", "deleted", target=row["name"])
    flash(f"Rule “{row['name']}” removed.", "success")
    return redirect(url_for("main.admin_rules"))


# ======================================================================= schedules

BLANK_ROWS = 3


def schedule_usage(db, schedule_id: int) -> dict:
    rules = [r["name"] for r in db.execute(
        "SELECT name FROM rules WHERE schedule_id = ? OR trigger_schedule_id = ? ORDER BY name",
        (schedule_id, schedule_id))]
    users = [r["username"] for r in db.execute(
        "SELECT username FROM users WHERE schedule_id = ? ORDER BY username", (schedule_id,))]
    return {"rules": rules, "users": users}


def parse_schedule_form(form, schedule_id: int | None) -> tuple[str, list[sched.Window], list[str]]:
    errors: list[str] = []
    name = " ".join(form.get("name", "").split())
    if not 1 <= len(name) <= 40:
        errors.append("Give the schedule a name of up to 40 characters.")
    elif name.lower() == sched.ALWAYS_NAME.lower():
        errors.append("That name belongs to the built-in schedule.")
    else:
        clash = get_db().execute("SELECT id FROM schedules WHERE name = ? COLLATE NOCASE AND id != ?",
                                 (name, schedule_id or 0)).fetchone()
        if clash:
            errors.append(f"There's already a schedule called {name}.")
    windows: list[sched.Window] = []
    try:
        rows = min(int(form.get("rows", "0")), 50)
    except ValueError:
        rows = 0
    for i in range(rows):
        days = 0
        for d in form.getlist(f"w{i}_days"):
            if d.isdigit() and 0 <= int(d) <= 6:
                days |= 1 << int(d)
        start_text, end_text = form.get(f"w{i}_start", "").strip(), form.get(f"w{i}_end", "").strip()
        if not days and not start_text and not end_text:
            continue
        label = f"Time window {len(windows) + 1}"
        start, end = sched.parse_time(start_text), sched.parse_time(end_text)
        if not days:
            errors.append(f"{label}: choose at least one day.")
        if start is None or end is None:
            errors.append(f"{label}: enter a start and end time.")
        if days and start is not None and end is not None:
            windows.append(sched.Window(days, start, end if end != 0 else 1440))
    return name, windows, errors


def render_schedule_form(schedule: sched.Schedule | None, name: str, windows, errors=(), status=200):
    rows = [{"days": w.days, "start": sched.fmt_time(w.start), "end": sched.fmt_time(w.end)}
            for w in windows]
    rows += [{"days": 0, "start": "", "end": ""} for _ in range(BLANK_ROWS)]
    preview = sched.Schedule(schedule.id if schedule else 0, name or "New schedule", False, list(windows))
    usage = schedule_usage(get_db(), schedule.id) if schedule else {"rules": [], "users": []}
    return render_template("admin_schedule_form.html", schedule=schedule, name=name, rows=rows,
                           errors=list(errors), days=sched.DAY_NAMES, preview=preview,
                           usage=usage), status


@bp.get("/admin/schedules")
@requires("schedules")
def admin_schedules():
    db = get_db()
    items = [{"schedule": s, "usage": schedule_usage(db, s.id)}
             for s in sched.load_all(db).values()]
    now_text = datetime.now().strftime("%A %H:%M")
    return render_template("admin_schedules.html", items=items, days=sched.DAY_NAMES,
                           now_text=f"now {now_text}")


@bp.route("/admin/schedules/new", methods=["GET", "POST"])
@requires("schedules")
def admin_schedule_new():
    if request.method != "POST":
        return render_schedule_form(None, "", [])
    name, windows, errors = parse_schedule_form(request.form, None)
    if errors:
        return render_schedule_form(None, name, windows, errors, 400)
    db = get_db()
    now = int(time.time())
    cur = db.execute("INSERT INTO schedules (name, builtin, created_at, updated_at) VALUES (?, 0, ?, ?)",
                     (name, now, now))
    _save_windows(db, cur.lastrowid, windows)
    reload_rules()
    audit("schedule", "created", target=name,
          detail=sched.Schedule(cur.lastrowid, name, False, windows).summary())
    flash(f"Schedule {name} added.", "success")
    return redirect(url_for("main.admin_schedules"))


def _save_windows(db, schedule_id: int, windows) -> None:
    db.execute("DELETE FROM schedule_windows WHERE schedule_id = ?", (schedule_id,))
    for w in windows:
        db.execute("INSERT INTO schedule_windows (schedule_id, days, start_min, end_min)"
                   " VALUES (?, ?, ?, ?)", (schedule_id, w.days, w.start, w.end))
    db.commit()


@bp.route("/admin/schedules/<int:schedule_id>", methods=["GET", "POST"])
@requires("schedules")
def admin_schedule_edit(schedule_id: int):
    db = get_db()
    schedule = sched.load_all(db).get(schedule_id)
    if schedule is None or schedule.builtin:
        abort(404)
    if request.method != "POST":
        return render_schedule_form(schedule, schedule.name, schedule.windows)
    name, windows, errors = parse_schedule_form(request.form, schedule_id)
    if errors:
        return render_schedule_form(schedule, name, windows, errors, 400)
    db.execute("UPDATE schedules SET name = ?, updated_at = ? WHERE id = ?",
               (name, int(time.time()), schedule_id))
    _save_windows(db, schedule_id, windows)
    reload_rules()
    audit("schedule", "updated", target=name,
          detail=sched.Schedule(schedule_id, name, False, windows).summary())
    flash(f"Schedule {name} saved.", "success")
    return redirect(url_for("main.admin_schedules"))


@bp.post("/admin/schedules/<int:schedule_id>/delete")
@requires("schedules")
def admin_schedule_delete(schedule_id: int):
    db = get_db()
    schedule = sched.load_all(db).get(schedule_id)
    if schedule is None or schedule.builtin:
        abort(404)
    usage = schedule_usage(db, schedule_id)
    if usage["rules"] or usage["users"]:
        flash("This schedule is still in use, so it wasn't removed. Change the rules and accounts "
              "listed below first.", "error")
        return redirect(url_for("main.admin_schedule_edit", schedule_id=schedule_id))
    if request.form.get("confirm") != "yes":
        flash("Tick the box to confirm removing the schedule.", "error")
        return redirect(url_for("main.admin_schedule_edit", schedule_id=schedule_id))
    db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    db.commit()
    reload_rules()
    audit("schedule", "deleted", target=schedule.name)
    flash(f"Schedule {schedule.name} removed.", "success")
    return redirect(url_for("main.admin_schedules"))
