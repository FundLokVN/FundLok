"""Push nudges to open tabs through PartyKit.

PartyKit is only a fan-out: the message carries no notification content, just
"something changed", and the client refetches through the normal
cookie-authenticated API. Nothing here is allowed to fail a request -- polling
is the fallback, so a PartyKit outage costs latency, never correctness.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable
from uuid import UUID

import httpx
import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 60
TOKEN_AUDIENCE = "partykit"
PUBLISH_TIMEOUT_SECONDS = 3.0


def _publish_url(user_id: str) -> str:
    host = settings.PARTYKIT_HOST or ""
    # `partykit dev` serves plain http on localhost; the hosted platform is https.
    local = host.startswith(("localhost", "127.0.0.1"))
    return f"{'http' if local else 'https'}://{host}/parties/main/{user_id}"


def is_enabled() -> bool:
    return bool(
        settings.PARTYKIT_HOST
        and settings.PARTYKIT_JWT_SECRET
        and settings.PARTYKIT_PUBLISH_SECRET
    )


def mint_token(user_id: UUID) -> str | None:
    """A short-lived token the browser presents to PartyKit for its own room."""
    if not settings.PARTYKIT_JWT_SECRET:
        return None
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(seconds=TOKEN_TTL_SECONDS),
        },
        settings.PARTYKIT_JWT_SECRET,
        algorithm="HS256",
    )


async def publish(user_ids: Iterable[UUID]) -> None:
    """Tell each user's open tabs to refetch. Call AFTER the commit.

    Never raises: the notification is already durable, and the client polls.
    """
    if not is_enabled():
        return
    unique = {str(u) for u in user_ids}
    if not unique:
        return
    headers = {"Authorization": f"Bearer {settings.PARTYKIT_PUBLISH_SECRET}"}
    try:
        async with httpx.AsyncClient(timeout=PUBLISH_TIMEOUT_SECONDS) as client:
            for uid in unique:
                try:
                    await client.post(
                        _publish_url(uid),
                        headers=headers,
                        json={"type": "notifications.changed"},
                    )
                except httpx.HTTPError:
                    logger.warning("PartyKit publish failed", exc_info=True)
    except Exception:  # noqa: BLE001 - must never break the caller
        logger.warning("PartyKit publish failed", exc_info=True)
