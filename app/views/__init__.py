# SPDX-License-Identifier: GPL-2.0-or-later
"""Routes. One blueprint ("main"), split across modules by area."""
from .common import bp  # noqa: F401
from . import account, admin, auth, automation, controls, groups, pinout  # noqa: F401  (registers routes)
