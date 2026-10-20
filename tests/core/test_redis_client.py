"""Shared Redis client (app/core/redis_client.py).

These check how the client is configured, not a live round trip: building a
redis.asyncio client opens no connection, and conftest.py's rule is that
tests never hit a real external dependency.
"""
from urllib.parse import urlparse

from redis.asyncio.connection import Connection, SSLConnection

from app.core.config import settings
from app.core.redis_client import create_redis, get_redis, redis_client


def test_redis_client_is_built_from_settings_redis_url():
    expected = urlparse(settings.REDIS_URL)
    kwargs = redis_client.connection_pool.connection_kwargs
    assert kwargs["host"] == expected.hostname
    assert kwargs["port"] == (expected.port or 6379)
    assert kwargs["db"] == int(expected.path.lstrip("/") or 0)


def test_local_redis_url_connects_without_tls():
    client = create_redis("redis://localhost:6379/0")
    assert client.connection_pool.connection_class is Connection


def test_upstash_rediss_url_connects_over_tls():
    client = create_redis("rediss://default:not-a-real-password@example-123.upstash.io:6379")
    pool = client.connection_pool
    assert pool.connection_class is SSLConnection
    assert pool.connection_kwargs["host"] == "example-123.upstash.io"
    assert pool.connection_kwargs["username"] == "default"
    assert pool.connection_kwargs["password"] == "not-a-real-password"


def test_replies_are_decoded_to_str():
    assert redis_client.connection_pool.connection_kwargs["decode_responses"] is True


def test_unreachable_redis_fails_fast_instead_of_hanging():
    kwargs = redis_client.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == 5
    assert kwargs["socket_timeout"] == 5


async def test_get_redis_dependency_returns_the_shared_client():
    assert await get_redis() is redis_client
