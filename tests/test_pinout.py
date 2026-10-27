# SPDX-License-Identifier: GPL-2.0-or-later
"""The Pinout page and the live pin maps on Controls."""
from __future__ import annotations

import re
from pathlib import Path

from conftest import add_point, add_user, io, login, post

from app import pinout
from app.hardware import PHYSICAL_PIN

ROOT = Path(__file__).resolve().parent.parent


def signed_in(app, role="admin", username="admin"):
    add_user(app, username=username, role=role)
    client = app.test_client()
    login(client, username=username)
    return client


def pin_tag(page: str, physical: int, compact: bool = False) -> str:
    """The opening tag of a header pin's button."""
    tags = re.findall(rf'<button type="button" class="hpin[^"]*" data-physical="{physical}"[^>]*>', page)
    assert tags, f"pin {physical} not found"
    return tags[-1] if compact else tags[0]


# ---------------------------------------------------------------- the data

def test_header_matches_the_pi_5():
    header = pinout.data()["header"]
    assert [p["physical"] for p in header] == list(range(1, 41))
    by_type = {}
    for p in header:
        by_type.setdefault(p["type"], []).append(p["physical"])
    assert by_type["3v3"] == [1, 17]
    assert by_type["5v"] == [2, 4]
    assert by_type["ground"] == [6, 9, 14, 20, 25, 30, 34, 39]
    gpio = {p["bcm"]: p["physical"] for p in header if p["type"] == "gpio"}
    assert sorted(gpio) == list(range(28))
    assert (gpio[0], gpio[1]) == (27, 28)            # ID EEPROM pins
    for bcm, physical in PHYSICAL_PIN.items():       # the pins CoilGate offers
        assert gpio[bcm] == physical, bcm


def test_rp1_functions_and_power_on_pulls():
    d = pinout.data()
    fn = d["functions"]
    assert all(len(fn[str(b)]) == 9 for b in range(28))
    assert fn["2"][3] == "I2C1 SDA" and fn["3"][3] == "I2C1 SCL"
    assert fn["14"][4] == "UART0 TXD" and fn["15"][4] == "UART0 RXD"
    assert fn["12"][0] == "PWM0 CHAN0" and fn["18"][3] == "PWM0 CHAN2"
    assert fn["4"][0] == "GPCLK0"
    assert all(fn[str(b)][7] == f"PIO{b}" for b in range(28))      # PIO reaches every pin
    assert [d["pulls"][str(b)] for b in range(28)] == ["up"] * 9 + ["down"] * 19
    i2c = next(i for i in d["interfaces"] if i["id"] == "i2c")
    assert {k for k, v in i2c["pins"].items() if v["main"]} == {"3", "5"}
    assert next(i for i in d["interfaces"] if i["id"] == "1wire")["pins"] == {
        "7": {"label": "1-Wire data (default pin)", "main": True}}


def test_interface_pins_only_claim_functions_the_pi_5_has():
    d = pinout.data()
    gpio_at = {str(p["physical"]): p["bcm"] for p in d["header"] if p["type"] == "gpio"}
    words = {"i2c": "I2C", "uart": "UART", "pwm": "PWM", "pio": "PIO", "dpi": "DPI", "sdio": "SD0"}
    for iface in d["interfaces"]:
        if iface["id"] not in words:
            continue
        for physical in iface["pins"]:
            names = d["functions"][str(gpio_at[physical])]
            assert any(n.startswith(words[iface["id"]]) for n in names), (iface["id"], physical)


def test_licence_files_and_attribution_ship_with_the_data():
    folder = ROOT / "app" / "pinout_data"
    assert "Attribution-ShareAlike 4.0" in (folder / "LICENSE-CC-BY-SA-4.0.txt").read_text(encoding="utf-8")
    notice = (folder / "NOTICE.md").read_text(encoding="utf-8")
    assert "Pinout.xyz" in notice and "CC BY-SA 4.0" in notice and "pinctrl" in notice
    assert pinout.data()["source_commit"] in notice
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Pinout.xyz" in readme and "CC BY-SA 4.0" in readme


# ---------------------------------------------------------------- the text renderer

def test_descriptions_are_escaped():
    html = str(pinout.render_text(
        "Hi <script>x</script> **bold** `a<b`\n\n* [ok](https://pinout.xyz) [bad](javascript:alert(1))\n\n"
        "| A | B |\n| --- | --- |\n| <i>1</i> | 2 |\n\n```\n<pre>\n```"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>bold</strong>" in html and "<code>a&lt;b</code>" in html
    assert 'href="https://pinout.xyz" rel="noopener noreferrer"' in html
    assert "javascript:" not in html and ">bad<" not in html and "bad" in html
    assert "<td>&lt;i&gt;1&lt;/i&gt;</td>" in html
    assert "<pre><code>&lt;pre&gt;</code></pre>" in html


def test_every_description_renders():
    for iface in pinout.interfaces():
        assert str(iface["html"]).startswith("<"), iface["id"]
        assert iface["map"]


# ---------------------------------------------------------------- the Pinout page

def test_pinout_page_needs_a_sign_in(client):
    assert client.get("/pinout").status_code == 302


def test_pinout_page_shows_all_pins_and_credits_pinout_xyz(app):
    client = signed_in(app, role="user", username="viewer")
    page = client.get("/pinout").get_data(as_text=True)
    assert len(re.findall(r'class="hpin hpin-(?:left|right)"', page)) == 40
    assert len(re.findall(r"data-pin-detail=", page)) == 40
    assert 'data-iface="i2c"' in page and 'data-iface="pio"' in page
    assert "https://pinout.xyz" in page and "CC BY-SA 4.0" in page
    assert "Pin 11: GPIO 17" in page and "GPCLK0" in page
    assert "No inputs, outputs or sensors are set up yet." in page
    assert "pins.js" in page and ' style="' not in page and "<script>" not in page


def test_pinout_page_lists_and_lights_the_pins_in_use(app):
    light = add_point(app, name="Porch light", pin=17)
    add_point(app, kind="input", name="Doorbell", pin=5, wiring="to_ground", bounce_ms=0)
    add_point(app, kind="sensor", name="Garden", pin=None, sensor_type="ds18b20",
              quantity="temperature", address="28-00000a1b2c3d")
    add_point(app, kind="sensor", name="Hall", pin=None, sensor_type="bme280",
              quantity="humidity", address="0x76")
    client = signed_in(app)
    page = client.get("/pinout").get_data(as_text=True)
    tag = pin_tag(page, 11)
    assert f'data-pin-point="{light}"' in tag and 'data-kind="output"' in tag
    assert 'data-used="yes"' in pin_tag(page, 7)        # 1-Wire on GPIO 4
    assert 'data-used="yes"' in pin_tag(page, 3) and 'data-used="yes"' in pin_tag(page, 5)
    assert "data-pin-point" not in pin_tag(page, 7)    # sensors don't light up
    assert 'data-used="no"' in pin_tag(page, 13)
    assert "1-Wire data" in page and "I²C SDA" in page and "Doorbell" in page


# ---------------------------------------------------------------- Controls

def test_controls_pins_follow_outputs_including_active_low(app):
    light = add_point(app, name="Porch light", pin=17)
    fan = add_point(app, name="Fan", pin=22, active_high=0)
    client = signed_in(app)
    page = client.get("/controls").get_data(as_text=True)
    assert "GPIO 17 · pin 11" in page and "Full pinout" in page
    assert 'class="pin-strip"' in page and 'class="pi-header compact"' in page
    for compact in (False, True):
        light_pin, fan_pin = pin_tag(page, 11, compact), pin_tag(page, 15, compact)
        assert 'data-on="no"' in light_pin and 'data-level="L"' in light_pin
        assert 'data-on="no"' in fan_pin and 'data-level="H"' in fan_pin   # off = high
    post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    post(client, f"/controls/{fan}", {"do": "on"}, page="/controls")
    page = client.get("/controls").get_data(as_text=True)
    assert 'data-on="yes"' in pin_tag(page, 11) and 'data-level="H"' in pin_tag(page, 11)
    assert 'data-on="yes"' in pin_tag(page, 15) and 'data-level="L"' in pin_tag(page, 15)
    strip = re.search(rf'<a class="spin" href="#io-{light}"[^>]*>', page).group(0)
    assert 'data-on="yes"' in strip


def test_controls_pins_follow_inputs(app):
    door = add_point(app, kind="input", name="Front door", pin=5, wiring="to_ground", bounce_ms=0)
    client = signed_in(app)
    tag = pin_tag(client.get("/controls").get_data(as_text=True), 29)
    assert 'data-kind="input"' in tag and 'data-active-level="low"' in tag
    assert 'data-on="no"' in tag and 'data-level="H"' in tag        # pulled up, idle
    post(client, f"/controls/{door}/simulate", page="/controls")
    tag = pin_tag(client.get("/controls").get_data(as_text=True), 29)
    assert 'data-on="yes"' in tag and 'data-level="L"' in tag


def test_controls_without_points_has_no_pin_map(app):
    client = signed_in(app)
    page = client.get("/controls").get_data(as_text=True)
    assert "pi-header" not in page and "pin-strip" not in page


def test_state_can_leave_out_activity(app):
    light = add_point(app)
    client = signed_in(app)
    post(client, f"/controls/{light}", {"do": "on"}, page="/controls")
    assert client.get("/controls/state").get_json()["activity"]
    data = client.get("/controls/state?activity=0").get_json()
    assert data["activity"] == [] and data["points"][0]["on"] is True


def test_scripts_are_served(client):
    for name in ("pins.js", "live.js"):
        resp = client.get(f"/static/{name}")
        assert resp.status_code == 200
        resp.close()
