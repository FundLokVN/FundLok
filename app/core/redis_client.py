"""Shared async Redis client for application code (cache, rate limits, locks).

Mirrors app/core/database.py: one client is built from `settings.REDIS_URL`
at import time, and `get_redis` hands it to routes as a FastAPI dependency.
Building the client opens no connection -- the pool connects lazily on the
first command, so importing this module never needs Redis to be running.

The same code runs against the local docker-compose `redis` service
(`redis://localhost:6379/0`) and against Upstash in deployed environments
(`rediss://default:<password>@<endpoint>.upstash.io:6379`); the `rediss`
scheme alone switches the pool to TLS. See .env.example.

Usage
-----
In a route::

    from redis.asyncio import Redis
    from app.core.redis_client import get_redis

    @router.get("/things/{thing_id}")
    async def read_thing(thing_id: str, redis: Redis = Depends(get_redis)):
        cached = await redis.get(f"thing:{thing_id}")

In a service or script, import `redis_client` directly.

Not for ARQ: the ARQ worker (app/scheduler/worker.py) keeps its own
connection, because ARQ stores pickled job payloads as bytes and this client
decodes every reply to `str`.
"""
from redis.asyncio import Redis

from app.core.config import settings


def create_redis(url: str) -> Redis:
    return Redis.from_url(
        url,
        # Replies come back as str, not bytes -- what cache/rate-limit code wants.
        decode_responses=True,
        # Fail a request after 5s instead of hanging it forever if Redis is
        # down or unreachable (redis-py's default is no timeout).
        socket_connect_timeout=5,
        socket_timeout=5,
        # PING a pooled connection that has sat idle for 30s before reusing
        # it, so a connection the server or a NAT dropped while idle is
        # replaced instead of failing the first command after a quiet spell.
        health_check_interval=30,
    )


redis_client = create_redis(settings.REDIS_URL)


async def get_redis() -> Redis:
    return redis_client
