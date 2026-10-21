"""Per-IP rate limiting for every request (compliance audit SEC-08).

Login was throttled per account only (app/auth/service.py), so one address
could walk a list of emails, and nothing else had a limit at all. This
middleware counts requests per client IP in fixed one-minute windows:

- `RATE_LIMIT_SENSITIVE_PER_MINUTE` for the public, unauthenticated POSTs in
  SENSITIVE_PATHS: sign-in, sign-up, password reset, contact and the public
  rate calculator. These are what a script hammers.
- `RATE_LIMIT_PER_MINUTE` for everything else, generous enough that a real
  user never meets it.

Over the limit is 429 with `Retry-After` and `X-Error-Code: RATE_LIMITED`.

Counters live in Redis (app/core/redis_client.py), so the limit holds across
Cloud Run instances. If Redis is unreachable the counter falls back to this
instance's memory and retries Redis after REDIS_RETRY_SECONDS: a weaker limit
beats none, and a Redis outage must not take the API down.

It is off unless RATE_LIMIT_ENABLED is set, and should only be set once the
audit log shows real visitor addresses: BACKEND_SECRET_KEY set and the
frontend sending X-Client-IP. The key is the client address from
app/utils/client_ip.py. Without those, behind a proxy that address is the
proxy's, and the limit would throttle every visitor as one caller.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from redis.exceptions import RedisError
from starlette.responses import JSONResponse

# The module, as client_ip.py does: read per request, so tests can switch
# settings without rebuilding the app.
from app.core import config
from app.core.redis_client import redis_client

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
REDIS_RETRY_SECONDS = 30
ERROR_CODE = "RATE_LIMITED"

EXEMPT_PATHS = frozenset({"/health"})
SENSITIVE_PATHS = frozenset(
    {
        "/auth/login",
        "/auth/login/2fa",
        "/auth/passkeys/login/options",
        "/auth/passkeys/login/verify",
        "/auth/oauth/login",
        "/auth/register",
        "/auth/resend-verification",
        "/auth/forgot-password",
        "/auth/reset-password",
        "/contact",
        "/api/v1/rates/calculate",
        "/api/v1/rates/investor/leads",
    }
)


@dataclass(frozen=True)
class Rule:
    name: str
    limit: int


class WindowCounter:
    """Counts hits per key in fixed windows: Redis first, memory as fallback."""

    def __init__(self, *, use_redis: bool = True) -> None:
        self._use_redis = use_redis
        self._redis_retry_at = 0.0
        self._memory: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, now: float | None = None) -> tuple[int, int]:
        """Count one hit; return (hits in this window, seconds until it ends)."""
        now = time.time() if now is None else now
        window_start = math.floor(now / WINDOW_SECONDS) * WINDOW_SECONDS
        window_end = window_start + WINDOW_SECONDS
        retry_after = max(1, math.ceil(window_end - now))
        bucket = f"ratelimit:{key}:{window_start}"

        if self._use_redis and now >= self._redis_retry_at:
            try:
                async with redis_client.pipeline(transaction=False) as pipe:
                    pipe.incr(bucket)
                    pipe.expire(bucket, WINDOW_SECONDS)
                    count, _ = await pipe.execute()
                return int(count), retry_after
            except (RedisError, OSError) as exc:
                self._redis_retry_at = now + REDIS_RETRY_SECONDS
                logger.warning(
                    "Rate limiter counting in memory for %ss: Redis unavailable (%s)",
                    REDIS_RETRY_SECONDS,
                    type(exc).__name__,
                )

        return self._memory_hit(bucket, now, window_end), retry_after

    def _memory_hit(self, bucket: str, now: float, window_end: float) -> int:
        if len(self._memory) > 10_000:
            self._memory = {k: v for k, v in self._memory.items() if v[1] > now}
        count, _ = self._memory.get(bucket, (0, window_end))
        count += 1
        self._memory[bucket] = (count, window_end)
        return count


counter = WindowCounter()


def _rule_for(method: str, path: str) -> Rule:
    settings = config.settings
    if method == "POST" and path in SENSITIVE_PATHS:
        return Rule("sensitive", settings.RATE_LIMIT_SENSITIVE_PER_MINUTE)
    return Rule("general", settings.RATE_LIMIT_PER_MINUTE)


class RateLimitMiddleware:
    """Pure ASGI. Sits inside TrustedProxyMiddleware, which settles the client
    address first, and inside CORS, so a browser can read the 429."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not config.settings.RATE_LIMIT_ENABLED
            or scope["method"] == "OPTIONS"
            or scope["path"] in EXEMPT_PATHS
            or not scope.get("client")
        ):
            return await self.app(scope, receive, send)

        rule = _rule_for(scope["method"], scope["path"])
        hits, retry_after = await counter.hit(f"{rule.name}:{scope['client'][0]}")
        if hits > rule.limit:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Try again shortly."},
                headers={"Retry-After": str(retry_after), "X-Error-Code": ERROR_CODE},
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)
