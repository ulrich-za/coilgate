# SPDX-License-Identifier: GPL-2.0-or-later
"""The Controls page: outputs, inputs, live state, logging, limits."""
from __future__ import annotations

import time

from conftest import add_point, add_user, csrf, db, io, login, post


def signed_in(app, role="admin", username="admin"):
    add_user(app, username=username, role=role)
    client = app.test_client()
    login(client, username=username)
    return client


def press(client, point_id, action):
    return post(client, f"/controls/{point_id}", {"do": action}, page="/controls")


def test_outputs_start_off(app):
    light = add_point(app)
    fan = add_point(app, name="Fan", pin=22, active_high=0)
    assert not io(app).is_on(light) and not io(app).is_on(fan)
    assert io(app)._devices[fan].pin.state == 1         # active-low board: HIGH means off


def test_switch_output_on_and_off(app):
    light = add_point(app)
    client = signed_in(app)
    assert press(client, light, "on").status_code == 302
    assert io(app).is_on(light) and io(app)._devices[light].pin.state == 1
    page = client.get("/controls").get_data(as_text=True)
    assert "Porch light turned on." in page and "Turn off" in page
    press(client, light, "off")
    assert not io(app).is_on(light)


def test_active_low_output_drives_pin_low_when_on(app):
    fan = add_point(app, name="Fan", pin=22, active_high=0)
    press(signed_in(app), fan, "on")
    assert io(app)._devices[fan].pin.state == 0


def test_users_can_control_outputs(app):
    light = add_point(app)
    client = signed_in(app, role="user", username="anna")
    press(client, light, "on")
    assert io(app).is_on(light)


def test_repeated_on_is_harmless(app):
    light = add_point(app)
    client = signed_in(app)
    press(client, light, "on")
    press(client, light, "on")
    assert io(app).is_on(light)


def test_short_pulse(app):
    gate = add_point(app, name="Gate", pin=27, mode="pulse", pulse_seconds=0.3)
    client = signed_in(app)
    press(client, gate, "pulse")
    assert io(app).is_on(gate) and io(app).is_pulsing(gate)
    press(client, gate, "pulse")
    assert "already active" in client.get("/controls").get_data(as_text=True)
    time.sleep(0.45)
    assert not io(app).is_on(gate)


def test_long_pulse_can_be_stopped_early(app):
    pump = add_point(app, name="Pump", pin=23, mode="pulse", pulse_seconds=2 * 3600)
    client = signed_in(app)
    press(client, pump, "pulse")
    page = client.get("/controls").get_data(as_text=True)
    assert "2 hours pulse" in page and ("2:00:00 left" in page or "1:59:5" in page)
    assert ">Stop</button>" in page
    press(client, pump, "stop")
    assert not io(app).is_on(pump) and not io(app).is_pulsing(pump)
    with db(app) as conn:
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM activity WHERE category='control' ORDER BY id")]
    assert actions == ["pulse", "stop"]


def test_wrong_action_for_mode_does_nothing(app):
    light = add_point(app)
    gate = add_point(app, name="Gate", pin=27, mode="pulse", pulse_seconds=1)
    client = signed_in(app)
    press(client, gate, "on")
    press(client, light, "pulse")
    assert not io(app).is_on(gate) and not io(app).is_on(light)


def test_unknown_output_is_404(app):
    assert press(signed_in(app), 999, "on").status_code == 404


def test_inputs_cannot_be_switched(app):
    door = add_point(app, kind="input", name="Front door", pin=5)
    assert press(signed_in(app), door, "on").status_code == 404


def test_control_without_csrf_is_rejected(app):
    light = add_point(app)
    client = signed_in(app)
    client.get("/controls")
    assert client.post(f"/controls/{light}", data={"do": "on"}).status_code == 400
    assert not io(app).is_on(light)


def test_actions_are_logged_with_user_and_ip(app):
    light = add_point(app)
    client = signed_in(app)
    press(client, light, "on")
    with db(app) as conn:
        row = conn.execute("SELECT category, action, target, username, ip FROM activity").fetchone()
    assert tuple(row) == ("control", "on", "Porch light", "admin", "127.0.0.1")
    assert "Turned on" in client.get("/controls").get_data(as_text=True)


def test_control_rate_limit(make_app):
    app = make_app(max_control_actions_per_minute=3)
    light = add_point(app)
    client = signed_in(app)
    for action in ("on", "off", "on"):
        press(client, light, action)
    press(client, light, "off")                     # 4th within a minute: refused
    assert io(app).is_on(light)
    assert "Too many actions" in client.get("/controls").get_data(as_text=True)


# ---------------------------------------------------------------- inputs

def test_input_shows_its_labels(app):
    door = add_point(app, kind="input", name="Front door", pin=5, wiring="to_ground",
                     on_label="Shut", off_label="Open")
    client = signed_in(app)
    assert ">Open<" in client.get("/controls").get_data(as_text=True)
    post(client, f"/controls/{door}/simulate", page="/controls")
    time.sleep(0.15)                                # debounce
    assert ">Shut<" in client.get("/controls").get_data(as_text=True)


def test_input_changes_are_logged(app):
    door = add_point(app, kind="input", name="Front door", pin=5, bounce_ms=0)
    client = signed_in(app)
    post(client, f"/controls/{door}/simulate", page="/controls")
    post(client, f"/controls/{door}/simulate", page="/controls")
    with db(app) as conn:
        rows = conn.execute("SELECT action, target, detail, username FROM activity"
                            " WHERE category='input' ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("active", "Front door", "Closed", None),
                                        ("inactive", "Front door", "Open", None)]
    assert ">Input<" in client.get("/controls").get_data(as_text=True)


def test_each_input_wiring_reads_correctly(app):
    ids = {w: add_point(app, kind="input", name=w, pin=pin, wiring=w, bounce_ms=0)
           for w, pin in (("to_ground", 5), ("to_3v3", 6), ("active_high", 13), ("active_low", 19))}
    for wiring, pid in ids.items():
        assert not io(app).is_on(pid), wiring
        io(app).simulate_input(pid, True)
        assert io(app).is_on(pid), wiring
        io(app).simulate_input(pid, False)
        assert not io(app).is_on(pid), wiring


# ---------------------------------------------------------------- live state

def test_state_endpoint(app):
    light = add_point(app)
    door = add_point(app, kind="input", name="Front door", pin=5)
    client = signed_in(app)
    press(client, light, "on")
    data = client.get("/controls/state").get_json()
    by_id = {p["id"]: p for p in data["points"]}
    assert by_id[light]["on"] is True and by_id[light]["state_text"] == "On"
    assert by_id[door]["on"] is False and by_id[door]["state_text"] == "Open"
    assert data["simulated"] is True


def test_empty_state_differs_for_admins_and_users(app):
    admin = signed_in(app)
    assert "Add an output" in admin.get("/controls").get_data(as_text=True)
    user = signed_in(app, role="user", username="anna")
    page = user.get("/controls").get_data(as_text=True)
    assert "Add an output" not in page and "An admin needs to add" in page


def test_simulation_notice_and_buttons_only_in_simulation(app):
    add_point(app, kind="input", name="Front door", pin=5)
    page = signed_in(app).get("/controls").get_data(as_text=True)
    assert "Simulation:" in page and "Simulate a change" in page
