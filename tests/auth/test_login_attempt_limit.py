"""Wrong passwords at /auth/login are limited per address (vbsec 2026-10-16).

/auth/login recorded nothing on a wrong password and never locked, and no
limiter existed anywhere in app/. The only brake was Turnstile, which is a
no-op wherever its secret is unset. These tests pin the limit (10 wrong
passwords per 15 minutes per address), that a locked address refuses even the
right password, that an unknown address locks exactly like a real one, that
parallel guesses cannot slip past, and that each miss on a real account is on
its owner's security screen.
"""
import asyncio
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import update

from app.auth import service
from app.auth.schemas import LoginRequest
from app.core.database import SessionLocal
from app.lending.models import AuditLog
from conftest import DEFAULT_PASSWORD as PASSWORD

LOGIN = "/auth/login"
LOCKED = {"detail": "Too many failed sign-in attempts. Try again in 15 minutes."}


async def _miss(client, email: str, times: int) -> None:
    for _ in range(times):
        resp = await client.post(LOGIN, json={"email": email, "password": "Wr0ngPassw0rd!"})
        assert resp.status_code == 401, resp.text


async def test_the_right_password_works_below_the_limit(client, make_user):
    user = await make_user()
    await _miss(client, user["email"], service.MAX_FAILED_LOGINS - 1)

    resp = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})

    assert resp.status_code == 200, resp.text


async def test_guessing_stops_at_the_limit(client, make_user):
    user = await make_user()
    await _miss(client, user["email"], service.MAX_FAILED_LOGINS)

    resp = await client.post(LOGIN, json={"email": user["email"], "password": "Wr0ngPassw0rd!"})

    assert resp.status_code == 429
    assert resp.json() == LOCKED


async def test_a_locked_address_refuses_even_the_right_password(client, make_user):
    # Otherwise the guesser would only need to wait out each 429.
    user = await make_user()
    await _miss(client, user["email"], service.MAX_FAILED_LOGINS)
    client.cookies.clear()

    resp = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})

    assert resp.status_code == 429
    assert not resp.cookies.get("access_token")


async def test_the_lock_ignores_the_case_of_the_address(client, make_user):
    user = await make_user()
    await _miss(client, user["email"].upper(), service.MAX_FAILED_LOGINS)

    resp = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})

    assert resp.status_code == 429


async def test_an_unknown_address_locks_like_a_real_one(client):
    # Locking only real accounts would tell a caller which addresses exist.
    await _miss(client, "nobody-here@example.com", service.MAX_FAILED_LOGINS)

    resp = await client.post(
        LOGIN, json={"email": "nobody-here@example.com", "password": "Wr0ngPassw0rd!"}
    )

    assert resp.status_code == 429
    assert resp.json() == LOCKED


async def test_one_address_locking_leaves_others_alone(client, make_user):
    victim = await make_user()
    bystander = await make_user()
    await _miss(client, victim["email"], service.MAX_FAILED_LOGINS)

    resp = await client.post(LOGIN, json={"email": bystander["email"], "password": PASSWORD})

    assert resp.status_code == 200, resp.text


async def test_the_lock_lifts_after_the_window(client, make_user, db_session):
    user = await make_user()
    await _miss(client, user["email"], service.MAX_FAILED_LOGINS)
    await db_session.execute(
        update(AuditLog)
        .where(AuditLog.action == service.SIGN_IN_FAILED)
        .values(created_at=AuditLog.created_at - service.FAILED_LOGIN_WINDOW - timedelta(seconds=1))
    )
    await db_session.commit()

    resp = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})

    assert resp.status_code == 200, resp.text


async def test_the_limit_holds_under_parallel_guesses(client, make_user):
    # Straight at the service, each guess in its own session (the test client
    # shares one session, so it cannot race). Without the per-address lock the
    # guesses all read the same count before any failure is written.
    user = await make_user()
    body = LoginRequest(email=user["email"], password="Wr0ngPassw0rd!")
    extra = 4

    async def one_guess() -> int:
        async with SessionLocal() as session:
            try:
                await service.login(session, body)
            except HTTPException as exc:
                return exc.status_code
            return 200

    codes = await asyncio.gather(*[one_guess() for _ in range(service.MAX_FAILED_LOGINS + extra)])

    assert codes.count(401) == service.MAX_FAILED_LOGINS, codes
    assert codes.count(429) == extra, codes


async def test_each_miss_is_on_the_owners_security_screen(client, make_user):
    user = await make_user()
    await _miss(client, user["email"], 2)

    events = await client.get("/auth/security-events", headers=user["headers"])

    failed = [e for e in events.json() if e["action"] == service.SIGN_IN_FAILED]
    assert len(failed) == 2
    assert all(e["severity"] == "critical" for e in failed)


async def test_an_unknown_address_is_not_stored_as_typed(client, db_session):
    await _miss(client, "typed-address@example.com", 1)

    rows = (
        await db_session.execute(
            AuditLog.__table__.select().where(AuditLog.action == service.SIGN_IN_FAILED)
        )
    ).all()

    assert len(rows) == 1
    assert rows[0].entity_id is None and rows[0].actor_id is None
    assert "typed-address" not in str(rows[0].after_state)
