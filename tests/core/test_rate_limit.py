"""SEC-08: per-IP rate limits (app/core/rate_limit.py).

Requests go through the real app with a chosen client address. Counters are
kept in memory here; the Redis path is checked against a fake, since tests
never reach a real Redis.
"""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core import config, rate_limit
from app.core.rate_limit import WINDOW_SECONDS, WindowCounter
from app.main import app

SENSITIVE_LIMIT = 3
GENERAL_LIMIT = 5


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(config.settings, "RATE_LIMIT_ENABLED", True)
    # A deployment that can see real visitor addresses; without one the
    # limiter stays off (see test_nothing_is_limited_without_real_addresses).
    monkeypatch.setattr(config.settings, "BACKEND_SECRET_KEY", "test-backend-secret")
    monkeypatch.setattr(config.settings, "RATE_LIMIT_SENSITIVE_PER_MINUTE", SENSITIVE_LIMIT)
    monkeypatch.setattr(config.settings, "RATE_LIMIT_PER_MINUTE", GENERAL_LIMIT)
    monkeypatch.setattr(rate_limit, "counter", WindowCounter(use_redis=False))


def _from(ip: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(ip, 40000)), base_url="http://testserver")


async def _login(client: AsyncClient):
    # A fresh unknown email each time, so the per-account throttle never fires.
    return await client.post(
        "/auth/login", json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com", "password": "Wr0ng-pass!"}
    )


async def test_sensitive_posts_are_limited_per_ip(limits):
    async with _from("203.0.113.10") as client:
        for _ in range(SENSITIVE_LIMIT):
            assert (await _login(client)).status_code != 429
        refused = await _login(client)

    assert refused.status_code == 429
    assert refused.headers["X-Error-Code"] == "RATE_LIMITED"
    assert 1 <= int(refused.headers["Retry-After"]) <= WINDOW_SECONDS
    assert refused.json() == {"detail": "Too many requests. Try again shortly."}


async def test_each_ip_has_its_own_budget(limits):
    async with _from("203.0.113.11") as first:
        for _ in range(SENSITIVE_LIMIT + 1):
            await _login(first)
    async with _from("203.0.113.12") as second:
        assert (await _login(second)).status_code != 429


async def test_everything_else_has_the_general_limit(limits):
    async with _from("203.0.113.13") as client:
        statuses = [(await client.get("/system/maintenance")).status_code for _ in range(GENERAL_LIMIT + 1)]

    assert statuses[:GENERAL_LIMIT] == [200] * GENERAL_LIMIT
    assert statuses[-1] == 429


async def test_using_up_the_sensitive_budget_leaves_the_general_one(limits):
    async with _from("203.0.113.14") as client:
        for _ in range(SENSITIVE_LIMIT + 1):
            await _login(client)
        assert (await client.get("/system/maintenance")).status_code == 200


async def test_health_is_never_limited(limits):
    async with _from("203.0.113.15") as client:
        statuses = {(await client.get("/health")).status_code for _ in range(GENERAL_LIMIT * 2)}
    assert statuses == {200}


async def test_nothing_is_limited_when_disabled(limits, monkeypatch):
    monkeypatch.setattr(config.settings, "RATE_LIMIT_ENABLED", False)
    async with _from("203.0.113.16") as client:
        statuses = {(await _login(client)).status_code for _ in range(SENSITIVE_LIMIT * 2)}
    assert 429 not in statuses


async def test_nothing_is_limited_without_real_addresses(limits, monkeypatch):
    """Enabled, but with neither BACKEND_SECRET_KEY nor TRUSTED_PROXY_HOPS
    set: behind the proxy every visitor has the proxy's address, so a per-IP
    limit would throttle them all as one caller. No limit beats that outage."""
    monkeypatch.setattr(config.settings, "BACKEND_SECRET_KEY", None)
    monkeypatch.setattr(config.settings, "TRUSTED_PROXY_HOPS", 0)
    async with _from("203.0.113.17") as client:
        statuses = {(await _login(client)).status_code for _ in range(SENSITIVE_LIMIT * 2)}
    assert 429 not in statuses


# --- the counter -------------------------------------------------------------


async def test_a_new_window_starts_from_zero():
    counter = WindowCounter(use_redis=False)
    assert (await counter.hit("k", now=0))[0] == 1
    assert (await counter.hit("k", now=59.9))[0] == 2
    assert (await counter.hit("k", now=60.0))[0] == 1


class _FakePipeline:
    def __init__(self, store: dict, expiries: dict):
        self.store, self.expiries, self.ops = store, expiries, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, key):
        self.ops.append(("incr", key))

    def expire(self, key, seconds):
        self.ops.append(("expire", key, seconds))

    async def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "incr":
                self.store[op[1]] = self.store.get(op[1], 0) + 1
                results.append(self.store[op[1]])
            else:
                self.expiries[op[1]] = op[2]
                results.append(True)
        return results


class _FakeRedis:
    def __init__(self):
        self.store, self.expiries = {}, {}

    def pipeline(self, transaction=True):
        return _FakePipeline(self.store, self.expiries)


async def test_counts_live_in_redis_with_an_expiry(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "redis_client", fake)
    counter = WindowCounter()

    await counter.hit("general:198.51.100.1", now=120)
    hits, _ = await counter.hit("general:198.51.100.1", now=130)

    assert hits == 2
    assert fake.store == {"ratelimit:general:198.51.100.1:120": 2}
    assert fake.expiries == {"ratelimit:general:198.51.100.1:120": WINDOW_SECONDS}


async def test_a_redis_outage_falls_back_to_memory_and_retries_later(monkeypatch):
    calls = []

    class _DownRedis:
        def pipeline(self, transaction=True):
            calls.append(transaction)
            raise RedisConnectionError("connection refused")

    monkeypatch.setattr(rate_limit, "redis_client", _DownRedis())
    counter = WindowCounter()

    counts = [(await counter.hit("k", now=t))[0] for t in (0, 1, 2)]

    assert counts == [1, 2, 3]  # still counted, in memory
    assert len(calls) == 1  # Redis not retried inside the back-off
    await counter.hit("k", now=rate_limit.REDIS_RETRY_SECONDS + 1)
    assert len(calls) == 2
