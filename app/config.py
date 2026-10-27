# SPDX-License-Identifier: GPL-2.0-or-later
"""Deployment configuration.

Only settings that decide how CoilGate is deployed live here: network, HTTPS,
proxy, database location, GPIO mode and where the Admin area may be reached
from. They come from three layers, later layers winning:
  1. DEFAULTS below
  2. config.toml in the project root (optional; copy config.example.toml)
  3. Environment variables named COILGATE_<SETTING>, e.g. COILGATE_PORT=8080

Everything else (inputs and outputs, accounts, security and session settings)
is stored in the database and changed from the Admin area.
"""
from __future__ import annotations

import ipaddress
import os
import secrets
import stat
import sys
import tomllib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
PRODUCT_NAME = "CoilGate"

DEFAULTS: dict[str, object] = {
    # Where the app listens. Keep on 127.0.0.1: Caddy or Cloudflare Tunnel
    # faces the network and forwards to this port.
    "host": "127.0.0.1",
    "port": 8000,
    # Each open Controls page keeps one thread waiting for changes, so allow
    # for several phones and browsers at once.
    "threads": 16,
    # True once the site is reached over HTTPS.
    "secure_cookies": False,
    # Number of reverse proxies in front (Caddy or cloudflared = 1). Forwarded
    # client IPs are only believed from proxy_address.
    "trust_proxy": 0,
    "proxy_address": "127.0.0.1",
    "database": "instance/coilgate.db",
    # "auto" drives real GPIO pins on a Pi and simulates them elsewhere.
    "gpio_mode": "auto",
    # Networks allowed to open the Admin area, e.g. ["192.168.1.0/24"].
    # Empty means anywhere (still admin accounts only).
    "admin_networks": [],
    # Set to false to pause every rule (for example while rewiring).
    "rule_engine": True,
}

# Settings from earlier versions (Gatehouse) that now live in the database.
# If present in config.toml they're copied into the database once.
LEGACY_KEYS = {
    "app_name", "session_idle_minutes", "session_absolute_hours", "min_password_length",
    "check_pwned_passwords", "max_failures_per_user", "max_failures_per_ip",
    "failure_window_minutes", "event_retention_days", "require_2fa_for_controls",
    "max_control_actions_per_minute", "outputs",
}


def _coerce(value: str, default: object) -> object:
    if isinstance(default, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(value)
    if isinstance(default, list):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def load_settings(overrides: dict | None = None) -> dict:
    """Return the deployment settings. Legacy keys are returned under "legacy"."""
    settings = dict(DEFAULTS)
    legacy: dict = {}

    config_file = Path(os.environ.get("COILGATE_CONFIG", BASE_DIR / "config.toml"))
    if config_file.is_file():
        with config_file.open("rb") as fh:
            data = tomllib.load(fh)
        unknown = set(data) - set(DEFAULTS) - LEGACY_KEYS
        if unknown:
            raise SystemExit(f"Unknown setting(s) in {config_file}: {', '.join(sorted(unknown))}")
        legacy = {k: v for k, v in data.items() if k in LEGACY_KEYS}
        settings.update({k: v for k, v in data.items() if k in DEFAULTS})

    for key, default in DEFAULTS.items():
        env_value = os.environ.get(f"COILGATE_{key.upper()}")
        if env_value is not None:
            settings[key] = _coerce(env_value, default)

    runtime_overrides = {}
    for key, value in (overrides or {}).items():
        if key in DEFAULTS:
            settings[key] = value
        else:
            runtime_overrides[key] = value   # applied to the database at startup

    try:
        settings["admin_networks"] = [
            ipaddress.ip_network(str(n), strict=False) for n in settings["admin_networks"]
        ]
    except ValueError as exc:
        raise SystemExit(f"admin_networks: {exc}") from None

    db_path = Path(str(settings["database"]))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    settings["database"] = str(db_path)
    settings["legacy"] = legacy
    settings["runtime_overrides"] = runtime_overrides
    return settings


def ensure_private_dir(path: Path) -> None:
    """Create a directory readable only by the owner (no-op permissions on Windows)."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(stat.S_IRWXU)


def load_secret_key(instance_dir: Path = INSTANCE_DIR) -> str:
    """Return the signing key, generating and storing one on first run."""
    env_key = os.environ.get("COILGATE_SECRET_KEY")
    if env_key:
        return env_key

    ensure_private_dir(instance_dir)
    key_file = instance_dir / "secret_key"
    if key_file.is_file():
        return key_file.read_text(encoding="ascii").strip()

    key = secrets.token_urlsafe(48)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key)
    return key
