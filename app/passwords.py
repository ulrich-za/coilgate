# SPDX-License-Identifier: GPL-2.0-or-later
"""Password hashing and password policy."""
from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
import urllib.error
import urllib.request

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2-cffi defaults to Argon2id with the RFC 9106 "low memory" profile:
# 64 MiB memory, 3 iterations, 4 lanes. Comfortable on a Pi 5.
_hasher = PasswordHasher()

# Verified against when the username doesn't exist, so a failed login costs
# the same time either way and can't be used to discover valid usernames.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(24))

MAX_PASSWORD_LENGTH = 256
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

# The breached-password check (Have I Been Pwned) does most of the work; this
# short list catches the obvious cases when the Pi is offline.
_COMMON_WORDS = {
    "password", "passw0rd", "qwerty", "letmein", "welcome", "admin", "administrator",
    "iloveyou", "monkey", "dragon", "football", "baseball", "sunshine", "princess",
    "trustno", "master", "shadow", "superman", "batman", "login", "changeme",
    "secret", "default", "raspberry", "raspberrypi", "windows", "abc", "test",
}
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "qwertyuiopasdfghjklzxcvbnm",
    "01234567890123456789",
)


def normalize_username(raw: str) -> str:
    return unicodedata.normalize("NFKC", raw).strip().lower()


def normalize_password(raw: str) -> str:
    # NIST SP 800-63B: apply Unicode normalisation before hashing so the same
    # password typed on different keyboards/OSes produces the same bytes.
    return unicodedata.normalize("NFKC", raw)


def hash_password(password: str) -> str:
    return _hasher.hash(normalize_password(password))


def verify_password(stored_hash: str | None, password: str) -> tuple[bool, bool]:
    """Return (matches, needs_rehash). Always does one full Argon2 verification."""
    candidate = normalize_password(password)[: MAX_PASSWORD_LENGTH * 4]
    target = stored_hash or _DUMMY_HASH
    try:
        _hasher.verify(target, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    if stored_hash is None:
        return False, False
    return True, _hasher.check_needs_rehash(stored_hash)


def _is_sequence(s: str) -> bool:
    if len(s) < 4:
        return False
    return any(s in seq or s in seq[::-1] for seq in _SEQUENCES)


def _is_repeated_word(core: str) -> bool:
    for word in _COMMON_WORDS:
        if core and len(core) % len(word) == 0 and core == word * (len(core) // len(word)):
            return True
    return False


def policy_problems(password: str, username: str, app_name: str, min_length: int) -> list[str]:
    """Check a new password. Returns human-readable problems; empty means acceptable.

    Deliberately no composition rules (no "must contain a symbol"): length plus
    a blocklist is what NIST SP 800-63B-4 recommends.
    """
    pw = normalize_password(password)
    problems: list[str] = []

    if len(pw) < min_length:
        problems.append(f"Use at least {min_length} characters. A few unrelated words works well.")
    if len(pw) > MAX_PASSWORD_LENGTH:
        problems.append(f"Use no more than {MAX_PASSWORD_LENGTH} characters.")
    if problems:
        return problems

    lowered = pw.lower()
    letters_only = re.sub(r"[^a-z]", "", lowered)
    compact = re.sub(r"\s", "", lowered)

    if len(set(pw)) < 5:
        problems.append("It repeats too few different characters.")
    elif _is_sequence(compact) or _is_sequence(letters_only):
        problems.append("It's a keyboard or number sequence.")
    elif letters_only and (letters_only in _COMMON_WORDS or _is_repeated_word(letters_only)):
        problems.append("It's built from a very common password.")
    elif not letters_only and len(set(compact)) < 8:
        problems.append("It's a predictable string of digits.")

    for context in {username.lower(), app_name.lower()}:
        if len(context) >= 3 and context in lowered:
            problems.append("Don't include your username or the site name.")
            break
    return problems


_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i


def generate_temporary_password() -> str:
    """A one-time password for a new or reset account: about 98 bits, easy to read out."""
    groups = ["".join(secrets.choice(_TEMP_ALPHABET) for _ in range(5)) for _ in range(4)]
    return "-".join(groups)


def pwned_count(password: str, timeout: float = 5.0) -> int | None:
    """Times this password appears in known breaches, via the Have I Been Pwned
    range API (k-anonymity: only the first 5 hex characters of the SHA-1 leave
    this machine). Returns None if the service can't be reached.
    """
    digest = hashlib.sha1(normalize_password(password).encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    req = urllib.request.Request(
        f"https://api.pwnedpasswords.com/range/{prefix}",
        headers={"Add-Padding": "true", "User-Agent": "coilgate"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("ascii", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    for line in body.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip() == suffix:
            return int(count.strip() or 0)
    return 0
