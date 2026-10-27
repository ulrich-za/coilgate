"""Instant updates: waiting state requests, and inputs that wake the rule engine."""
from __future__ import annotations

import threading
import time

from conftest import add_point, add_user, db, io, login, post


def signed_in(app):
    add_user(app)
    client = app.test_client()
    login(client)
    return client


def state(client, **params):
    return client.get("/controls/state", query_string=params).get_json()


def test_changes_bump_the_version(app):
    light = add_point(app)
    door = add_point(app, kind="input", name="Door", pin=5, bounce_ms=0)
    manager = io(app)
    v = manager.version
    manager.set(light, True)
    assert manager.version > v
    v = manager.version
    manager.simulate_input(door, True)
    assert manager.version > v


def test_stale_version_answers_at_once(app):
    add_point(app)
    client = signed_in(app)
    started = time.monotonic()
    data = state(client, wait="1", v=io(app).version - 1)
    assert time.monotonic() - started < 1
    assert data["version"] == io(app).version


def test_waits_until_something_changes(app):
    app.config["COILGATE_WAIT_SECONDS"] = 5
    door = add_point(app, kind="input", name="Door", pin=5, bounce_ms=0, on_label="Open", off_label="Shut")
    client = signed_in(app)
    first = state(client)
    threading.Timer(0.3, io(app).simulate_input, (door, True)).start()
    started = time.monotonic()
    data = state(client, wait="1", v=first["version"], since=0)
    took = time.monotonic() - started
    assert 0.2 < took < 3
    assert next(p for p in data["points"] if p["id"] == door)["state_text"] == "Open"
    assert data["activity"][0]["what"] == "Door"


def test_gives_up_after_the_wait_limit(app):
    app.config["COILGATE_WAIT_SECONDS"] = 0.3
    add_point(app)
    client = signed_in(app)
    v = state(client)["version"]
    started = time.monotonic()
    data = state(client, wait="1", v=v)
    assert 0.25 < time.monotonic() - started < 2 and data["version"] == v


def test_pulse_end_is_announced(app):
    gate = add_point(app, name="Gate", pin=22, mode="pulse", pulse_seconds=0.2)
    manager = io(app)
    manager.pulse(gate)
    v = manager.version
    assert manager.wait_for_change(v, 2) != v
    time.sleep(0.05)
    assert not manager.is_pulsing(gate)


def test_button_presses_report_the_version(app):
    light = add_point(app)
    client = signed_in(app)
    token_page = client.get("/controls").get_data(as_text=True)
    assert 'data-version="' in token_page
    resp = post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    assert resp.status_code == 302
    from conftest import csrf
    data = client.post(f"/controls/{light}", headers={"Accept": "application/json"},
                       data={"csrf_token": csrf(client, "/controls"), "do": "off"}).get_json()
    assert data["version"] == io(app).version


def test_input_wakes_the_rule_engine(app):
    door = add_point(app, kind="input", name="Door", pin=5, bounce_ms=0)
    light = add_point(app, kind="output", name="Light", pin=17)
    client = signed_in(app)
    post(client, "/admin/rules/new/input", {"source": f"{door}:active", "target": f"{light}:on", "enabled": "on"})
    engine = app.extensions["coilgate_rules"]
    engine.evaluate_once()
    # A slow tick shows the wake-up is what acts, not the regular check.
    original_wait = engine._wake.wait
    engine._wake.wait = lambda timeout=None: original_wait(30)
    engine.start()
    try:
        time.sleep(0.1)
        io(app).simulate_input(door, True)
        deadline = time.monotonic() + 2
        while not io(app).is_on(light) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert io(app).is_on(light)
    finally:
        engine.stop()
