"""Wrong 2FA codes at sign-in are limited per account.

/auth/login/2fa checked any number of codes. A challenge lives five minutes
and a fresh one is a password login away, so whoever had the password could
guess codes until one landed: the second factor fell to patience. These tests
pin the limit (5 wrong codes per 15 minutes per account, counted across
challenges), that a locked account refuses even the right code, and that each
miss is on the owner's security screen.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.auth.service import MAX_SECOND_FACTOR_FAILURES
from app.lending.models import AuditLog
from conftest import DEFAULT_PASSWORD as PASSWORD
from conftest import audit_rows_backdatable
from tests.auth.test_totp import code_for, enrol


def _wrong(secret: str) -> str:
    return f"{(int(code_for(secret)) + 500_000) % 1_000_000:06d}"


async def _challenge(client, user) -> str:
    client.cookies.clear()
    login = await client.post("/auth/login", json={"email": user["email"], "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()["challenge_token"]


async def _second_step(client, challenge: str, code: str):
    return await client.post("/auth/login/2fa", json={"challenge_token": challenge, "code": code})


async def _miss(client, user, secret, times: int) -> None:
    for _ in range(times):
        resp = await _second_step(client, await _challenge(client, user), _wrong(secret))
        assert resp.status_code == 401, resp.text


async def test_the_right_code_works_below_the_limit(client, make_user):
    user = await make_user()
    secret, _ = await enrol(client, user)
    await _miss(client, user, secret, MAX_SECOND_FACTOR_FAILURES - 1)

    resp = await _second_step(client, await _challenge(client, user), code_for(secret))

    assert resp.status_code == 200, resp.text


async def test_guessing_stops_at_the_limit_even_across_fresh_challenges(client, make_user):
    user = await make_user()
    secret, _ = await enrol(client, user)
    await _miss(client, user, secret, MAX_SECOND_FACTOR_FAILURES)

    resp = await _second_step(client, await _challenge(client, user), _wrong(secret))

    assert resp.status_code == 429, resp.text


async def test_a_locked_account_refuses_even_the_right_code(client, make_user):
    # Otherwise the guesser would only need to wait out each 429.
    user = await make_user()
    secret, _ = await enrol(client, user)
    await _miss(client, user, secret, MAX_SECOND_FACTOR_FAILURES)

    resp = await _second_step(client, await _challenge(client, user), code_for(secret))

    assert resp.status_code == 429, resp.text
    assert not resp.cookies.get("access_token")


async def test_a_recovery_code_is_limited_too(client, make_user):
    user = await make_user()
    secret, codes = await enrol(client, user)
    await _miss(client, user, secret, MAX_SECOND_FACTOR_FAILURES)

    resp = await _second_step(client, await _challenge(client, user), codes[0])

    assert resp.status_code == 429, resp.text


async def test_old_misses_age_out(client, make_user, db_session):
    user = await make_user()
    secret, _ = await enrol(client, user)
    await _miss(client, user, secret, MAX_SECOND_FACTOR_FAILURES)

    async with audit_rows_backdatable(db_session):
        await db_session.execute(
            update(AuditLog)
            .where(AuditLog.action == "TWO_FACTOR_FAILED")
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=16))
        )
    await db_session.commit()

    resp = await _second_step(client, await _challenge(client, user), code_for(secret))
    assert resp.status_code == 200, resp.text


async def test_other_accounts_are_not_affected(client, make_user):
    target, bystander = await make_user(), await make_user()
    target_secret, _ = await enrol(client, target)
    bystander_secret, _ = await enrol(client, bystander)
    await _miss(client, target, target_secret, MAX_SECOND_FACTOR_FAILURES)

    resp = await _second_step(client, await _challenge(client, bystander), code_for(bystander_secret))

    assert resp.status_code == 200, resp.text


async def test_a_miss_is_on_the_owners_security_screen(client, make_user, db_session):
    user = await make_user()
    secret, _ = await enrol(client, user)
    await _miss(client, user, secret, 1)

    rows = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "TWO_FACTOR_FAILED"))
    ).scalars().all()
    assert [str(r.entity_id) for r in rows] == [user["id"]]

    client.cookies.clear()
    events = await client.get("/auth/security-events", headers=user["headers"])
    assert events.status_code == 200, events.text
    [event] = [e for e in events.json() if e["action"] == "TWO_FACTOR_FAILED"]
    assert event["severity"] == "critical"


async def test_the_limit_holds_under_parallel_guesses(client, make_user):
    # vbsec 2026-10-16: the count was read before the code was checked and
    # written after, with nothing in between, so a burst of parallel requests
    # all read a count under the limit and each got a guess. Straight at the
    # service, each guess in its own session (the test client shares one
    # session, so it cannot race).
    import asyncio

    from fastapi import HTTPException

    from app.auth import service
    from app.core.database import SessionLocal

    user = await make_user()
    secret, _ = await enrol(client, user)
    challenge = await _challenge(client, user)
    extra = 3

    async def one_guess() -> int:
        async with SessionLocal() as session:
            try:
                await service.complete_totp_login(session, challenge, _wrong(secret))
            except HTTPException as exc:
                return exc.status_code
            return 200

    codes = await asyncio.gather(*[one_guess() for _ in range(MAX_SECOND_FACTOR_FAILURES + extra)])

    assert codes.count(401) == MAX_SECOND_FACTOR_FAILURES, codes
    assert codes.count(429) == extra, codes
