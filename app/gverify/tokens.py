"""Short-lived handoff tokens for the phone capture flow.

A logged-in investor on desktop mints a token, encodes it in a QR, and the
phone uses it as a Bearer credential for exactly one purpose: submitting the
KYC images. Mirrors the purpose-scoped token pattern in app/utils/jwt.py
(email_verification / password_reset) but lives here — it is GVerify-specific.

Single use. The token names the user's latest attempt at the moment it was
minted, and it only authorises a submission while that is still the latest:
the first attempt it creates spends it. No table of spent tokens is needed,
and a submission that creates no attempt (a bad image, a mode change) leaves
it usable.
"""
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
import jwt
from jwt import InvalidTokenError

from app.core.config import settings
from app.utils.jwt import create_access_token

_PURPOSE = "gverify_kyc_handoff"
# Claim naming the latest attempt when the token was minted; "" for none.
_PREVIOUS_ATTEMPT = "prev"

# Long enough to scan + photograph three images, short enough that a leaked
# QR (screenshot, shoulder-surf) goes stale quickly.
HANDOFF_TTL = timedelta(minutes=10)

INVALID_HANDOFF_DETAIL = "Invalid or expired handoff token"


@dataclass(frozen=True)
class HandoffClaims:
    user_id: str
    previous_attempt_id: str  # "" when the user had no attempt yet


def create_handoff_token(user_id, previous_attempt_id=None) -> str:
    return create_access_token(
        data={
            "sub": str(user_id),
            "purpose": _PURPOSE,
            _PREVIOUS_ATTEMPT: str(previous_attempt_id) if previous_attempt_id else "",
        },
        expires_delta=HANDOFF_TTL,
    )


def verify_handoff_token(token: str) -> HandoffClaims:
    """Validate a handoff token and return its claims. Raises 401 otherwise.

    Whether it is still unspent is for the caller to check, under the
    per-user lock (see _run_verification in the router).
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"verify_aud": False},
        )
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail=INVALID_HANDOFF_DETAIL)
    if payload.get("purpose") != _PURPOSE:
        raise HTTPException(status_code=401, detail=INVALID_HANDOFF_DETAIL)
    user_id = payload.get("sub")
    previous = payload.get(_PREVIOUS_ATTEMPT)
    # A token minted before single use existed has no claim; it is refused
    # rather than treated as reusable. Its 10 minutes are long gone anyway.
    if not user_id or not isinstance(previous, str):
        raise HTTPException(status_code=401, detail=INVALID_HANDOFF_DETAIL)
    return HandoffClaims(user_id=user_id, previous_attempt_id=previous)
