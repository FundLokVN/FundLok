"""Only Google Cloud Scheduler may run a job.

Cloud Scheduler calls the job URL with an OIDC ID token signed by Google for
its service account (`--oidc-service-account-email`), for an audience we
choose (`--oidc-token-audience`). The prod-api service is public (the web app
calls it), so Cloud Run does not check the token: this does. It verifies
Google's signature, the issuer, the expiry and the audience
(google-auth), then that the token is that service account's.

Configuration (environment, read on every call so tests can set it):
  SCHEDULER_SERVICE_ACCOUNT  the scheduler's service account email
  SCHEDULER_AUDIENCE         the audience its jobs ask for (the service URL)
Either unset: every job answers 503. Nothing runs by accident.
"""

import asyncio
import logging
import os

from fastapi import Header, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)


def _config() -> tuple[str | None, str | None]:
    return (
        (os.getenv("SCHEDULER_SERVICE_ACCOUNT") or "").strip() or None,
        (os.getenv("SCHEDULER_AUDIENCE") or "").strip() or None,
    )


def _verify(token: str, audience: str) -> dict:
    # Fetches Google's public certificates over HTTP (sync): run in a thread.
    return id_token.verify_oauth2_token(token, google_requests.Request(), audience)


async def require_scheduler(authorization: str | None = Header(default=None)) -> dict:
    """The verified token's claims, or 401/403/503."""
    account, audience = _config()
    if not account or not audience:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scheduled jobs are not configured",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        claims = await asyncio.to_thread(_verify, authorization[7:].strip(), audience)
    except Exception:  # noqa: BLE001 - any failure is the same answer
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from None
    if claims.get("email") != account or claims.get("email_verified") is not True:
        logger.warning("Scheduled job refused a token for another account")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    return claims
