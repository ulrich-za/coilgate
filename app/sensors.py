# SPDX-License-Identifier: GPL-2.0-or-later
"""Sensor drivers: DS18B20 (1-Wire), BME280/BMP280 (I²C) and DHT22/AM2302.

Each sensor *reading* is configured as its own named point, so a BME280 can
feed "Living Room Temperature", "Living Room Humidity" and "Living Room
Pressure" separately, and rules can refer to each by name.

How each one is read on the Pi:
  DS18B20  the kernel's 1-Wire driver (dtoverlay=w1-gpio); files in /sys/bus/w1
  DHT22    the kernel's dht11 IIO driver (dtoverlay=dht11,gpiopin=N), which also
           handles DHT22/AM2302; files in /sys/bus/iio. The kernel does the
           microsecond timing, which is far more reliable than doing it in Python.
  BME280   I²C (raspi-config: enable I2C) through the small smbus2 library,
           with Bosch's compensation formulas from the datasheet.
"""
from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

W1_ROOT = "/sys/bus/w1/devices"
IIO_ROOT = "/sys/bus/iio/devices"

QUANTITIES = {
    "temperature": {"label": "Temperature", "unit": "°C", "decimals": 1, "sim": 22.0},
    "humidity": {"label": "Humidity", "unit": "%", "decimals": 0, "sim": 50.0},
    "pressure": {"label": "Pressure", "unit": "hPa", "decimals": 0, "sim": 1013.0},
}

SENSOR_TYPES = {
    "ds18b20": {
        "label": "DS18B20 temperature probe",
        "help": "1-Wire probe; several can share one data pin (GPIO 4 by default).",
        "quantities": ("temperature",),
        "min_poll": 2,
    },
    "bme280": {
        "label": "BME280 environment sensor",
        "help": "I²C board on GPIO 2 and 3. Temperature, humidity and pressure (a BMP280 has no humidity).",
        "quantities": ("temperature", "humidity", "pressure"),
        "min_poll": 1,
    },
    "dht22": {
        "label": "DHT22 / AM2302",
        "help": "Single-wire temperature and humidity sensor, read by the kernel's DHT driver.",
        "quantities": ("temperature", "humidity"),
        "min_poll": 3,
    },
}


class SensorError(Exception):
    """A reading couldn't be taken. The message is shown to the admin."""


# ---------------------------------------------------------------- DS18B20

def read_ds18b20(address: str, root: str = W1_ROOT) -> float:
    device = Path(root) / address
    if not device.exists():
        raise SensorError(f"Probe {address} not found. Is 1-Wire enabled and the probe connected?")
    temp_file = device / "temperature"
    try:
        if temp_file.exists():
            text = temp_file.read_text().strip()
            if not text:
                raise SensorError("The probe didn't answer. Check its wiring and pull-up resistor.")
            value = int(text) / 1000
        else:
            # Older kernels only have w1_slave: line 1 ends YES if the CRC is good.
            lines = (device / "w1_slave").read_text().strip().splitlines()
            if len(lines) < 2 or not lines[0].endswith("YES"):
                raise SensorError("Bad reading (checksum failed). Check the wiring.")
            value = int(lines[1].rsplit("t=", 1)[1]) / 1000
    except (OSError, ValueError, IndexError) as exc:
        raise SensorError(f"Couldn't read probe {address}: {exc}") from None
    # 85 °C is the power-on value a probe reports when it hasn't measured yet.
    if value == 85.0 or not -55 <= value <= 125:
        raise SensorError(f"Implausible reading ({value:g} °C). Check the probe's power and pull-up.")
    return value


def discover_ds18b20(root: str = W1_ROOT) -> list[str]:
    return sorted(Path(p).name for p in glob.glob(os.path.join(root, "28-*")))


# ---------------------------------------------------------------- DHT22 (kernel IIO)

def _iio_name(path: Path) -> str:
    try:
        return (path / "name").read_text().strip()
    except OSError:
        return ""


def discover_dht(root: str = IIO_ROOT) -> list[tuple[str, str]]:
    """IIO devices that look like DHT sensors: [(device dir name, driver name)]."""
    found = []
    for p in sorted(Path(root).glob("iio:device*")):
        name = _iio_name(p)
        if "dht" in name.lower():
            found.append((p.name, name))
    return found


def read_dht(address: str, quantity: str, root: str = IIO_ROOT, attempts: int = 3) -> float:
    device = Path(root) / address
    if not device.exists():
        raise SensorError(f"{address} not found. Is the dht11 overlay set in config.txt?")
    channel = "in_temp_input" if quantity == "temperature" else "in_humidityrelative_input"
    last = None
    for attempt in range(attempts):
        try:
            # millidegrees / milli-percent
            return int((device / channel).read_text().strip()) / 1000
        except (OSError, ValueError) as exc:
            # DHT sensors miss the odd read (EIO or timeout); the kernel needs ~2 s between tries.
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2.1)
    raise SensorError(f"No reading from the DHT sensor after {attempts} tries ({last}).")


# ---------------------------------------------------------------- BME280 / BMP280

def _s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def _s8(v: int) -> int:
    return v - 256 if v > 127 else v


@dataclass
class BMECalibration:
    T1: int; T2: int; T3: int
    P1: int; P2: int; P3: int; P4: int; P5: int; P6: int; P7: int; P8: int; P9: int
    H1: int = 0; H2: int = 0; H3: int = 0; H4: int = 0; H5: int = 0; H6: int = 0


def bme_t_fine(adc_t: int, c: BMECalibration) -> float:
    v1 = (adc_t / 16384.0 - c.T1 / 1024.0) * c.T2
    v2 = ((adc_t / 131072.0 - c.T1 / 8192.0) ** 2) * c.T3
    return v1 + v2


def bme_temperature(adc_t: int, c: BMECalibration) -> float:
    """°C, Bosch datasheet double-precision formula."""
    return bme_t_fine(adc_t, c) / 5120.0


def bme_pressure(adc_p: int, adc_t: int, c: BMECalibration) -> float:
    """hPa."""
    t_fine = bme_t_fine(adc_t, c)
    v1 = t_fine / 2.0 - 64000.0
    v2 = v1 * v1 * c.P6 / 32768.0
    v2 = v2 + v1 * c.P5 * 2.0
    v2 = v2 / 4.0 + c.P4 * 65536.0
    v1 = (c.P3 * v1 * v1 / 524288.0 + c.P2 * v1) / 524288.0
    v1 = (1.0 + v1 / 32768.0) * c.P1
    if v1 == 0:
        raise SensorError("Invalid pressure calibration.")
    p = 1048576.0 - adc_p
    p = (p - v2 / 4096.0) * 6250.0 / v1
    v1 = c.P9 * p * p / 2147483648.0
    v2 = p * c.P8 / 32768.0
    return (p + (v1 + v2 + c.P7) / 16.0) / 100.0


def bme_humidity(adc_h: int, adc_t: int, c: BMECalibration) -> float:
    """% relative humidity, clamped to 0-100."""
    h = bme_t_fine(adc_t, c) - 76800.0
    h = (adc_h - (c.H4 * 64.0 + c.H5 / 16384.0 * h)) * (
        c.H2 / 65536.0 * (1.0 + c.H6 / 67108864.0 * h * (1.0 + c.H3 / 67108864.0 * h)))
    h = h * (1.0 - c.H1 * h / 524288.0)
    return max(0.0, min(100.0, h))


class BME280Device:
    """One physical BME280/BMP280. Shared by its temperature, humidity and
    pressure points; a sample is reused for a second so they read together."""

    CHIP_IDS = {0x60: "BME280", 0x58: "BMP280"}

    def __init__(self, bus: int, address: int):
        self.bus_number, self.address = bus, address
        self._lock = threading.Lock()
        self._bus = None
        self._cal: BMECalibration | None = None
        self.chip = None
        self._sample: tuple[float, dict] | None = None

    def _open(self):
        if self._bus is None:
            try:
                from smbus2 import SMBus
            except ImportError:
                raise SensorError("The smbus2 library isn't installed "
                                  "(pip install -r requirements.txt on the Pi).") from None
            try:
                self._bus = SMBus(self.bus_number)
            except OSError as exc:
                raise SensorError(f"Can't open I²C bus {self.bus_number}: {exc}. Is I²C enabled?") from None
        if self._cal is None:
            try:
                chip_id = self._bus.read_byte_data(self.address, 0xD0)
            except OSError:
                raise SensorError(f"Nothing answered at I²C address 0x{self.address:02x}. "
                                  "Check the wiring, and try 0x76 or 0x77.") from None
            if chip_id not in self.CHIP_IDS:
                raise SensorError(f"The device at 0x{self.address:02x} isn't a BME280 or BMP280 "
                                  f"(chip id 0x{chip_id:02x}).")
            self.chip = self.CHIP_IDS[chip_id]
            self._cal = self._read_calibration()

    def _read_calibration(self) -> BMECalibration:
        b = self._bus.read_i2c_block_data(self.address, 0x88, 24)
        def u16(i): return b[i] | (b[i + 1] << 8)
        def s16(i): return _s16(u16(i))
        cal = BMECalibration(
            T1=u16(0), T2=s16(2), T3=s16(4),
            P1=u16(6), P2=s16(8), P3=s16(10), P4=s16(12), P5=s16(14),
            P6=s16(16), P7=s16(18), P8=s16(20), P9=s16(22))
        if self.chip == "BME280":
            cal.H1 = self._bus.read_byte_data(self.address, 0xA1)
            h = self._bus.read_i2c_block_data(self.address, 0xE1, 7)
            cal.H2 = _s16(h[0] | (h[1] << 8))
            cal.H3 = h[2]
            cal.H4 = (_s8(h[3]) << 4) | (h[4] & 0x0F)
            cal.H5 = (_s8(h[5]) << 4) | (h[4] >> 4)
            cal.H6 = _s8(h[6])
        return cal

    def sample(self) -> dict:
        with self._lock:
            if self._sample and time.monotonic() - self._sample[0] < 1.0:
                return self._sample[1]
            try:
                self._open()
                # One forced measurement, 1x oversampling for each quantity.
                # ctrl_hum must be written before ctrl_meas; the BMP280 doesn't have it.
                if self.chip == "BME280":
                    self._bus.write_byte_data(self.address, 0xF2, 0x01)
                self._bus.write_byte_data(self.address, 0xF4, (1 << 5) | (1 << 2) | 0x01)
                time.sleep(0.012)
                d = self._bus.read_i2c_block_data(self.address, 0xF7, 8)
            except OSError as exc:
                self._bus, self._cal = None, None        # reopen next time
                raise SensorError(f"I²C read failed: {exc}") from None
            adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
            adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
            adc_h = (d[6] << 8) | d[7]
            values = {
                "temperature": bme_temperature(adc_t, self._cal),
                "pressure": bme_pressure(adc_p, adc_t, self._cal),
            }
            if self.chip == "BME280":
                values["humidity"] = bme_humidity(adc_h, adc_t, self._cal)
            self._sample = (time.monotonic(), values)
            return values

    def read(self, quantity: str) -> float:
        values = self.sample()
        if quantity not in values:
            raise SensorError(f"This is a {self.chip}, which doesn't measure {quantity}.")
        return values[quantity]

    def close(self):
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None


_bme_devices: dict[tuple[int, int], BME280Device] = {}
_bme_lock = threading.Lock()


def bme_device(bus: int, address: int) -> BME280Device:
    with _bme_lock:
        key = (bus, address)
        if key not in _bme_devices:
            _bme_devices[key] = BME280Device(bus, address)
        return _bme_devices[key]


def parse_i2c_address(text: str) -> int | None:
    text = (text or "").strip().lower()
    try:
        value = int(text, 16) if text.startswith("0x") else int(text, 16)
    except ValueError:
        return None
    return value if 0x03 <= value <= 0x77 else None


# ---------------------------------------------------------------- reading a point

def read_point(sensor_type: str, address: str, quantity: str) -> float:
    if sensor_type == "ds18b20":
        return read_ds18b20(address)
    if sensor_type == "dht22":
        return read_dht(address, quantity)
    if sensor_type == "bme280":
        addr = parse_i2c_address(address)
        if addr is None:
            raise SensorError(f"Invalid I²C address {address!r}.")
        return bme_device(1, addr).read(quantity)
    raise SensorError(f"Unknown sensor type {sensor_type!r}.")


def format_value(value: float | None, quantity: str) -> str:
    if value is None:
        return "No reading yet"
    q = QUANTITIES[quantity]
    return f"{value:.{q['decimals']}f} {q['unit']}".replace(" %", "%")
