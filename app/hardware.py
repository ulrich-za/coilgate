# SPDX-License-Identifier: GPL-2.0-or-later
"""Inputs and outputs on the Pi's GPIO header, via gpiozero.

On a Pi this drives real pins through the lgpio library. Anywhere without GPIO
hardware (Windows, a normal PC) it uses gpiozero's mock pins, so everything can
be set up and tested before the Pi is involved.

The configuration lives in the database (io_points). IOManager.sync() applies
it: points whose wiring is unchanged keep their device and state, so renaming
an output that's switched on doesn't switch it off.
"""
from __future__ import annotations

import atexit
import logging
import sys
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

log = logging.getLogger("coilgate")

# BCM GPIO numbers usable on the 40-pin header. GPIO 0 and 1 are reserved for
# HAT identification EEPROMs.
VALID_PINS = range(2, 28)

PHYSICAL_PIN = {
    2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23, 12: 32, 13: 33,
    14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38, 21: 40, 22: 15, 23: 16,
    24: 18, 25: 22, 26: 37, 27: 13,
}

PIN_NOTES = {
    2: "I²C, fixed pull-up to 3.3 V",
    3: "I²C, fixed pull-up to 3.3 V",
    7: "SPI if enabled", 8: "SPI if enabled", 9: "SPI if enabled",
    10: "SPI if enabled", 11: "SPI if enabled",
    14: "serial console if enabled", 15: "serial console if enabled",
}


def default_pull(pin: int) -> str:
    """The pull a pin has at power-on, before any software runs."""
    return "up" if pin <= 8 else "down"


MAX_PULSE_SECONDS = 24 * 3600
MIN_PULSE_SECONDS = 0.1

# How an input is wired, and what that means to gpiozero.
WIRING = {
    "to_ground": {
        "label": "Contact to ground",
        "help": "A switch, button or dry contact between the pin and GND. Uses the Pi's internal pull-up.",
        "kwargs": {"pull_up": True, "active_state": None},
        "labels": ("Closed", "Open"),
    },
    "to_3v3": {
        "label": "Contact to 3.3 V",
        "help": "A switch between the pin and 3.3 V. Uses the Pi's internal pull-down.",
        "kwargs": {"pull_up": False, "active_state": None},
        "labels": ("Closed", "Open"),
    },
    "active_high": {
        "label": "Signal, active when high",
        "help": "A device that drives the pin itself (sensor, opto-isolated input board). No internal pull.",
        "kwargs": {"pull_up": None, "active_state": True},
        "labels": ("On", "Off"),
    },
    "active_low": {
        "label": "Signal, active when low",
        "help": "As above, for devices that pull the pin low when active.",
        "kwargs": {"pull_up": None, "active_state": False},
        "labels": ("On", "Off"),
    },
}


class HardwareConfigError(ValueError):
    pass


@dataclass(frozen=True)
class IOPoint:
    id: int
    kind: str                     # "output" or "input"
    name: str
    pin: int
    active_high: bool = True      # outputs: relay board triggers on HIGH
    mode: str = "toggle"          # outputs: "toggle" or "pulse"
    pulse_seconds: float = 1.0
    wiring: str = "to_ground"     # inputs
    bounce_ms: int = 50           # inputs
    on_label: str = ""
    off_label: str = ""
    sort_order: int = 0
    sensor_type: str | None = None   # sensors: ds18b20, bme280, dht22
    quantity: str | None = None      # sensors: temperature, humidity, pressure
    address: str | None = None       # sensors: probe id, I2C address or IIO device
    poll_seconds: int = 30
    calibration: float = 0.0         # sensors: added to every reading

    @classmethod
    def from_row(cls, row) -> "IOPoint":
        keys = row.keys()
        return cls(
            id=row["id"], kind=row["kind"], name=row["name"], pin=row["pin"],
            active_high=bool(row["active_high"]), mode=row["mode"],
            pulse_seconds=float(row["pulse_seconds"]), wiring=row["wiring"],
            bounce_ms=int(row["bounce_ms"]), on_label=row["on_label"],
            off_label=row["off_label"], sort_order=row["sort_order"],
            sensor_type=row["sensor_type"] if "sensor_type" in keys else None,
            quantity=row["quantity"] if "quantity" in keys else None,
            address=row["address"] if "address" in keys else None,
            poll_seconds=int(row["poll_seconds"]) if "poll_seconds" in keys else 30,
            calibration=float(row["calibration"]) if "calibration" in keys else 0.0,
        )

    @property
    def hardware_key(self) -> tuple:
        """Settings that need the device recreated when they change."""
        if self.kind == "output":
            return ("output", self.pin, self.active_high, self.mode)
        if self.kind == "sensor":
            return ("sensor", self.sensor_type, self.address, self.quantity, self.pin)
        return ("input", self.pin, self.wiring, self.bounce_ms)

    @property
    def unit(self) -> str:
        from .sensors import QUANTITIES
        return QUANTITIES[self.quantity]["unit"] if self.quantity else ""

    @property
    def active_text(self) -> str:
        if self.on_label:
            return self.on_label
        return WIRING[self.wiring]["labels"][0] if self.kind == "input" else "On"

    @property
    def inactive_text(self) -> str:
        if self.off_label:
            return self.off_label
        if self.kind == "input":
            return WIRING[self.wiring]["labels"][1]
        return "Ready" if self.mode == "pulse" else "Off"

    @property
    def physical_pin(self) -> int | None:
        return PHYSICAL_PIN.get(self.pin) if self.pin is not None else None


@dataclass
class Reading:
    value: float | None = None       # calibrated value
    updated: float | None = None     # wall-clock time of the last good reading
    fresh_until: float = 0.0         # monotonic time after which the value counts as stale
    error: str | None = None


def _has_gpio_hardware() -> bool:
    return sys.platform.startswith("linux") and any(Path("/dev").glob("gpiochip*"))


def _friendly(exc: Exception, pin: int) -> str:
    text = str(exc)
    if "busy" in text.lower():
        return f"GPIO {pin} is in use by another program."
    if isinstance(exc, PermissionError) or "permission" in text.lower():
        return "No permission to use GPIO. Is the service account in the gpio group?"
    return text or exc.__class__.__name__


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def describe_pulse(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g} s"
    if seconds % 3600 == 0:
        h = int(seconds // 3600)
        return f"{h} hour{'s' if h != 1 else ''}"
    if seconds % 60 == 0:
        m = int(seconds // 60)
        return f"{m} minute{'s' if m != 1 else ''}"
    return format_duration(seconds)


class IOManager:
    """Owns every GPIO device. Shared by all request threads."""

    def __init__(self, gpio_mode: str = "auto", on_input_change=None):
        if gpio_mode not in ("auto", "hardware", "simulate"):
            raise HardwareConfigError("gpio_mode must be 'auto', 'hardware' or 'simulate'.")
        self.simulated = gpio_mode == "simulate" or (gpio_mode == "auto" and not _has_gpio_hardware())
        self._on_input_change = on_input_change
        self._lock = threading.RLock()
        self._factory = None
        self._points: dict[int, IOPoint] = {}
        self._devices: dict[int, object] = {}
        self._keys: dict[int, tuple] = {}
        self._pulse_until: dict[int, float] = {}
        self.errors: dict[int, str] = {}
        self.readings: dict[int, Reading] = {}
        self._next_poll: dict[int, float] = {}
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()
        # Every visible change (output switched, input changed, new reading)
        # bumps the version, waking pages that are waiting for news.
        self.version = 0
        self._changed = threading.Condition()
        # Counts changes to the setup itself (points added, removed, renamed,
        # moved or reordered). Open pages reload when it changes, since they
        # were drawn for the old setup.
        self.setup = 0
        self._layout: tuple | None = None
        # Called (without arguments) right after an input changes: the rule
        # engine uses this to act at once instead of on its next check.
        self.listeners: list = []
        atexit.register(self.close)

    # ------------------------------------------------------------ change notification

    def bump(self) -> None:
        with self._changed:
            self.version += 1
            self._changed.notify_all()

    def wait_for_change(self, version: int, timeout: float) -> int:
        """Block until the version differs from `version`, or the timeout passes."""
        with self._changed:
            self._changed.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version

    # ------------------------------------------------------------ setup

    def _factory_or_raise(self):
        if self._factory is None:
            if self.simulated:
                from gpiozero.pins.mock import MockFactory
                self._factory = MockFactory()
            else:
                try:
                    from gpiozero.pins.lgpio import LGPIOFactory
                except ImportError as exc:
                    raise HardwareConfigError(
                        "The lgpio library isn't installed. On the Pi run "
                        "'sudo apt install python3-lgpio' and create the virtual environment "
                        "with --system-site-packages (see README).") from exc
                self._factory = LGPIOFactory()
        return self._factory

    def _make_device(self, p: IOPoint):
        from gpiozero import DigitalInputDevice, DigitalOutputDevice
        factory = self._factory_or_raise()
        if p.kind == "output":
            return DigitalOutputDevice(p.pin, active_high=p.active_high, initial_value=False,
                                       pin_factory=factory)
        kwargs = dict(WIRING[p.wiring]["kwargs"])
        device = DigitalInputDevice(
            p.pin, bounce_time=(p.bounce_ms / 1000) if p.bounce_ms else None,
            pin_factory=factory, **kwargs)
        if self.simulated and p.wiring == "active_low":
            # A simulated pin with no pull floats low, which an active-low input
            # reads as active. Start it at rest, before the change handlers exist.
            device.pin.drive_high()
        device.when_activated = partial(self._input_event, p.id, True)
        device.when_deactivated = partial(self._input_event, p.id, False)
        return device

    def _close_device(self, pid: int) -> None:
        device = self._devices.pop(pid, None)
        self._keys.pop(pid, None)
        self._pulse_until.pop(pid, None)
        if device is not None:
            try:
                if hasattr(device, "off"):
                    device.off()
                device.close()
            except Exception:
                pass

    def sync(self, points: list[IOPoint]) -> None:
        """Apply the configuration. Unchanged wiring keeps its device and state."""
        with self._lock:
            wanted = {p.id: p for p in points}
            # Close first, so pins can move between points in one change.
            for pid in list(self._devices):
                if pid not in wanted or wanted[pid].hardware_key != self._keys.get(pid):
                    self._close_device(pid)
            self.errors = {pid: e for pid, e in self.errors.items() if pid in wanted}
            # Sensors have no gpiozero device; they're polled.
            for pid in list(self.readings):
                if pid not in wanted or (self._keys.get(pid) != wanted[pid].hardware_key
                                         and wanted[pid].kind == "sensor"):
                    self.readings.pop(pid, None)
                    self._next_poll.pop(pid, None)
                    if pid not in self._devices:
                        self._keys.pop(pid, None)
            for pid, p in wanted.items():
                if p.kind == "sensor":
                    if pid not in self.readings:
                        self.readings[pid] = self._initial_reading(p)
                        self._next_poll[pid] = 0.0
                        self._keys[pid] = p.hardware_key
                    continue
                if pid in self._devices:
                    continue
                try:
                    self._devices[pid] = self._make_device(p)
                    self._keys[pid] = p.hardware_key
                    self.errors.pop(pid, None)
                except Exception as exc:
                    self.errors[pid] = _friendly(exc, p.pin)
                    log.warning("GPIO %s (%s): %s", p.pin, p.name, self.errors[pid])
            self._points = wanted
            layout = tuple(points)
            if layout != self._layout:
                self._layout = layout
                self.setup += 1
            if not self.simulated and any(p.kind == "sensor" for p in wanted.values()):
                self._start_poller()
        self.bump()

    # ------------------------------------------------------------ sensors

    def _initial_reading(self, p: IOPoint) -> Reading:
        if self.simulated:
            from .sensors import QUANTITIES
            return Reading(value=QUANTITIES[p.quantity]["sim"], updated=time.time(),
                           fresh_until=float("inf"))
        return Reading()

    def _start_poller(self) -> None:
        if self._poller is None or not self._poller.is_alive():
            self._stop.clear()
            self._poller = threading.Thread(target=self._poll_loop, name="coilgate-sensors",
                                            daemon=True)
            self._poller.start()

    def _poll_loop(self) -> None:
        while not self._stop.wait(0.5):
            self.poll_due()

    def poll_due(self) -> None:
        """Read every sensor whose interval has passed (runs on the poller thread)."""
        from .sensors import SENSOR_TYPES, SensorError, read_point
        now = time.monotonic()
        for p in [p for p in self._points.values() if p.kind == "sensor"]:
            if now < self._next_poll.get(p.id, 0.0):
                continue
            interval = max(p.poll_seconds, SENSOR_TYPES[p.sensor_type]["min_poll"])
            self._next_poll[p.id] = now + interval
            reading = self.readings.setdefault(p.id, Reading())
            before = (reading.value, reading.error)
            try:
                raw = read_point(p.sensor_type, p.address or "", p.quantity)
            except SensorError as exc:
                reading.error = str(exc)
            except Exception as exc:          # never let one sensor stop the poller
                reading.error = f"{exc.__class__.__name__}: {exc}"
            else:
                reading.value = round(raw + p.calibration, 2)
                reading.updated = time.time()
                # A value is trusted for three polling intervals (at least 90 s).
                reading.fresh_until = time.monotonic() + max(3 * interval, 90)
                reading.error = None
            if (reading.value, reading.error) != before:
                self.bump()

    def sensor_value(self, pid: int) -> float | None:
        """The current reading, or None if there isn't a recent one."""
        r = self.readings.get(pid)
        if r is None or r.value is None or time.monotonic() > r.fresh_until:
            return None
        return r.value

    def simulate_reading(self, pid: int, value: float) -> None:
        if not self.simulated:
            raise HardwareConfigError("Sensor readings can only be simulated without GPIO hardware.")
        p = self._points.get(pid)
        if p is None or p.kind != "sensor":
            raise KeyError(pid)
        self.readings[pid] = Reading(value=round(value, 2), updated=time.time(),
                                     fresh_until=float("inf"))
        self.bump()

    def _input_event(self, pid: int, active: bool, *_ignored) -> None:
        point = self._points.get(pid)
        if point is None:
            return
        if self._on_input_change is not None:
            try:
                self._on_input_change(point, active)
            except Exception:
                log.exception("Recording an input change failed")
        self.bump()
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                log.exception("An input-change listener failed")

    # ------------------------------------------------------------ queries

    def points(self, kind: str | None = None) -> list[IOPoint]:
        items = sorted(self._points.values(), key=lambda p: (p.sort_order, p.id))
        return [p for p in items if kind is None or p.kind == kind]

    def get(self, pid: int) -> IOPoint | None:
        return self._points.get(pid)

    def available(self, pid: int) -> bool:
        return pid in self._devices

    def is_on(self, pid: int) -> bool:
        device = self._devices.get(pid)
        return bool(device is not None and device.is_active)

    def pulse_remaining(self, pid: int) -> float:
        return max(0.0, self._pulse_until.get(pid, 0) - time.monotonic())

    def is_pulsing(self, pid: int) -> bool:
        return self.pulse_remaining(pid) > 0 and self.is_on(pid)

    def state_text(self, p: IOPoint) -> str:
        if pid_error := self.errors.get(p.id):
            return f"Unavailable: {pid_error}"
        if p.kind == "sensor":
            from .sensors import format_value
            r = self.readings.get(p.id) or Reading()
            value = self.sensor_value(p.id)
            if value is None:
                return f"Unavailable: {r.error}" if r.error else "Waiting for the first reading"
            return format_value(value, p.quantity)
        if p.kind == "output" and p.mode == "pulse":
            if self.is_pulsing(p.id):
                return f"{p.on_label or 'Active'}, {format_duration(self.pulse_remaining(p.id))} left"
            return p.inactive_text
        return p.active_text if self.is_on(p.id) else p.inactive_text

    def snapshot(self) -> list[dict]:
        return [{
            "id": p.id, "kind": p.kind, "mode": p.mode,
            "on": self.is_on(p.id), "pulsing": self.is_pulsing(p.id),
            "available": self.available(p.id) if p.kind != "sensor" else self.sensor_value(p.id) is not None,
            "value": self.sensor_value(p.id) if p.kind == "sensor" else None,
            "state_text": self.state_text(p),
        } for p in self.points()]

    # ------------------------------------------------------------ actions

    def _output(self, pid: int, mode: str):
        p = self._points.get(pid)
        if p is None or p.kind != "output":
            raise KeyError(pid)
        if p.mode != mode:
            raise ValueError(f"{p.name} is not a {mode} output.")
        device = self._devices.get(pid)
        if device is None:
            raise HardwareConfigError(self.errors.get(pid, "Not available."))
        return p, device

    def set(self, pid: int, on: bool) -> None:
        with self._lock:
            _, device = self._output(pid, "toggle")
            device.on() if on else device.off()
        self.bump()

    def pulse(self, pid: int) -> bool:
        """Switch on for pulse_seconds, then off. Returns False if already pulsing."""
        with self._lock:
            p, device = self._output(pid, "pulse")
            if self.is_pulsing(pid):
                return False
            self._pulse_until[pid] = time.monotonic() + p.pulse_seconds
            device.blink(on_time=p.pulse_seconds, off_time=0.01, n=1, background=True)
        self.bump()
        # Tell waiting pages when the pulse ends, too.
        ender = threading.Timer(p.pulse_seconds + 0.05, self.bump)
        ender.daemon = True
        ender.start()
        return True

    def stop(self, pid: int) -> bool:
        """End a pulse early. Returns False if it wasn't pulsing."""
        with self._lock:
            _, device = self._output(pid, "pulse")
            was = self.is_pulsing(pid)
            device.off()
            self._pulse_until.pop(pid, None)
        self.bump()
        return was

    def simulate_input(self, pid: int, active: bool) -> None:
        """Change a simulated input (only without real GPIO hardware)."""
        if not self.simulated:
            raise HardwareConfigError("Inputs can only be simulated without GPIO hardware.")
        with self._lock:
            p = self._points.get(pid)
            device = self._devices.get(pid)
            if p is None or p.kind != "input" or device is None:
                raise KeyError(pid)
            active_high = p.wiring in ("to_3v3", "active_high")
            device.pin.drive_high() if active == active_high else device.pin.drive_low()

    def close(self) -> None:
        """Switch every output off and release all pins."""
        self._stop.set()
        self.bump()             # release any page waiting for news
        with self._lock:
            for pid in list(self._devices):
                self._close_device(pid)
            if self._factory is not None:
                try:
                    self._factory.close()
                except Exception:
                    pass
                self._factory = None
