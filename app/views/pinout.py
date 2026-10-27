# SPDX-License-Identifier: GPL-2.0-or-later
"""The Pinout page: the Pi 5's 40-pin header, what each pin can do, and which
pins CoilGate uses, lit up live."""
from __future__ import annotations

from flask import render_template

from .. import pinout
from .common import bp, io, login_required


@bp.get("/pinout")
@login_required
def pinout_page():
    manager = io()
    return render_template(
        "pinout.html",
        rows=pinout.rows(manager.points()),
        interfaces=pinout.interfaces(),
        io=manager,
        version=manager.version,
        setup=manager.setup,
        source_commit=pinout.data()["source_commit"],
    )
