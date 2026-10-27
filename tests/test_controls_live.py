# SPDX-License-Identifier: GPL-2.0-or-later
"""Controls without page reloads: JSON answers, the activity panel, live updates."""
from __future__ import annotations

import re

from conftest import add_point, add_user, csrf, db, io, login, post

JSON = {"Accept": "application/json"}


def signed_in(app, **user):
    add_user(app, **user)
    client = app.test_client()
    login(client, username=user.get("username", "admin"))
    return client


def press(client, point_id, action, since=0, page="/controls"):
    token = csrf(client, page)
    return client.post(f"/controls/{point_id}", headers=JSON,
                       data={"csrf_token": token, "do": action, "since": str(since)})


def test_button_answers_in_json_with_state_and_new_activity(app):
    light = add_point(app)
    client = signed_in(app)
    resp = press(client, light, "on")
    assert resp.status_code == 200 and resp.is_json
    data = resp.get_json()
    assert data["ok"] is True and data["level"] == "success"
    assert data["message"] == "Porch light turned on."
    assert next(p for p in data["points"] if p["id"] == light)["on"] is True
    entry = data["activity"][0]
    assert (entry["what"], entry["change"], entry["by"]) == ("Porch light", "Turned on", "admin")
    # Asking again with the newest id returns nothing new.
    data = press(client, light, "off", since=entry["id"]).get_json()
    assert [e["change"] for e in data["activity"]] == ["Turned off"]
    assert io(app).is_on(light) is False


def test_refusals_come_back_as_messages(app):
    light = add_point(app)
    client = signed_in(app)
    data = press(client, light, "pulse").get_json()
    assert data["ok"] is False and data["level"] == "error"
    assert "isn't available" in data["message"]
    with db(app) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('max_control_actions_per_minute', '1')")
    press(client, light, "on")
    data = press(client, light, "off").get_json()
    assert data["ok"] is False and "Too many actions" in data["message"]


def test_expired_form_gives_a_json_message(app):
    light = add_point(app)
    client = signed_in(app)
    client.get("/controls")
    resp = client.post(f"/controls/{light}", headers=JSON, data={"csrf_token": "stale", "do": "on"})
    assert resp.status_code == 400 and resp.is_json
    assert "Reload" in resp.get_json()["message"]


def test_without_javascript_the_page_returns_to_the_same_row(app):
    light = add_point(app)
    client = signed_in(app)
    resp = post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/controls#io-{light}")
    page = client.get("/controls").get_data(as_text=True)
    # The message is shown in the activity panel's status line, not above the page.
    assert re.search(r'class="dock-status"[^>]*data-level="success">Porch light turned on\.<', page)
    assert 'class="flashes"' not in page


def test_activity_panel_lists_recent_changes(app):
    light = add_point(app)
    door = add_point(app, kind="input", name="Front door", pin=5, on_label="Open", off_label="Shut")
    client = signed_in(app)
    press(client, light, "on")
    io(app).simulate_input(door, True)
    page = client.get("/controls").get_data(as_text=True)
    assert 'id="activity-dock"' in page and "has-dock" in page
    assert "Front door" in page and ">Open<" in page and ">Input<" in page
    last_id = int(re.search(r'data-last-id="(\d+)"', page).group(1))
    assert last_id > 0
    # The live endpoint only sends what's newer than the page already has.
    assert client.get(f"/controls/state?since={last_id}").get_json()["activity"] == []
    io(app).simulate_input(door, False)
    new = client.get(f"/controls/state?since={last_id}").get_json()["activity"]
    assert [(e["what"], e["change"]) for e in new] == [("Front door", "Shut")]


def test_simulated_reading_answers_in_json(app):
    temp = add_point(app, kind="sensor", name="Hall Temperature", pin=None,
                     sensor_type="ds18b20", quantity="temperature", address="28-00000a1b2c3d")
    client = signed_in(app)
    token = csrf(client, "/controls")
    data = client.post(f"/controls/{temp}/simulate-reading", headers=JSON,
                       data={"csrf_token": token, "value": "23.4"}).get_json()
    assert data["ok"] and "23.4 °C" in data["message"]
    data = client.post(f"/controls/{temp}/simulate-reading", headers=JSON,
                       data={"csrf_token": token, "value": "warm"}).get_json()
    assert data["ok"] is False and "Enter a number" in data["message"]


def test_no_panel_when_nothing_is_set_up(app):
    client = signed_in(app)
    page = client.get("/controls").get_data(as_text=True)
    assert "activity-dock" not in page and "Nothing to control yet" in page
