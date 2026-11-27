"""The Claude call every console assistant makes, and its failure modes.

One structured-output request: `claude-opus-5-5` at low effort (short copy
from text the admin supplies), a JSON schema from a Pydantic model, and
server-side refusal fallbacks. It answers or fails within DEADLINE_SECONDS
because the browser reaches the API through the Next.js /api rewrite, which
gives up after 30 s.

Failures come back as HTTP errors whose X-Error-Code is the assistant's own
prefix plus one of BUSY (slow, overloaded, rate limited, unreachable: try
again), NO_CREDIT (the Console workspace hit its spend limit or ran out of
credit), FAILED (key refused, refusal, unusable answer) or UNAVAILABLE (not
switched on for this admin). The console translates each by its code.
"""
from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache
from typing import Literal, TypeVar

import anthropic
from fastapi import HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.users.models import Role, User

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
EFFORT = "low"
MAX_TOKENS = 8_000
DEADLINE_SECONDS = 25.0
# On a refusal the API re-runs the request on a model its policy routes to.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

Audience = Literal["system_admin", "all_admins"]
Answer = TypeVar("Answer", bound=BaseModel)


def error(status_code: int, prefix: str, kind: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": f"{prefix}_{kind}"})


def unusable(prefix: str, reason: str) -> HTTPException:
    logger.warning("%s: unusable answer: %s", prefix, reason)
    return error(status.HTTP_502_BAD_GATEWAY, prefix, "FAILED", "The answer could not be used. Try again.")


# --------------------------------------------------------------------------- #
# Who may use an assistant
# --------------------------------------------------------------------------- #


def available_to(user: User, audience: Audience) -> bool:
    """The key is set and the admin is in the audience (SYSTEM_ADMIN first,
    while answers are reviewed; then every admin)."""
    if not settings.ANTHROPIC_API_KEY:
        return False
    if audience == "all_admins":
        return user.role in (Role.ADMIN.value, Role.SYSTEM_ADMIN.value)
    return user.role == Role.SYSTEM_ADMIN.value


def require_access(user: User, audience: Audience, prefix: str) -> None:
    if not settings.ANTHROPIC_API_KEY:
        raise error(status.HTTP_503_SERVICE_UNAVAILABLE, prefix, "UNAVAILABLE", "The assistant is not switched on.")
    if not available_to(user, audience):
        raise error(
            status.HTTP_403_FORBIDDEN, prefix, "UNAVAILABLE", "The assistant is not switched on for you yet."
        )


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _client_for(api_key: str) -> anthropic.AsyncAnthropic:
    """One client per key, so connections are reused between clicks."""
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=anthropic.Timeout(DEADLINE_SECONDS, connect=5.0),
        max_retries=1,
    )


def _busy(prefix: str) -> HTTPException:
    return error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        prefix,
        "BUSY",
        "Claude is slow or unavailable right now. Try again in a moment.",
    )


def _no_credit(prefix: str) -> HTTPException:
    return error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        prefix,
        "NO_CREDIT",
        "The assistant is paused: the Claude workspace is out of credit for this month.",
    )


def _failed(prefix: str) -> HTTPException:
    return error(status.HTTP_502_BAD_GATEWAY, prefix, "FAILED", "The assistant failed. Try again.")


async def ask(*, prefix: str, system: str, user_message: str, output_format: type[Answer]) -> Answer:
    """Claude's answer as an `output_format` instance, or an HTTPException
    carrying `prefix`_BUSY / _NO_CREDIT / _FAILED."""
    client = _client_for(settings.ANTHROPIC_API_KEY or "")
    started = time.monotonic()
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            response = await client.beta.messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": EFFORT},
                system=system,
                messages=[{"role": "user", "content": user_message}],
                output_format=output_format,
            )
    except TimeoutError:
        logger.warning("%s: no answer within %.0f s", prefix, DEADLINE_SECONDS)
        raise _busy(prefix) from None
    except (
        anthropic.EnforcedSpendLimitReachedError,
        anthropic.ContractSpendLimitReachedError,
        anthropic.PaymentRequiredError,
    ) as exc:
        logger.error("%s: spend limit reached (%s)", prefix, exc.status_code)
        raise _no_credit(prefix) from None
    except anthropic.RateLimitError:
        raise _busy(prefix) from None
    except anthropic.BadRequestError as exc:
        # An exhausted prepaid balance is a plain 400 with no error code of its own.
        if "credit balance" in str(exc.message).lower():
            logger.error("%s: credit balance too low", prefix)
            raise _no_credit(prefix) from None
        logger.error("%s: request refused: %s", prefix, exc.message)
        raise _failed(prefix) from None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        logger.error("%s: the API key was refused (%s)", prefix, exc.status_code)
        raise _failed(prefix) from None
    except anthropic.InternalServerError:  # 5xx, overloaded included
        raise _busy(prefix) from None
    except anthropic.APIConnectionError:  # timeouts included
        raise _busy(prefix) from None
    except anthropic.APIStatusError as exc:
        logger.error("%s: unexpected API error %s", prefix, exc.status_code)
        raise _failed(prefix) from None
    except ValueError as exc:  # the JSON did not match the schema (e.g. cut off)
        raise unusable(prefix, f"unparseable answer: {type(exc).__name__}") from None

    logger.info(
        "%s: model=%s stop=%s input_tokens=%s output_tokens=%s seconds=%.1f request_id=%s",
        prefix,
        response.model,
        response.stop_reason,
        response.usage.input_tokens,
        response.usage.output_tokens,
        time.monotonic() - started,
        response._request_id,
    )
    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise unusable(prefix, f"refused (category {category})")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise unusable(prefix, f"no complete answer (stop {response.stop_reason})")
    return response.parsed_output
