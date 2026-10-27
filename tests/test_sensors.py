# SPDX-License-Identifier: GPL-2.0-or-later
"""Sensors: drivers, polling, simulation and the admin pages."""
from __future__ import annotations

import time

import pytest

from app import sensors
from app.hardware import IOManager, IOPoint
from conftest import add_point, add_user, db, io, login, post


# ---------------------------------------------------------------- DS18B20

def make_probe(root, pid="28-0316a2795cff", temperature=None, w1_slave=None):
    d = root / pid
    d.mkdir(parents=True)
    if temperature is not None:
        (d / "temperature").write_text(temperature)
    if w1_slave is not None:
        (d / "w1_slave").write_text(w1_slave)
    return pid


def test_ds18b20_temperature_file(tmp_path):
    pid = make_probe(tmp_path, temperature="23125\n")
    assert sensors.read_ds18b20(pid, root=str(tmp_path)) == 23.125
    assert sensors.discover_ds18b20(root=str(tmp_path)) == [pid]


def test_ds18b20_older_w1_slave_file(tmp_path):
    pid = make_probe(tmp_path, w1_slave="72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n")
    assert sensors.read_ds18b20(pid, root=str(tmp_path)) == 23.125


@pytest.mark.parametrize("kwargs, message", [
    ({"w1_slave": "72 01 : crc=57 NO\n72 01 t=23125\n"}, "checksum"),
    ({"temperature": "85000"}, "Implausible"),
    ({"temperature": ""}, "didn't answer"),
])
def test_ds18b20_bad_readings(tmp_path, kwargs, message):
    pid = make_probe(tmp_path, **kwargs)
    with pytest.raises(sensors.SensorError, match=message):
        sensors.read_ds18b20(pid, root=str(tmp_path))


def test_ds18b20_missing_probe(tmp_path):
    with pytest.raises(sensors.SensorError, match="not found"):
        sensors.read_ds18b20("28-000000000000", root=str(tmp_path))


# ---------------------------------------------------------------- DHT22 (kernel IIO)

def test_dht_reads_kernel_iio_files(tmp_path):
    d = tmp_path / "iio:device0"
    d.mkdir()
    (d / "name").write_text("dht11@4\n")
    (d / "in_temp_input").write_text("21600\n")
    (d / "in_humidityrelative_input").write_text("48300\n")
    assert sensors.discover_dht(root=str(tmp_path)) == [("iio:device0", "dht11@4")]
    assert sensors.read_dht("iio:device0", "temperature", root=str(tmp_path)) == 21.6
    assert sensors.read_dht("iio:device0", "humidity", root=str(tmp_path)) == 48.3


def test_dht_retries_then_reports(tmp_path, monkeypatch):
    d = tmp_path / "iio:device0"
    d.mkdir()
    monkeypatch.setattr(sensors.time, "sleep", lambda s: None)
    with pytest.raises(sensors.SensorError, match="after 3 tries"):
        sensors.read_dht("iio:device0", "temperature", root=str(tmp_path))


# ---------------------------------------------------------------- BME280

DATASHEET = sensors.BMECalibration(T1=27504, T2=26435, T3=-1000, P1=36477, P2=-10685, P3=3024,
                                   P4=2855, P5=140, P6=-7, P7=15500, P8=-14600, P9=6000)


def test_bme_compensation_matches_bosch_datasheet_example():
    # Worked example from the Bosch datasheet: adc_T=519888 -> 25.08 °C, adc_P=415148 -> 100653.27 Pa
    assert round(sensors.bme_temperature(519888, DATASHEET), 2) == 25.08
    assert round(sensors.bme_pressure(415148, 519888, DATASHEET) * 100, 2) == 100653.27


class FakeBus:
    """Registers of a BME280 holding the datasheet calibration and raw readings."""
    def __init__(self, chip_id=0x60):
        c = DATASHEET
        def le(v): v &= 0xFFFF; return [v & 0xFF, v >> 8]
        self.regs = {0xD0: chip_id, 0xA1: 75}
        calib = sum((le(x) for x in (c.T1, c.T2, c.T3, c.P1, c.P2, c.P3, c.P4, c.P5, c.P6, c.P7, c.P8, c.P9)), [])
        for i, b in enumerate(calib):
            self.regs[0x88 + i] = b
        for i, b in enumerate([0x6A, 0x01, 0x00, 0x13, 0x27, 0x03, 0x1E]):   # H2..H6
            self.regs[0xE1 + i] = b
        adc_p, adc_t, adc_h = 415148, 519888, 27000
        data = [adc_p >> 12, (adc_p >> 4) & 0xFF, (adc_p & 0xF) << 4,
                adc_t >> 12, (adc_t >> 4) & 0xFF, (adc_t & 0xF) << 4, adc_h >> 8, adc_h & 0xFF]
        for i, b in enumerate(data):
            self.regs[0xF7 + i] = b
        self.writes = []

    def read_byte_data(self, addr, reg):
        return self.regs[reg]

    def read_i2c_block_data(self, addr, reg, n):
        return [self.regs[reg + i] for i in range(n)]

    def write_byte_data(self, addr, reg, value):
        self.writes.append((reg, value))

    def close(self):
        pass


def bme_with(bus, monkeypatch):
    monkeypatch.setattr(sensors.time, "sleep", lambda s: None)
    dev = sensors.BME280Device(1, 0x76)
    dev._bus = bus
    return dev


def test_bme280_device_reads_all_three(monkeypatch):
    dev = bme_with(FakeBus(), monkeypatch)
    assert round(dev.read("temperature"), 2) == 25.08
    assert round(dev.read("pressure"), 2) == 1006.53
    assert 0 <= dev.read("humidity") <= 100
    assert dev.chip == "BME280"
    assert (0xF2, 0x01) in dev._bus.writes                    # humidity oversampling set first


def test_bmp280_has_no_humidity(monkeypatch):
    dev = bme_with(FakeBus(chip_id=0x58), monkeypatch)
    assert round(dev.read("temperature"), 2) == 25.08
    with pytest.raises(sensors.SensorError, match="doesn't measure humidity"):
        dev.read("humidity")
    assert all(reg != 0xF2 for reg, _ in dev._bus.writes)    # BMP280 has no ctrl_hum


def test_wrong_chip_is_reported(monkeypatch):
    with pytest.raises(sensors.SensorError, match="isn't a BME280"):
        bme_with(FakeBus(chip_id=0x99), monkeypatch).read("temperature")


def test_i2c_address_parsing():
    assert sensors.parse_i2c_address("0x76") == 0x76 and sensors.parse_i2c_address("77") == 0x77
    assert sensors.parse_i2c_address("zz") is None


def test_value_formatting():
    assert sensors.format_value(23.456, "temperature") == "23.5 °C"
    assert sensors.format_value(48.6, "humidity") == "49%"
    assert sensors.format_value(1013.2, "pressure") == "1013 hPa"
    assert sensors.format_value(None, "temperature") == "No reading yet"


# ---------------------------------------------------------------- polling (hardware mode, faked reads)

def sensor_point(pid=1, **kw):
    fields = dict(id=pid, kind="sensor", name="Living Room Temperature", pin=None,
                  sensor_type="ds18b20", quantity="temperature", address="28-0316a2795cff",
                  poll_seconds=30, calibration=-0.5)
    fields.update(kw)
    return IOPoint(**fields)


def test_poller_applies_calibration_and_marks_stale(monkeypatch):
    readings = iter([23.0])
    monkeypatch.setattr(sensors, "read_point", lambda *a: next(readings))
    m = IOManager("hardware")
    monkeypatch.setattr(m, "_start_poller", lambda: None)
    m.sync([sensor_point()])
    m.poll_due()
    assert m.sensor_value(1) == 22.5                          # 23.0 - 0.5 correction
    assert m.state_text(m.get(1)) == "22.5 °C"
    m.readings[1].fresh_until = time.monotonic() - 1           # pretend it's old
    assert m.sensor_value(1) is None
    m.close()


def test_poller_keeps_going_after_an_error(monkeypatch):
    def failing(*a):
        raise sensors.SensorError("Probe 28-0316a2795cff not found.")
    monkeypatch.setattr(sensors, "read_point", failing)
    m = IOManager("hardware")
    monkeypatch.setattr(m, "_start_poller", lambda: None)
    m.sync([sensor_point()])
    m.poll_due()
    assert m.sensor_value(1) is None
    assert m.state_text(m.get(1)) == "Unavailable: Probe 28-0316a2795cff not found."
    m.close()


def test_poll_interval_is_respected(monkeypatch):
    calls = []
    monkeypatch.setattr(sensors, "read_point", lambda *a: calls.append(1) or 20.0)
    m = IOManager("hardware")
    monkeypatch.setattr(m, "_start_poller", lambda: None)
    m.sync([sensor_point(poll_seconds=30)])
    m.poll_due()
    m.poll_due()
    assert len(calls) == 1
    m.close()


# ---------------------------------------------------------------- simulation and pages

def admin(app):
    add_user(app)
    client = app.test_client()
    login(client)
    return client


def test_simulated_sensor_on_controls(app):
    pid = add_point(app, kind="sensor", name="Living Room Temperature", pin=None,
                    sensor_type="ds18b20", quantity="temperature", address="28-00000a1b2c3d")
    client = admin(app)
    page = client.get("/controls").get_data(as_text=True)
    assert "Living Room Temperature" in page and "22.0 °C" in page
    post(client, f"/controls/{pid}/simulate-reading", {"value": "27.5"}, page="/controls")
    assert io(app).sensor_value(pid) == 27.5
    assert "27.5 °C" in client.get("/controls").get_data(as_text=True)
    state = {p["id"]: p for p in client.get("/controls/state").get_json()["points"]}
    assert state[pid]["value"] == 27.5 and state[pid]["state_text"] == "27.5 °C"


def test_add_ds18b20_from_admin(app):
    client = admin(app)
    page = client.get("/admin/io/new/sensor/ds18b20").get_data(as_text=True)
    assert "28-00000a1b2c3d" in page                         # simulated discovery
    resp = post(client, "/admin/io/new/sensor/ds18b20", {
        "name": "Geyser Temperature", "quantity": "temperature", "address": "28-00000a1b2c3d",
        "poll_seconds": "15", "calibration": "-0.3"})
    assert resp.status_code == 302
    with db(app) as conn:
        row = conn.execute("SELECT * FROM io_points WHERE kind='sensor'").fetchone()
    assert (row["sensor_type"], row["address"], row["pin"], row["calibration"]) == ("ds18b20", "28-00000a1b2c3d", None, -0.3)
    assert "Geyser Temperature" in client.get("/admin/io").get_data(as_text=True)


def test_add_bme280_readings_share_the_board(app):
    client = admin(app)
    for q in ("temperature", "humidity"):
        assert post(client, "/admin/io/new/sensor/bme280", {
            "name": f"Lounge {q}", "quantity": q, "address": "0x76", "poll_seconds": "30",
            "calibration": "0"}).status_code == 302
    with db(app) as conn:
        assert conn.execute("SELECT COUNT(*) FROM io_points WHERE address='0x76'").fetchone()[0] == 2


def test_add_dht22_reserves_its_pin(app):
    client = admin(app)
    add_point(app, name="Porch light", pin=17)
    resp = post(client, "/admin/io/new/sensor/dht22", {
        "name": "Shed humidity", "quantity": "humidity", "address": "iio:device0", "pin": "17",
        "poll_seconds": "10", "calibration": "0"})
    assert resp.status_code == 400 and "already used by Porch light" in resp.get_data(as_text=True)
    assert post(client, "/admin/io/new/sensor/dht22", {
        "name": "Shed humidity", "quantity": "humidity", "address": "iio:device0", "pin": "22",
        "poll_seconds": "10", "calibration": "0"}).status_code == 302


@pytest.mark.parametrize("stype, form, message", [
    ("ds18b20", {"quantity": "humidity", "address": "28-00000a1b2c3d"}, "measure that"),
    ("ds18b20", {"quantity": "temperature", "address": "not-a-probe"}, "Choose a probe"),
    ("bme280", {"quantity": "pressure", "address": "0x40"}, "0x76 or 0x77"),
    ("dht22", {"quantity": "temperature", "address": "iio:device0", "poll_seconds": "1"}, "every 3 to 3600"),
    ("bme280", {"quantity": "temperature", "address": "0x76", "calibration": "abc"}, "must be a number"),
])
def test_sensor_form_validation(app, stype, form, message):
    client = admin(app)
    data = {"name": "Test", "poll_seconds": "30", "calibration": "0", "pin": "22", **form}
    resp = post(client, f"/admin/io/new/sensor/{stype}", data)
    assert resp.status_code == 400 and message in resp.get_data(as_text=True)


def test_i2c_pins_are_kept_free_for_a_bme280(app):
    from conftest import add_point, add_user, login, post
    add_user(app)
    client = app.test_client()
    login(client)
    relay = add_point(app, kind="output", name="Pump", pin=2)
    resp = post(client, "/admin/io/new/sensor/bme280", {"name": "Hall", "quantity": "temperature",
                                                       "address": "0x76", "poll_seconds": "30"})
    assert resp.status_code == 400 and "Pump is on GPIO 2" in resp.get_data(as_text=True)
    post(client, f"/admin/io/{relay}/delete", {"confirm": "yes"}, page=f"/admin/io/{relay}")
    resp = post(client, "/admin/io/new/sensor/bme280", {"name": "Hall", "quantity": "temperature",
                                                       "address": "0x76", "poll_seconds": "30"})
    assert resp.status_code == 302
    page = client.get("/admin/io/new/output").get_data(as_text=True)
    assert "used by I²C for the BME280" in page
    resp = post(client, "/admin/io/new/output", {"name": "Pump", "pin": "3", "mode": "toggle", "trigger": "high"})
    assert resp.status_code == 400 and "GPIO 3 is used by I²C" in resp.get_data(as_text=True)
