"""Rules that trigger, and switch, other rules; toggle actions; the drag-and-drop builder."""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime

import pytest

from conftest import add_point, add_user, db, io, login, post
from app import rules as rules_mod
from app.db import SCHEMA_VERSION

MONDAY_10 = datetime(2026, 10, 19, 10, 0)


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


def setup(app):
    button = add_point(app, kind="input", name="Button", pin=16, bounce_ms=0,
                       on_label="Pressed", off_label="Released")
    door = add_point(app, kind="input", name="Front door", pin=5, bounce_ms=0,
                     on_label="Open", off_label="Shut")
    led = add_point(app, kind="output", name="LED", pin=17)
    siren = add_point(app, kind="output", name="Siren", pin=27)
    return button, door, led, siren


def rule(client, trigger, **fields):
    resp = post(client, f"/admin/rules/new/{trigger}", {"enabled": "on", **fields})
    assert resp.status_code == 302, resp.get_data(as_text=True)[-1500:]


def rule_id(app, name):
    with db(app) as conn:
        return conn.execute("SELECT id FROM rules WHERE name = ?", (name,)).fetchone()[0]


def enabled(app, name):
    with db(app) as conn:
        return bool(conn.execute("SELECT enabled FROM rules WHERE name = ?", (name,)).fetchone()[0])


# ---------------------------------------------------------------- toggle

def test_toggle_action_flips_an_output(app, engine):
    button, _, led, _ = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{button}:active", target=f"{led}:toggle", undo="on", name="Press to toggle")
    with db(app) as conn:
        assert conn.execute("SELECT undo FROM rules").fetchone()[0] == 0    # toggles can't be undone
    for expected in (True, False, True):
        io(app).simulate_input(button, True)
        engine.evaluate_once(MONDAY_10)
        io(app).simulate_input(button, False)
        engine.evaluate_once(MONDAY_10)
        assert io(app).is_on(led) is expected
    with db(app) as conn:
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM activity WHERE category = 'automation' ORDER BY id")]
    assert actions == ["on", "off", "on"]                     # logged as what actually happened


# ---------------------------------------------------------------- chains

def test_rule_triggered_by_another_rule(app, engine):
    _, door, led, siren = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", undo="on",
         hold_value="2", hold_unit="min", name="Door left open")
    first = rule_id(app, "Door left open")
    rule(client, "rule", source=f"{first}:active", target=f"{siren}:on", undo="on",
         hold_value="1", hold_unit="min", name="Still open")
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    engine.clock.t += 120
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(led) and not io(app).is_on(siren)
    engine.clock.t += 60
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(siren)
    io(app).simulate_input(door, False)
    engine.evaluate_once(MONDAY_10)                           # the whole chain undoes in one check
    assert not io(app).is_on(led) and not io(app).is_on(siren)


def test_chain_settles_in_one_check(app, engine):
    _, door, led, siren = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="A")
    rule(client, "rule", source=f"{rule_id(app, 'A')}:active", target=f"{siren}:on", name="B")
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(led) and io(app).is_on(siren)


def test_not_active_trigger(app, engine):
    _, door, led, siren = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", undo="on", name="A")
    rule(client, "rule", source=f"{rule_id(app, 'A')}:inactive", target=f"{siren}:on", undo="on", name="B")
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(siren)
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    assert not io(app).is_on(siren)


def test_switching_rules_on_and_off(app, engine):
    button, door, led, _ = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", undo="on", name="Alarm")
    alarm = rule_id(app, "Alarm")
    post(client, f"/admin/rules/{alarm}/toggle", page="/admin/rules")          # start disarmed
    rule(client, "input", source=f"{button}:active", target=f"r{alarm}:rule_toggle", name="Arm button")
    io(app).simulate_input(button, True)
    engine.evaluate_once(MONDAY_10)
    assert enabled(app, "Alarm")
    io(app).simulate_input(button, False)
    engine.evaluate_once(MONDAY_10)
    io(app).simulate_input(door, True)
    engine.evaluate_once(MONDAY_10)
    assert io(app).is_on(led)                                  # armed: it acts
    io(app).simulate_input(button, True)
    engine.evaluate_once(MONDAY_10)
    assert not enabled(app, "Alarm")
    with db(app) as conn:
        row = conn.execute("SELECT action, target, detail FROM activity WHERE category='automation'"
                           " AND action LIKE 'rule_%' ORDER BY id DESC").fetchone()
    assert tuple(row) == ("rule_off", "Alarm", "Rule: Arm button")


def test_switch_rule_on_with_undo(app, engine):
    button, door, led, _ = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="Night light")
    target = rule_id(app, "Night light")
    rule(client, "input", source=f"{button}:active", target=f"r{target}:rule_on", undo="on", name="Hold to enable")
    post(client, f"/admin/rules/{target}/toggle", page="/admin/rules")
    io(app).simulate_input(button, True)
    engine.evaluate_once(MONDAY_10)
    assert enabled(app, "Night light")
    io(app).simulate_input(button, False)
    engine.evaluate_once(MONDAY_10)
    assert not enabled(app, "Night light")


def test_loops_are_refused(app):
    _, door, led, siren = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="A")
    a = rule_id(app, "A")
    rule(client, "rule", source=f"{a}:active", target=f"{siren}:on", name="B")
    b = rule_id(app, "B")
    resp = post(client, f"/admin/rules/{a}", {"view": "blocks", "trigger_type": "rule", "source": f"{b}:active",
                                              "target": f"{led}:on", "enabled": "on", "name": "A"})
    assert resp.status_code == 400 and "would make a loop: A → B → A" in resp.get_data(as_text=True)


def test_rules_in_use_cannot_be_removed(app):
    _, door, led, siren = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="A")
    a = rule_id(app, "A")
    rule(client, "rule", source=f"{a}:active", target=f"{siren}:on", name="B")
    page = client.get(f"/admin/rules/{a}").get_data(as_text=True)
    assert "Other rules use this one" in page and "B" in page
    post(client, f"/admin/rules/{a}/delete", {"confirm": "yes"}, page=f"/admin/rules/{a}")
    assert rule_id(app, "A") == a


def test_a_rule_cannot_target_itself(app):
    _, door, led, _ = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="A")
    a = rule_id(app, "A")
    page = client.get(f"/admin/rules/{a}").get_data(as_text=True)
    assert f'value="r{a}:rule_' not in page
    resp = post(client, f"/admin/rules/{a}", {"source": f"{door}:active", "target": f"r{a}:rule_off",
                                              "enabled": "on", "name": "A"})
    assert resp.status_code == 400


def test_sentences(app):
    button, door, led, _ = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="Door light")
    a = rule_id(app, "Door light")
    rule(client, "input", source=f"{button}:active", target=f"r{a}:rule_on", undo="on")
    rule(client, "input", source=f"{button}:inactive", target=f"{led}:toggle")
    page = html.unescape(client.get("/admin/rules").get_data(as_text=True)).replace("<strong>", "").replace("</strong>", "")
    assert ("When Button is Pressed, switch on rule Door light, and switch it off again "
            "when that's no longer the case.") in page
    assert "When Button is Released, toggle LED." in page
    with db(app) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM rules")}
    assert "Button Pressed: switch on rule Door light" in names


def test_chained_rule_sentence(app):
    _, door, led, siren = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="Door light")
    rule(client, "rule", source=f"{rule_id(app, 'Door light')}:active", target=f"{siren}:on",
         hold_value="5", hold_unit="min")
    page = client.get("/admin/rules").get_data(as_text=True).replace("<strong>", "").replace("</strong>", "")
    assert "When rule Door light is active for at least 5 minutes, turn on Siren." in page


# ---------------------------------------------------------------- drag-and-drop builder

def test_blocks_builder_page_and_remembered_choice(app):
    _, _, led, _ = setup(app)
    client = admin(app)
    resp = client.get("/admin/rules/new?view=blocks")
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "data-blocks" in page and "blocks.js" in page
    assert 'data-slot="when"' in page and 'data-trigger="input"' in page
    assert f'data-kind="output" data-id="{led}"' in page and '["toggle", "toggle", false]' in page
    assert "coilgate_rule_builder=blocks" in resp.headers.get("Set-Cookie", "")
    # Next time, "Add a rule" opens the same builder.
    assert "data-blocks" in client.get("/admin/rules/new").get_data(as_text=True)
    client.get("/admin/rules/new?view=sentence")
    assert "What should start it?" in client.get("/admin/rules/new").get_data(as_text=True)


def test_blocks_builder_saves_like_the_sentence_builder(app):
    button, _, led, _ = setup(app)
    client = admin(app)
    resp = post(client, "/admin/rules/new", {"view": "blocks", "trigger_type": "input",
                                             "source": f"{button}:active", "target": f"{led}:on",
                                             "undo": "on", "enabled": "on", "hold_value": "0",
                                             "hold_unit": "s", "schedule_id": ""}, page="/admin/rules/new?view=blocks")
    assert resp.status_code == 302
    with db(app) as conn:
        row = conn.execute("SELECT name, trigger_type, output_id, action, undo FROM rules").fetchone()
    assert tuple(row) == ("Button Pressed: turn on LED", "input", led, "on", 1)


def test_blocks_builder_can_change_what_starts_a_rule(app):
    button, door, led, _ = setup(app)
    client = admin(app)
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="Light")
    rid = rule_id(app, "Light")
    page = client.get(f"/admin/rules/{rid}?view=blocks").get_data(as_text=True)
    assert f'value="{door}:active" data-field="source"' in page
    resp = post(client, f"/admin/rules/{rid}", {"view": "blocks", "trigger_type": "input",
                                                "source": f"{button}:inactive", "target": f"{led}:off",
                                                "enabled": "on", "name": "Light"}, page=f"/admin/rules/{rid}?view=blocks")
    assert resp.status_code == 302
    with db(app) as conn:
        row = conn.execute("SELECT source_id, input_state, action FROM rules").fetchone()
    assert tuple(row) == (button, "inactive", "off")


def test_blocks_builder_errors_stay_in_the_builder(app):
    setup(app)
    client = admin(app)
    resp = post(client, "/admin/rules/new", {"view": "blocks", "trigger_type": "", "target": ""},
                page="/admin/rules/new?view=blocks")
    assert resp.status_code == 400 and "data-blocks" in resp.get_data(as_text=True)


def test_rule_pick_offers_chaining(app):
    _, door, led, _ = setup(app)
    client = admin(app)
    page = client.get("/admin/rules/new?view=sentence").get_data(as_text=True)
    assert "Another rule is active" in page and "Add another rule first" in page
    rule(client, "input", source=f"{door}:active", target=f"{led}:on", name="A")
    page = client.get("/admin/rules/new/rule").get_data(as_text=True)
    assert "A is active" in page and "A is not active" in page


# ---------------------------------------------------------------- upgrade

def test_v5_rules_survive_the_upgrade(make_app):
    app = make_app()
    led = add_point(app, kind="output", name="LED", pin=17)
    with db(app) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE rules")
        conn.execute("""CREATE TABLE rules (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, trigger_type TEXT NOT NULL, source_id INTEGER,
            input_state TEXT, comparator TEXT, threshold REAL, reset_threshold REAL,
            trigger_schedule_id INTEGER, hold_seconds INTEGER NOT NULL DEFAULT 0,
            schedule_id INTEGER NOT NULL, output_id INTEGER NOT NULL, action TEXT NOT NULL,
            undo INTEGER NOT NULL DEFAULT 0, last_fired_at INTEGER, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL)""")
        conn.execute("INSERT INTO rules (name, trigger_type, trigger_schedule_id, schedule_id, output_id,"
                     " action, undo, created_at, updated_at) VALUES ('Evening', 'schedule', 1, 1, ?, 'on', 1, 1, 1)",
                     (led,))
        conn.execute("PRAGMA user_version = 5")
    upgraded = make_app()
    with db(upgraded) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = conn.execute("SELECT name, output_id, target_rule_id, source_rule_id, action FROM rules").fetchone()
        assert tuple(row) == ("Evening", led, None, None, "on")
        with pytest.raises(sqlite3.IntegrityError):          # exactly one target
            conn.execute("UPDATE rules SET target_rule_id = id")
