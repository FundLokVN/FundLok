import logging

import httpx
from fastapi import HTTPException
from app.core.config import settings

logger = logging.getLogger(__name__)

def _captcha_secret() -> str | None:
    """The Turnstile secret, or None when this deployment runs without captcha.

    With no secret, CAPTCHA_REQUIRED decides (compliance audit SEC-08). Until
    then a missing secret switched captcha off everywhere, so a deployment
    that lost the secret silently accepted every request. Required is the
    default, so that case now refuses instead; local dev and the tests set
    CAPTCHA_REQUIRED=False.
    """
    if settings.CLOUDFLARE_TURNSTILE_SECRET_KEY:
        return settings.CLOUDFLARE_TURNSTILE_SECRET_KEY
    if settings.CAPTCHA_REQUIRED:
        logger.error("Captcha is required but CLOUDFLARE_TURNSTILE_SECRET_KEY is not set; refusing")
        raise HTTPException(
            status_code=503,
            detail="Captcha service unavailable",
            headers={"X-Error-Code": "CAPTCHA_UNCONFIGURED"},
        )
    return None


async def verify_turnstile_token_async(token: str | None) -> None:
    """Verify a Cloudflare Turnstile token without blocking the event loop."""
    secret = _captcha_secret()
    if secret is None:
        return

    if not token:
        raise HTTPException(status_code=400, detail="Turnstile token is missing")

    payload = {
        "secret": secret,
        "response": token,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=payload,
            )
            result = resp.json()
    except httpx.RequestError:
        raise HTTPException(status_code=500, detail="Error communicating with captcha service")
    except ValueError:
        # A non-JSON body -- a Cloudflare outage page, a 5xx HTML error. Without
        # this it escaped as an unhandled exception. Fails CLOSED: an
        # unverifiable token is not a verified one.
        logger.warning("Turnstile siteverify returned non-JSON (status %s)", resp.status_code)
        raise HTTPException(status_code=503, detail="Captcha service unavailable")

    if not result.get("success"):
        logger.warning(
            "Turnstile verification failed: error-codes=%s",
            result.get("error-codes"),
        )
        raise HTTPException(
            status_code=400, detail="Cloudflare Turnstile verification failed"
        )
