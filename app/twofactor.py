# SPDX-License-Identifier: GPL-2.0-or-later
"""TOTP two-step verification (RFC 6238), compatible with any authenticator app."""
from __future__ import annotations

import base64
import hmac
import io
import time

import pyotp
import qrcode
import qrcode.image.svg

STEP_SECONDS = 30
ALLOWED_DRIFT_STEPS = 1   # accept the previous and next code for clock drift


def new_secret() -> str:
    return pyotp.random_base32()   # 160 bits


def provisioning_uri(secret: str, username: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def qr_data_uri(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, border=4)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def grouped(secret: str) -> str:
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def clean_code(raw: str) -> str:
    return "".join(ch for ch in raw if ch.isdigit())


def verify(secret: str, code: str, last_step: int) -> int | None:
    """Return the matched time step if the code is valid and unused, else None.

    Codes at or before last_step are rejected, so a code that has been used
    (or observed) can't be replayed within its 30-90 second validity window.
    """
    code = clean_code(code)
    if len(code) != 6 or not secret:
        return None
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // STEP_SECONDS
    matched = None
    for step in range(current - ALLOWED_DRIFT_STEPS, current + ALLOWED_DRIFT_STEPS + 1):
        # Compare every candidate so timing doesn't reveal which one matched.
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), code) and step > last_step:
            matched = step
    return matched
