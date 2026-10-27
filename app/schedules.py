# SPDX-License-Identifier: GPL-2.0-or-later
"""Schedules: named sets of weekly time windows, e.g. "Business hours" =
Monday-Friday 08:00-17:00. Rules can run only during a schedule, a schedule can
itself trigger a rule, and a User account can be limited to one.

Times are the Pi's local time, so set its time zone (raspi-config or
timedatectl set-timezone).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ALWAYS_NAME = "Always (24/7)"
ALL_DAYS = 0b1111111


@dataclass(frozen=True)
class Window:
    days: int           # bitmask: bit 0 = Monday ... bit 6 = Sunday
    start: int          # minutes after midnight, 0-1439
    end: int            # minutes after midnight, 1-1440; end <= start means "into the next day"

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    def covers(self, now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        today = 1 << now.weekday()
        yesterday = 1 << ((now.weekday() - 1) % 7)
        if not self.overnight:
            return bool(self.days & today) and self.start <= minute < self.end
        # 22:00-06:00: the evening part belongs to the selected day, the
        # early-morning part to the day after it.
        return (bool(self.days & today) and minute >= self.start) or \
               (bool(self.days & yesterday) and minute < self.end)

    def describe(self) -> str:
        if self.start == 0 and self.end == 1440:
            return f"{describe_days(self.days)}, all day"
        return f"{describe_days(self.days)}, {fmt_time(self.start)} to {fmt_time(self.end)}" + \
            (" (next day)" if self.overnight and self.end != self.start else "")


@dataclass
class Schedule:
    id: int
    name: str
    builtin: bool = False
    windows: list[Window] = field(default_factory=list)

    def is_active(self, now: datetime | None = None) -> bool:
        if self.builtin:
            return True
        now = now or datetime.now()
        return any(w.covers(now) for w in self.windows)

    def next_start(self, now: datetime | None = None) -> datetime | None:
        """When this schedule next becomes active (None if never, or if it's active now)."""
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        if self.builtin or self.is_active(now):
            return None
        candidates = []
        for offset in range(0, 8):
            day = (now + timedelta(days=offset)).replace(hour=0, minute=0)
            for w in self.windows:
                if w.days & (1 << day.weekday()):
                    start = day + timedelta(minutes=w.start)
                    if start > now:
                        candidates.append(start)
        return min(candidates) if candidates else None

    def summary(self) -> str:
        if self.builtin:
            return "Every day, all day"
        if not self.windows:
            return "No times set, so never active"
        return "; ".join(w.describe() for w in self.windows)

    def chart_rows(self) -> list[list[tuple[int, int]]]:
        """Per weekday (Mon..Sun), the active (start, end) minute spans, for the weekly chart."""
        rows: list[list[tuple[int, int]]] = [[] for _ in range(7)]
        if self.builtin:
            return [[(0, 1440)] for _ in range(7)]
        for w in self.windows:
            for d in range(7):
                if not w.days & (1 << d):
                    continue
                if not w.overnight:
                    rows[d].append((w.start, w.end))
                else:
                    rows[d].append((w.start, 1440))
                    if w.end > 0:
                        rows[(d + 1) % 7].append((0, w.end))
        return rows


def fmt_time(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_time(text: str) -> int | None:
    """'08:30' -> 510. Returns None if not a valid HH:MM."""
    try:
        hh, mm = text.strip().split(":")[:2]
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def describe_days(mask: int) -> str:
    if mask == ALL_DAYS:
        return "Every day"
    if mask == 0b0011111:
        return "Monday to Friday"
    if mask == 0b1100000:
        return "Weekends"
    days = [DAY_NAMES[i] for i in range(7) if mask & (1 << i)]
    return ", ".join(days) if days else "No days"


def describe_when(dt: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if dt.date() == now.date():
        return f"today at {fmt_time(dt.hour * 60 + dt.minute)}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {fmt_time(dt.hour * 60 + dt.minute)}"
    return f"{DAY_LONG[dt.weekday()]} at {fmt_time(dt.hour * 60 + dt.minute)}"


# ---------------------------------------------------------------- database

def ensure_always(conn: sqlite3.Connection) -> int:
    """The built-in 24/7 schedule exists exactly once. Returns its id."""
    row = conn.execute("SELECT id FROM schedules WHERE builtin = 1").fetchone()
    if row:
        return row[0]
    now = int(time.time())
    cur = conn.execute("INSERT INTO schedules (name, builtin, created_at, updated_at)"
                       " VALUES (?, 1, ?, ?)", (ALWAYS_NAME, now, now))
    conn.commit()
    return cur.lastrowid


def load_all(conn: sqlite3.Connection) -> dict[int, Schedule]:
    schedules = {r["id"]: Schedule(r["id"], r["name"], bool(r["builtin"]))
                 for r in conn.execute("SELECT * FROM schedules ORDER BY builtin DESC, name")}
    for r in conn.execute("SELECT * FROM schedule_windows ORDER BY id"):
        if r["schedule_id"] in schedules:
            schedules[r["schedule_id"]].windows.append(Window(r["days"], r["start_min"], r["end_min"]))
    return schedules


def is_active(conn: sqlite3.Connection, schedule_id: int | None, now: datetime | None = None) -> bool:
    if schedule_id is None:
        return True
    s = load_all(conn).get(schedule_id)
    return True if s is None else s.is_active(now)
