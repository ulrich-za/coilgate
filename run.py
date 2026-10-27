#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Start CoilGate with Waitress, a production WSGI server that runs the same
on Windows and Linux (Flask's built-in server is for development only)."""
import ipaddress
import logging

from waitress import serve

from app import create_app

app = create_app()
cfg = app.config["COILGATE"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("coilgate")


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if not is_loopback(cfg["host"]) and not cfg["secure_cookies"]:
    log.warning(
        "Listening on %s over plain HTTP: passwords would cross the network unencrypted. "
        "Put Caddy in front (see README) and set secure_cookies = true.", cfg["host"])

proxy_options = {}
if cfg["trust_proxy"]:
    # Accept X-Forwarded-* only from the reverse proxy, so the real client IP
    # reaches the throttling and audit log. From anyone else they're stripped.
    proxy_options = {
        "trusted_proxy": cfg["proxy_address"],
        "trusted_proxy_count": int(cfg["trust_proxy"]),
        "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
    }
    log.info("Trusting proxy headers from %s", cfg["proxy_address"])

log.info("CoilGate listening on http://%s:%s", cfg["host"], cfg["port"])
serve(
    app,
    host=cfg["host"],
    port=cfg["port"],
    threads=cfg["threads"],
    ident="",                      # don't advertise the server software
    max_request_body_size=65536,
    clear_untrusted_proxy_headers=True,
    **proxy_options,
)
