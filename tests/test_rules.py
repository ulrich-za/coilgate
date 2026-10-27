# SPDX-License-Identifier: GPL-2.0-or-later
"""Rules: the engine's behaviour and the rule builder."""
from __future__ import annotations

from datetime import datetime

import pytest

from conftest import add_point, add_user, db, io, login, post

MONDAY_10 = datetime(2026, 10, 19, 10, 0)
MONDAY_19 = datetime(2026, 10, 19, 19, 0)
MONDAY_2330 = datetime(2026, 10, 19, 23, 30)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def engine(app):
    e = app.extensions["coilgate_rules"]
    e.clock = Clock()
    return e


def admin(app):
    add_user(app)
    client = app.test_client()
    login(client)
    return client


def schedule_id(app, name):
    with db(app) as conn:
        return conn.execute("SELECT id FROM schedules WHERE name = ?", (name,)).fetchone()[0]


def add_schedule(app, name, days, start, end):
    import time
    with db(app) as conn:
        cur = conn.execute("INSERT INTO schedules (name, builtin, created_at, updated_at) VALUES (?, 0, ?, ?)",
                           (name, int(time.time()), int(time.time())))
        conn.execute("INSERT INTO schedule_windows (schedule_id, days, start_min, end_min) VALUES (?, ?, ?, ?)",
                     (cur.lastrowid, days, start, end))
        return cur.lastrowid


def setup(app):
    door = add_point(app, kind="input", name="Front door", pin=5, bounce_ms=0, on_label="Open", off_label="Shut")
    light = add_point(app, kind="output", name="Porch light", pin=17)
    temp = add_point(app, kind="sensor", name="Living Room Temperature", pin=None,
                     sensor_type="ds18b20", quantity="temperature", address="28-00000a1b2c3d")
    aircon = add_point(app, kind="output", name="Living Room Aircon", pin=27)
    gate = add_point(app, kind="output", name="Gate", pin=22, mode="pulse", pulse_seconds=1)
    return door, light, temp, aircon, gate


def new_rule(client, trigger, **fields):
    data = {"enabled": "on", **fields}
    return post(client, f"/admin/rules/new/{trigger}", data)


def rule_ids(app):
    with db(app) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM rules ORDER BY id")]


# ---------------------------------------------------------------- input rules

def test_input_rule_switches_and_undoes(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    resp = new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", undo="on",
                    schedule_id=schedule_id(app, "Always (24/7)"))
    assert resp.status_code == 302
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)
    io(app).simulate_input(door, True)          # door opens
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(light)
    io(app).simulate_input(door, False)         # door shuts
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)


def test_hold_time(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on",
             hold_value="2", hold_unit="min")
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    engine.clock.t += 119
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)
    engine.clock.t += 1
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(light)


def test_hold_timer_resets_if_condition_drops(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", hold_value="60", hold_unit="s")
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    engine.clock.t += 50
    io(app).simulate_input(door, False)
    engine.evaluate_once(MONDAY_10)
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    engine.clock.t += 50
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)


def test_rule_acts_on_change_not_continuously(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on")
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    io(app).set(light, False)                   # someone switches it off by hand
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)             # the rule doesn't fight them


# ---------------------------------------------------------------- sensor rules

def test_sensor_rule_with_hysteresis(app, engine):
    _, _, temp, aircon, _ = setup(app)
    client = admin(app)
    resp = new_rule(client, "sensor", source=str(temp), comparator="above", threshold="26",
                    reset_threshold="24", target=f"{aircon}:on", undo="on")
    assert resp.status_code == 302
    for value, expected in ((25.0, False), (26.5, True), (25.0, True), (24.5, True), (24.0, False),
                            (25.5, False), (26.1, True)):
        io(app).simulate_reading(temp, value)
        engine.evaluate_once(MONDAY_10)
        assert io(app).is_on(aircon) is expected, value


def test_default_gap_is_one_unit(app, engine):
    _, _, temp, aircon, _ = setup(app)
    client = admin(app)
    new_rule(client, "sensor", source=str(temp), comparator="below", threshold="18",
             target=f"{aircon}:on", undo="on")
    with db(app) as conn:
        assert conn.execute("SELECT reset_threshold FROM rules").fetchone()[0] == 19.0


def test_sensor_without_reading_changes_nothing(app, engine):
    _, _, temp, aircon, _ = setup(app)
    client = admin(app)
    new_rule(client, "sensor", source=str(temp), comparator="above", threshold="26",
             target=f"{aircon}:on", undo="on")
    io(app).simulate_reading(temp, 30)
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(aircon)
    io(app).readings[temp].fresh_until = 0      # sensor goes quiet
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(aircon)                # left as it was, not switched off


@pytest.mark.parametrize("fields, message", [
    ({"threshold": "26", "reset_threshold": "27"}, "must be lower"),
    ({"threshold": "", "reset_threshold": ""}, "Enter the value"),
    ({"comparator": "below", "threshold": "18", "reset_threshold": "17"}, "must be higher"),
])
def test_sensor_rule_validation(app, fields, message):
    _, _, temp, aircon, _ = setup(app)
    client = admin(app)
    data = {"source": str(temp), "comparator": "above", "target": f"{aircon}:on", "undo": "on", **fields}
    resp = new_rule(client, "sensor", **data)
    assert resp.status_code == 400 and message in resp.get_data(as_text=True)


# ---------------------------------------------------------------- schedules in rules

def test_schedule_triggered_rule(app, engine):
    _, light, *_ = setup(app)
    evenings = add_schedule(app, "Evenings", 0b1111111, 18 * 60, 23 * 60)
    client = admin(app)
    new_rule(client, "schedule", trigger_schedule=str(evenings), target=f"{light}:on", undo="on")
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)
    engine.evaluate_once(MONDAY_19)
    assert io(app).is_on(light)
    engine.evaluate_once(MONDAY_2330)
    assert not io(app).is_on(light)


def test_rule_limited_to_a_schedule(app, engine):
    door, light, *_ = setup(app)
    evenings = add_schedule(app, "Evenings", 0b1111111, 18 * 60, 23 * 60)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", undo="on",
             schedule_id=str(evenings))
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)             # door open, but not during Evenings
    engine.evaluate_once(MONDAY_19)
    assert io(app).is_on(light)
    engine.evaluate_once(MONDAY_2330)           # Evenings over: undone
    assert not io(app).is_on(light)


def test_condition_already_true_at_startup_runs(app, engine):
    _, light, *_ = setup(app)
    evenings = add_schedule(app, "Evenings", 0b1111111, 18 * 60, 23 * 60)
    client = admin(app)
    new_rule(client, "schedule", trigger_schedule=str(evenings), target=f"{light}:on", undo="on")
    engine.evaluate_once(MONDAY_19)             # first check happens mid-evening, e.g. after a power cut
    assert io(app).is_on(light)


# ---------------------------------------------------------------- actions and safety

def test_pulse_action(app, engine):
    door, _, _, _, gate = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{gate}:pulse", undo="on")
    with db(app) as conn:
        assert conn.execute("SELECT undo FROM rules").fetchone()[0] == 0    # pulses can't be undone
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_pulsing(gate)


def test_disabled_rule_does_nothing_and_toggle_works(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    post(client, "/admin/rules/new/input", {"source": f"{door}:active", "target": f"{light}:on"})   # no "enabled"
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)
    rid = rule_ids(app)[0]
    post(client, f"/admin/rules/{rid}/toggle", page="/admin/rules")
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(light)


def set_limit(app, engine, limit):
    with db(app) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('rule_max_actions_per_minute', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(limit),))
    engine.reload()


def press_and_release(app, engine, door, times):
    for _ in range(times):
        io(app).simulate_input(door, True)
        engine.evaluate_once(MONDAY_10)
        io(app).simulate_input(door, False)
        engine.evaluate_once(MONDAY_10)


def automation(app, action):
    with db(app) as conn:
        return conn.execute("SELECT COUNT(*) FROM activity WHERE category='automation'"
                            " AND action = ?", (action,)).fetchone()[0]


def test_flapping_rule_is_paused(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", undo="on")
    press_and_release(app, engine, door, 29)          # 58 actions: a busy button is fine
    assert automation(app, "paused") == 0
    press_and_release(app, engine, door, 6)           # a chattering contact isn't
    assert automation(app, "paused") == 1
    assert automation(app, "on") + automation(app, "off") == 60


def test_pause_catches_up_when_it_ends(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", undo="on", name="Hold")
    set_limit(app, engine, 11)
    press_and_release(app, engine, door, 6)           # the 12th action (an "off") is held back
    assert io(app).is_on(light)                       # ...so the light is left on
    assert engine.paused_for(1) > 0
    page = client.get("/admin/rules").get_data(as_text=True)
    assert "Paused for 60 s more" in page and "Resume now" in page
    engine.clock.t += 61
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)                   # caught up: the button is released
    assert automation(app, "resumed") == 1
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(light)                       # and it works normally again


def test_resume_now(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", undo="on", name="Hold")
    set_limit(app, engine, 11)
    press_and_release(app, engine, door, 6)
    resp = post(client, "/admin/rules/1/resume", page="/admin/rules")
    assert resp.status_code == 302
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light) and engine.paused_for(1) == 0


def test_catch_up_skips_toggles(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:toggle", name="Toggle")
    set_limit(app, engine, 10)
    press_and_release(app, engine, door, 12)          # 11th toggle held back
    before = io(app).is_on(light)
    engine.clock.t += 61
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(light) == before             # a held-back toggle isn't replayed


def test_limit_is_a_setting(app, engine):
    add_user(app)
    client = app.test_client()
    login(client)
    page = client.get("/admin/settings").get_data(as_text=True)
    assert "Actions per rule per minute" in page
    form = {}
    import re
    for name, value in re.findall(r'name="([a-z_0-9]+)"[^>]*value="([^"]*)"', page):
        if name != "csrf_token":
            form[name] = value
    for name in re.findall(r'name="([a-z_0-9]+)" type="checkbox"[^>]*checked', page):
        form[name] = "on"
    form["rule_max_actions_per_minute"] = "120"
    post(client, "/admin/settings", form)
    assert engine.max_actions == 120


def test_rule_actions_are_logged(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", name="Porch on arrival")
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    with db(app) as conn:
        row = conn.execute("SELECT category, action, target, detail, username FROM activity"
                           " WHERE category='automation'").fetchone()
        assert conn.execute("SELECT last_fired_at FROM rules").fetchone()[0] is not None
    assert tuple(row) == ("automation", "on", "Porch light", "Rule: Porch on arrival", None)
    page = client.get("/controls").get_data(as_text=True)
    assert "Rule: Porch on arrival" in page and "Automated by 1 rule" in page


# ---------------------------------------------------------------- builder and admin pages

def test_builder_offers_names_and_labels(app):
    setup(app)
    client = admin(app)
    page = client.get("/admin/rules/new/input").get_data(as_text=True)
    assert "Front door is Open" in page and "Front door is Shut" in page
    assert "Turn on Porch light" in page and "Pulse Gate (1 s)" in page
    sensor_page = client.get("/admin/rules/new/sensor").get_data(as_text=True)
    assert "Living Room Temperature (°C)" in sensor_page


def test_rule_list_reads_as_a_sentence(app):
    _, _, temp, aircon, _ = setup(app)
    client = admin(app)
    new_rule(client, "sensor", source=str(temp), comparator="above", threshold="26",
             reset_threshold="24", target=f"{aircon}:on", undo="on")
    page = client.get("/admin/rules").get_data(as_text=True)
    text = page.replace("<strong>", "").replace("</strong>", "")
    assert ("When Living Room Temperature is above 26.0 °C, turn on Living Room Aircon, "
            "and turn it off again when it drops to 24.0 °C.") in text
    with db(app) as conn:
        assert conn.execute("SELECT name FROM rules").fetchone()[0] == \
            "Living Room Temperature above 26.0 °C: turn on Living Room Aircon"


def test_renaming_keeps_rule_state(app, engine):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on", undo="on")
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    rid = rule_ids(app)[0]
    post(client, f"/admin/rules/{rid}", {"name": "Renamed", "source": f"{door}:active",
                                         "target": f"{light}:on", "undo": "on", "enabled": "on"})
    assert engine.status(rid).active                          # still knows the door is open
    io(app).simulate_input(door, False)
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(light)


def test_points_in_use_are_protected(app):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on")
    post(client, f"/admin/io/{door}/delete", {"confirm": "yes"}, page=f"/admin/io/{door}")
    assert io(app).get(door) is not None
    resp = post(client, f"/admin/io/{light}", {"name": "Porch light", "pin": "17", "mode": "pulse",
                                               "trigger": "high", "pulse_value": "1", "pulse_unit": "s"})
    assert resp.status_code == 400 and "wouldn" in resp.get_data(as_text=True)


def test_delete_rule(app):
    door, light, *_ = setup(app)
    client = admin(app)
    new_rule(client, "input", source=f"{door}:active", target=f"{light}:on")
    rid = rule_ids(app)[0]
    post(client, f"/admin/rules/{rid}/delete", {"confirm": "yes"}, page=f"/admin/rules/{rid}")
    assert rule_ids(app) == []


def test_rule_pick_page_explains_what_is_missing(app):
    client = admin(app)
    assert "Add an output first" in client.get("/admin/rules").get_data(as_text=True)
    add_point(app, kind="output", name="Porch light", pin=17)
    page = client.get("/admin/rules/new").get_data(as_text=True)
    assert "Add an input</a> first" in page and "Add a sensor</a> first" in page
