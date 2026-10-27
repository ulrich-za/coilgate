# SPDX-License-Identifier: GPL-2.0-or-later
"""Activity log downloads as CSV and PDF."""
from __future__ import annotations

import csv
import io as iolib
import time
from datetime import date, timedelta

import pytest

from conftest import add_point, add_user, db, io, login
from app import activity, export


def admin(app):
    add_user(app)
    client = app.test_client()
    login(client)
    return client


def log(app, category, action, ts, **kw):
    with db(app) as conn:
        activity.log(conn, category, action, **kw)
        conn.execute("UPDATE activity SET ts = ? WHERE id = (SELECT MAX(id) FROM activity)", (ts,))


def download(client, **params):
    params.setdefault("show", "all")
    params.setdefault("start", (date.today() - timedelta(days=6)).isoformat())
    params.setdefault("end", date.today().isoformat())
    return client.get("/admin/activity/download", query_string=params)


def rows_of(resp):
    text = resp.data.decode("utf-8-sig")
    return list(csv.reader(iolib.StringIO(text)))


def test_csv_download(app):
    client = admin(app)
    now = int(time.time())
    log(app, "control", "on", now - 60, target="Porch light", username="admin", ip="10.0.0.5")
    log(app, "automation", "off", now - 30, target="Aircon", detail="Rule: Cool down")
    log(app, "input", "active", now - 10, target="Front door", detail="Open")
    resp = download(client, format="csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert resp.data.startswith(b"\xef\xbb\xbf")       # Excel reads it as UTF-8
    assert "attachment;" in resp.headers["Content-Disposition"]
    assert "coilgate-activity-" in resp.headers["Content-Disposition"]
    rows = rows_of(resp)
    assert rows[0] == export.ACTIVITY_HEADER
    assert [r[2:5] for r in rows[1:]] == [
        ["Porch light", "Turned on", "admin"],
        ["Aircon", "Turned off", "Rule: Cool down"],
        ["Front door", "Open", "Input"],
    ]
    assert rows[1][6] == "10.0.0.5"


def test_date_range_and_area(app):
    client = admin(app)
    today = date.today()
    old = int(time.mktime((today - timedelta(days=10)).timetuple())) + 3600
    recent = int(time.time()) - 5
    log(app, "control", "on", old, target="Old")
    log(app, "control", "on", recent, target="New")
    log(app, "settings", "updated", recent, target="Settings")
    rows = rows_of(download(client, format="csv", show="control"))
    assert [r[2] for r in rows[1:]] == ["New"]
    rows = rows_of(download(client, format="csv", start=(today - timedelta(days=11)).isoformat()))
    assert [r[2] for r in rows[1:]][:3] == ["Old", "New", "Settings"]
    one_day = (today - timedelta(days=10)).isoformat()
    rows = rows_of(download(client, format="csv", start=one_day, end=one_day))
    assert [r[2] for r in rows[1:]] == ["Old"]


def test_formula_like_text_is_neutralised(app):
    client = admin(app)
    add_point(app, name="=HYPERLINK(\"http://x\")")
    log(app, "control", "on", int(time.time()), target="=HYPERLINK(\"http://x\")")
    rows = rows_of(download(client, format="csv"))
    assert rows[1][2].startswith("'=")


def test_signin_history(app):
    client = admin(app)
    login(app.test_client(), password="wrong password entirely")
    rows = rows_of(download(client, format="csv", show="signin"))
    assert rows[0] == export.SIGNIN_HEADER
    results = [(r[1], r[3]) for r in rows[1:]]
    assert ("Sign-in", "Failed") in results and ("Sign-in", "Succeeded") in results


def test_pdf_download(app):
    client = admin(app)
    for n in range(40):
        log(app, "control", "on", int(time.time()) - n, target=f"Relay {n} °C Ł →", username="admin")
    resp = download(client, format="pdf")
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF-")
    assert resp.headers["Content-Disposition"].endswith('.pdf"')


def test_empty_pdf(app):
    client = admin(app)
    resp = download(client, format="pdf", show="schedule")
    assert resp.status_code == 200 and resp.data.startswith(b"%PDF-")


def test_downloads_are_logged(app):
    client = admin(app)
    download(client, format="csv", show="control")
    with db(app) as conn:
        row = conn.execute("SELECT * FROM activity WHERE category = 'export'").fetchone()
    assert (row["action"], row["target"], row["username"]) == ("csv", "Activity log", "admin")
    assert row["detail"].startswith("Controls, ") and row["detail"].endswith("0 entries")


@pytest.mark.parametrize("params, message", [
    ({"start": "2026-13-01"}, "Choose both dates"),
    ({"start": "2026-10-10", "end": "2026-10-01"}, "end date is before"),
    ({"format": "xlsx"}, "Choose what to download"),
])
def test_bad_requests_explain_themselves(app, params, message):
    client = admin(app)
    params.setdefault("format", "csv")
    resp = download(client, **params)
    assert resp.status_code == 302
    assert message in client.get(resp.headers["Location"]).get_data(as_text=True)


def test_too_many_rows_for_a_pdf(app, monkeypatch):
    client = admin(app)
    monkeypatch.setattr(export, "PDF_ROW_LIMIT", 3)
    for n in range(5):
        log(app, "control", "on", int(time.time()) - n, target="Relay")
    resp = download(client, format="pdf")
    assert resp.status_code == 302
    assert "download a CSV" in client.get(resp.headers["Location"]).get_data(as_text=True)


def test_users_cannot_download(app):
    add_user(app, username="guest", role="user")
    client = app.test_client()
    login(client, username="guest")
    assert download(client, format="csv").status_code in (403, 404)


def test_activity_page_offers_downloads(app):
    client = admin(app)
    page = client.get("/admin/activity?show=automation").get_data(as_text=True)
    assert 'Download <span class="muted">Rules ran</span>' in page and 'value="automation"' in page
    assert 'name="format" value="pdf"' in page
