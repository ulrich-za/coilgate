# SPDX-License-Identifier: GPL-2.0-or-later
"""The Raspberry Pi 5 40-pin header: data for the Pinout page and the live pin
maps on the Controls page.

The pin data and interface descriptions come from pinout_data/pi5.json, which
is adapted from Pinout.xyz and licensed CC BY-SA 4.0 (see
pinout_data/NOTICE.md). This module only reads it.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup, escape

from .hardware import PHYSICAL_PIN, WIRING, describe_pulse

DATA_FILE = Path(__file__).resolve().parent / "pinout_data" / "pi5.json"
POWER_NAMES = {"3v3": "3.3 V power", "5v": "5 V power", "ground": "Ground"}
ONE_WIRE_PIN = 4            # dtoverlay=w1-gpio's default GPIO
I2C_PINS = (2, 3)           # I2C1, where CoilGate reads BME280 sensors


@lru_cache(maxsize=1)
def data() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- text

_INLINE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    """Escape, then allow `code`, **bold** and https links. Nothing else."""
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(str(escape(text[pos:m.start()])))
        code, bold, label, url = m.groups()
        if code is not None:
            out.append(f"<code>{escape(code)}</code>")
        elif bold is not None:
            out.append(f"<strong>{escape(bold)}</strong>")
        elif url.startswith("https://"):
            out.append(f'<a href="{escape(url)}" rel="noopener noreferrer" target="_blank">'
                       f"{escape(label)}</a>")
        else:
            out.append(str(escape(label)))
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return "".join(out)


def render_text(source: str) -> Markup:
    """The small Markdown subset the interface descriptions use: paragraphs,
    ## headings, bullet lists, tables and fenced code. Everything is escaped."""
    html: list[str] = []
    lines = source.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            html.append(f"<pre><code>{escape(chr(10).join(code))}</code></pre>")
        elif line.startswith("## "):
            html.append(f"<h3>{_inline(line[3:])}</h3>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            html.append('<div class="table-wrap"><table class="ref"><thead><tr>'
                        + "".join(f'<th scope="col">{_inline(c)}</th>' for c in head)
                        + "</tr></thead><tbody>"
                        + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                                  for r in body)
                        + "</tbody></table></div>")
        elif line.startswith(("* ", "- ")):
            items = []
            while i < len(lines) and lines[i].startswith(("* ", "- ")):
                items.append(f"<li>{_inline(lines[i][2:].strip())}</li>")
                i += 1
            html.append("<ul>" + "".join(items) + "</ul>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(
                    ("```", "## ", "|", "* ", "- ")):
                para.append(lines[i].strip())
                i += 1
            html.append(f"<p>{_inline(' '.join(para))}</p>")
    return Markup("".join(html))


# ---------------------------------------------------------------- CoilGate's use of the pins

def active_level(p) -> str | None:
    """The pin level that means "active" for an output or input."""
    if p.kind == "output":
        return "high" if p.active_high else "low"
    if p.kind == "input":
        return "low" if p.wiring in ("to_ground", "active_low") else "high"
    return None


def describe_use(p) -> str:
    if p.kind == "output":
        how = "on" if p.mode == "toggle" else f"pulses ({describe_pulse(p.pulse_seconds)})"
        return f"Output, {how} when {'high' if p.active_high else 'low'}"
    if p.kind == "input":
        return f"Input, {WIRING.get(p.wiring, {}).get('label', p.wiring).lower()}"
    if p.sensor_type == "ds18b20":
        return "DS18B20 probe, 1-Wire bus (default pin)"
    if p.sensor_type == "bme280":
        return f"BME280, I²C bus ({p.address})"
    return "DHT22 sensor"


def usage(points) -> dict[int, list[dict]]:
    """Physical pin -> what CoilGate has on it."""
    used: dict[int, list[dict]] = {}

    def add(gpio: int, p, role: str = ""):
        physical = PHYSICAL_PIN.get(gpio)
        if physical is None:
            return
        used.setdefault(physical, []).append({
            "id": p.id, "name": p.name, "kind": p.kind, "role": role,
            "level": active_level(p), "detail": describe_use(p),
        })

    for p in points:
        if p.kind in ("output", "input") and p.pin is not None:
            add(p.pin, p)
        elif p.kind == "sensor":
            if p.sensor_type == "ds18b20":
                add(ONE_WIRE_PIN, p, "1-Wire data")
            elif p.sensor_type == "bme280":
                add(I2C_PINS[0], p, "I²C SDA")
                add(I2C_PINS[1], p, "I²C SCL")
            elif p.pin is not None:
                add(p.pin, p, "Data")
    return used


# ---------------------------------------------------------------- the header

def pins(points) -> list[dict]:
    """All 40 pins in physical order, with Pi 5 details and CoilGate's use."""
    d = data()
    used = usage(points)
    result = []
    for entry in d["header"]:
        pin = dict(entry)
        if pin["type"] == "gpio":
            bcm = str(pin["bcm"])
            pin["name"] = f"GPIO {pin['bcm']}"
            pin["label"] = d["default_labels"].get(bcm, "")
            pin["pull"] = d["pulls"][bcm]
            pin["functions"] = [(f"F{n}", name) for n, name in enumerate(d["functions"][bcm]) if name]
        else:
            pin["name"] = POWER_NAMES[pin["type"]]
            pin["label"] = ""
        pin["uses"] = used.get(pin["physical"], [])
        # Outputs and inputs light up; one per pin, since pins can't be shared.
        live = [u for u in pin["uses"] if u["kind"] in ("output", "input")]
        pin["live"] = live[0] if live else None
        pin["interfaces"] = [i["id"] for i in d["interfaces"] if str(pin["physical"]) in i["pins"]]
        result.append(pin)
    return result


def rows(points) -> list[tuple[dict, dict]]:
    """The header as 20 rows of (odd pin, even pin), pin 1 at the top left."""
    all_pins = pins(points)
    return [(all_pins[n], all_pins[n + 1]) for n in range(0, 40, 2)]


def interfaces() -> list[dict]:
    return [{**i, "name": POWER_NAMES.get(i["id"], i["name"]),
             "title": POWER_NAMES.get(i["id"], i["title"]),
             "html": render_text(i["text"]),
             "map": {k: [v["label"], v["main"]] for k, v in i["pins"].items()}}
            for i in data()["interfaces"]]
