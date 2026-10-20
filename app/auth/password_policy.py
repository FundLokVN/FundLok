"""What a new password must meet (compliance audit SEC-03).

Applies wherever a password is chosen: sign-up, reset (which also completes
an admin invite), a first password, and a change. Never at sign-in, where a
password set under the older rules must keep working.

Two checks, at two layers:

- `check_password_strength` is pure, so the request schemas run it and a weak
  password is a 422 before any work happens: at least 8 characters, at most
  72 bytes, with a lowercase letter, an uppercase letter, a digit and a symbol.
- `reject_breached_password` asks Have I Been Pwned whether the password has
  appeared in a breach. It needs the network, so the services call it.
  k-anonymity: only the first 5 hex characters of the password's SHA-1 leave
  the server, and padded responses hide even which prefix matched. It fails
  open: if HIBP is down or slower than 2s the password is accepted and a
  warning logged, because a third-party outage must not stop sign-up.
"""
import hashlib
import logging

import httpx
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

MIN_LENGTH = 8
# Argon2 has no such limit; the ceiling dates from bcrypt, which silently
# truncated past 72 bytes. Kept so every password the app ever accepted is
# still a valid choice, and so a pasted megabyte is refused before hashing.
MAX_BYTES = 72

HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"
HIBP_TIMEOUT_SECONDS = 2.0
BREACHED_ERROR_CODE = "PASSWORD_BREACHED"


def check_password_strength(password: str) -> str:
    """Return `password` unchanged, or raise ValueError naming what it lacks."""
    if len(password.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"Password must be at most {MAX_BYTES} bytes")

    missing = []
    if len(password) < MIN_LENGTH:
        missing.append(f"at least {MIN_LENGTH} characters")
    if not any(c.islower() for c in password):
        missing.append("a lowercase letter")
    if not any(c.isupper() for c in password):
        missing.append("an uppercase letter")
    if not any(c.isdigit() for c in password):
        missing.append("a digit")
    if not any(not c.isalnum() and not c.isspace() for c in password):
        missing.append("a symbol")
    if missing:
        listed = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
        raise ValueError(f"Password needs {listed}")
    return password


async def _fetch_range(prefix: str) -> str:
    async with httpx.AsyncClient(timeout=HIBP_TIMEOUT_SECONDS) as client:
        response = await client.get(
            HIBP_RANGE_URL.format(prefix=prefix),
            headers={"Add-Padding": "true", "User-Agent": "FundLok-password-check"},
        )
        response.raise_for_status()
        return response.text


async def is_breached(password: str) -> bool:
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        body = await _fetch_range(prefix)
    except httpx.HTTPError as exc:
        logger.warning("Breached-password check skipped, HIBP unreachable: %s", type(exc).__name__)
        return False

    for line in body.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip() != suffix:
            continue
        try:
            # Padding rows are fake suffixes with a count of 0.
            return int(count) > 0
        except ValueError:
            return False
    return False


async def reject_breached_password(password: str) -> None:
    if await is_breached(password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This password has appeared in a known data breach. Choose a different one.",
            headers={"X-Error-Code": BREACHED_ERROR_CODE},
        )
