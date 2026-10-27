# SPDX-License-Identifier: GPL-2.0-or-later
"""Schedules: time windows, the built-in 24/7 schedule, and the admin pages."""
from __future__ import annotations

from datetime import datetime

import pytest

from app import schedules as sched
from conftest import add_user, db, login, post

MON, TUE, WED, SAT, SUN = (1 << 0), (1 << 1), (1 << 2), (1 << 5), (1 << 6)
WEEKDAYS = 0b0011111


def at(day: int, hh: int, mm: int = 0) -> datetime:
    # 2026-10-19 is a Monday
    return datetime(2026, 10, 19 + day, hh, mm)


def schedule(*windows) -> sched.Schedule:
    return sched.Schedule(1, "Test", False, [sched.Window(*w) for w in windows])


@pytest.mark.parametrize("when, expected", [
    (at(0, 8, 0), True), (at(0, 16, 59), True), (at(0, 17, 0), False), (at(0, 7, 59), False),
    (at(4, 12), True), (at(5, 12), False),           # Friday yes, Saturday no
])
def test_business_hours(when, expected):
    assert schedule((WEEKDAYS, 8 * 60, 17 * 60)).is_active(when) is expected


@pytest.mark.parametrize("when, expected", [
    (at(0, 22, 0), True), (at(0, 23, 59), True),      # Monday night
    (at(1, 3, 0), True), (at(1, 5, 59), True),        # carries into Tuesday morning
    (at(1, 6, 0), False), (at(0, 3, 0), False),       # but not Monday morning (Sunday not ticked)
    (at(1, 22, 0), False),                            # Tuesday night isn't ticked
])
def test_overnight_window_belongs_to_the_day_it_starts(when, expected):
    assert schedule((MON, 22 * 60, 6 * 60)).is_active(when) is expected


def test_until_midnight_and_all_day():
    evening = schedule((SAT | SUN, 18 * 60, 1440))
    assert evening.is_active(at(5, 23, 59)) and not evening.is_active(at(6, 0, 0))
    all_day = schedule((WED, 0, 1440))
    assert all_day.is_active(at(2, 0, 0)) and all_day.is_active(at(2, 23, 59))
    assert not all_day.is_active(at(3, 0, 0))


def test_builtin_is_always_active():
    s = sched.Schedule(1, sched.ALWAYS_NAME, True, [])
    assert s.is_active(at(3, 3)) and s.next_start(at(3, 3)) is None
    assert s.summary() == "Every day, all day"


def test_next_start():
    s = schedule((WEEKDAYS, 8 * 60, 17 * 60))
    assert s.next_start(at(0, 7, 0)) == at(0, 8, 0)
    assert s.next_start(at(0, 18, 0)) == at(1, 8, 0)
    assert s.next_start(at(4, 18, 0)) == at(7, 8, 0)          # Friday evening -> Monday
    assert s.next_start(at(0, 9, 0)) is None                  # active now
    assert sched.describe_when(at(1, 8, 0), now=at(0, 18, 0)) == "tomorrow at 08:00"


def test_summary_and_chart():
    s = schedule((WEEKDAYS, 8 * 60, 17 * 60), (SUN, 22 * 60, 2 * 60))
    assert s.summary() == "Monday to Friday, 08:00 to 17:00; Sun, 22:00 to 02:00 (next day)"
    rows = s.chart_rows()
    assert rows[0] == [(480, 1020), (0, 120)]                 # Monday gets the spill-over from Sunday
    assert rows[6] == [(1320, 1440)]


def test_time_parsing():
    assert sched.parse_time("08:30") == 510
    assert sched.parse_time("24:00") is None and sched.parse_time("nope") is None


# ---------------------------------------------------------------- admin pages

def admin(app):
    add_user(app)
    client = app.test_client()
    login(client)
    return client


def schedule_form(name="Business hours", rows=None):
    rows = rows or [({0, 1, 2, 3, 4}, "08:00", "17:00")]
    data = {"name": name, "rows": str(len(rows) + 1)}
    for i, (days, start, end) in enumerate(rows):
        data[f"w{i}_days"] = [str(d) for d in sorted(days)]
        data[f"w{i}_start"], data[f"w{i}_end"] = start, end
    return data


def schedules_in(app):
    with db(app) as conn:
        return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM schedules")}


def test_builtin_schedule_exists_and_is_protected(app):
    client = admin(app)
    ids = schedules_in(app)
    assert sched.ALWAYS_NAME in ids
    assert client.get(f"/admin/schedules/{ids[sched.ALWAYS_NAME]}").status_code == 404
    page = client.get("/admin/schedules").get_data(as_text=True)
    assert "Always (24/7)" in page and "Built in" in page


def test_create_and_edit_schedule(app):
    client = admin(app)
    resp = post(client, "/admin/schedules/new", schedule_form(), page="/admin/schedules/new")
    assert resp.status_code == 302
    sid = schedules_in(app)["Business hours"]
    page = client.get("/admin/schedules").get_data(as_text=True)
    assert "Monday to Friday, 08:00 to 17:00" in page and "week-chart" in page
    form = schedule_form("Office", [({0, 1, 2, 3, 4}, "07:30", "16:00"), ({5}, "09:00", "12:00")])
    assert post(client, f"/admin/schedules/{sid}", form).status_code == 302
    with db(app) as conn:
        windows = conn.execute("SELECT days, start_min, end_min FROM schedule_windows"
                               " WHERE schedule_id = ? ORDER BY id", (sid,)).fetchall()
    assert [tuple(w) for w in windows] == [(31, 450, 960), (32, 540, 720)]


def test_midnight_end_is_stored_as_end_of_day(app):
    client = admin(app)
    post(client, "/admin/schedules/new", schedule_form("Evenings", [({0, 1, 2, 3, 4, 5, 6}, "18:00", "00:00")]),
         page="/admin/schedules/new")
    with db(app) as conn:
        assert tuple(conn.execute("SELECT start_min, end_min FROM schedule_windows").fetchone()) == (1080, 1440)


@pytest.mark.parametrize("form, message", [
    (schedule_form(""), "Give the schedule a name"),
    (schedule_form("Always (24/7)"), "belongs to the built-in"),
    (schedule_form("X", [(set(), "08:00", "17:00")]), "choose at least one day"),
    (schedule_form("X", [({1}, "", "17:00")]), "enter a start and end time"),
])
def test_schedule_validation(app, form, message):
    client = admin(app)
    resp = post(client, "/admin/schedules/new", form, page="/admin/schedules/new")
    assert resp.status_code == 400 and message in resp.get_data(as_text=True)


def test_duplicate_schedule_names_refused(app):
    client = admin(app)
    post(client, "/admin/schedules/new", schedule_form(), page="/admin/schedules/new")
    resp = post(client, "/admin/schedules/new", schedule_form("business HOURS"), page="/admin/schedules/new")
    assert resp.status_code == 400 and "already a schedule" in resp.get_data(as_text=True)


def test_schedule_in_use_cannot_be_deleted(app):
    client = admin(app)
    post(client, "/admin/schedules/new", schedule_form(), page="/admin/schedules/new")
    sid = schedules_in(app)["Business hours"]
    add_user(app, username="anna", role="user", schedule_id=sid)
    post(client, f"/admin/schedules/{sid}/delete", {"confirm": "yes"}, page=f"/admin/schedules/{sid}")
    assert "Business hours" in schedules_in(app)
    with db(app) as conn:
        conn.execute("UPDATE users SET schedule_id = NULL")
    post(client, f"/admin/schedules/{sid}/delete", {"confirm": "yes"}, page=f"/admin/schedules/{sid}")
    assert "Business hours" not in schedules_in(app)


def test_all_day_window_reads_naturally():
    assert sched.Window(0b1100000, 0, 1440).describe() == "Weekends, all day"
