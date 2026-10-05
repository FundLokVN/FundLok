import logging

import httpx
from fastapi import HTTPException
from app.core.config import settings

logger = logging.getLogger(__name__)

def verify_turnstile_token(token: str | None) -> None:
    """
    Verifies the Cloudflare Turnstile token.
    Raises HTTPException if verification fails or the token is missing when required.
    """
    if settings.CLOUDFLARE_TURNSTILE_SECRET_KEY:
        if not token:
            raise HTTPException(status_code=400, detail="Turnstile token is missing")

        verify_url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        payload = {
            "secret": settings.CLOUDFLARE_TURNSTILE_SECRET_KEY,
            "response": token
        }
        try:
            with httpx.Client() as client:
                resp = client.post(verify_url, data=payload)
                result = resp.json()
                if not result.get("success"):
                    # The client-facing detail stays generic; the reason lives in
                    # error-codes (e.g. timeout-or-duplicate = single-use token
                    # re-submitted, invalid-input-response = wrong/expired token
                    # or sitekey/secret pair mismatch). Log it for diagnosis.
                    logger.warning(
                        "Turnstile verification failed: error-codes=%s",
                        result.get("error-codes"),
                    )
                    raise HTTPException(status_code=400, detail="Cloudflare Turnstile verification failed")
        except httpx.RequestError:
            raise HTTPException(status_code=500, detail="Error communicating with captcha service")
        except ValueError:
            # Non-JSON siteverify response; see the async twin. Fails closed.
            logger.warning("Turnstile siteverify returned non-JSON")
            raise HTTPException(status_code=503, detail="Captcha service unavailable")


async def verify_turnstile_token_async(token: str | None) -> None:
    """Async twin of `verify_turnstile_token`, same contract.

    The sync version opens a blocking `httpx.Client` inside whatever coroutine
    calls it, which parks the whole event loop for the duration of a network
    round trip to Cloudflare. That is tolerable on the contact form. It is not
    tolerable on an unauthenticated public endpoint, where it hands anyone a
    way to stall every other in-flight request by firing calculator submissions
    — the captcha would become the denial-of-service it was added to prevent.
    """
    if not settings.CLOUDFLARE_TURNSTILE_SECRET_KEY:
        return

    if not token:
        raise HTTPException(status_code=400, detail="Turnstile token is missing")

    payload = {
        "secret": settings.CLOUDFLARE_TURNSTILE_SECRET_KEY,
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
