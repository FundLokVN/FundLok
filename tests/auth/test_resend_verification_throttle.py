"""POST /auth/resend-verification is limited per account (vbsec 2026-10-16 [2]).

The endpoint is public, had no captcha and no limit, and every call queued a
verification email. Registering a victim's address once (one captcha) was
enough to mail-bomb that inbox without limit. These tests pin the captcha, the
cooldown, the daily ceiling, that parallel calls cannot slip past either, and
that the endpoint answers the same way whatever it did.
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select, update

from app.auth import service
from app.auth.schemas import ResendVerificationRequest
from app.core.config import settings
from app.core.database import SessionLocal
from app.lending.models import AuditLog
from app.users.models import User
from app.utils import captcha

RESEND = "/auth/resend-verification"


@pytest.fixture
def sent(monkeypatch):
    """Records every verification email queued, by recipient."""
    outbox: list[str] = []
    monkeypatch.setattr(
        "app.auth.service.send_verification_email",
        lambda *a, to_email, token, **kw: outbox.append(to_email),
    )
    return outbox


async def _register(client, email: str, outbox: list[str]) -> None:
    resp = await client.post(
        "/auth/register",
        json={"email": email, "password": "Str0ngPassw0rd!1", "full_name": "Test User"},
    )
    assert resp.status_code == 201, resp.text
    # Registration mails the first link itself; only resends are under test.
    outbox.clear()


async def _age_sends(db_session, email: str, by: timedelta) -> None:
    """Move this account's recorded sends back in time."""
    user_id = (await db_session.execute(select(User.id).where(User.email == email))).scalar_one()
    await db_session.execute(
        update(AuditLog)
        .where(AuditLog.entity_id == user_id, AuditLog.action == service.VERIFICATION_EMAIL_SENT)
        .values(created_at=AuditLog.created_at - by)
    )
    await db_session.commit()


async def test_resend_sends_once_then_waits_out_the_cooldown(client, db_session, sent):
    await _register(client, "cooldown@example.com", sent)

    first = await client.post(RESEND, json={"email": "cooldown@example.com"})
    again = await client.post(RESEND, json={"email": "cooldown@example.com"})

    assert first.status_code == 200 and again.status_code == 200
    assert sent == ["cooldown@example.com"]

    await _age_sends(db_session, "cooldown@example.com", service.VERIFICATION_RESEND_COOLDOWN)
    later = await client.post(RESEND, json={"email": "cooldown@example.com"})

    assert later.status_code == 200
    assert sent == ["cooldown@example.com", "cooldown@example.com"]


async def test_resend_stops_at_the_daily_ceiling(client, db_session, sent):
    await _register(client, "ceiling@example.com", sent)
    for _ in range(service.MAX_VERIFICATION_EMAILS_PER_DAY):
        await client.post(RESEND, json={"email": "ceiling@example.com"})
        # Past the cooldown, still inside the day.
        await _age_sends(db_session, "ceiling@example.com", service.VERIFICATION_RESEND_COOLDOWN)
    assert len(sent) == service.MAX_VERIFICATION_EMAILS_PER_DAY

    resp = await client.post(RESEND, json={"email": "ceiling@example.com"})

    assert resp.status_code == 200
    assert len(sent) == service.MAX_VERIFICATION_EMAILS_PER_DAY


async def test_resend_ceiling_is_a_rolling_day(client, db_session, sent):
    await _register(client, "rolling@example.com", sent)
    for _ in range(service.MAX_VERIFICATION_EMAILS_PER_DAY):
        await client.post(RESEND, json={"email": "rolling@example.com"})
        await _age_sends(db_session, "rolling@example.com", service.VERIFICATION_RESEND_COOLDOWN)

    await _age_sends(db_session, "rolling@example.com", timedelta(days=1))
    await client.post(RESEND, json={"email": "rolling@example.com"})

    assert len(sent) == service.MAX_VERIFICATION_EMAILS_PER_DAY + 1


async def test_resend_holds_the_cooldown_under_parallel_calls(client, sent):
    # Straight at the service, each call in its own session: the test client
    # shares one session, so it cannot race. Without the per-account lock all
    # of them read "nothing sent yet" and each sends.
    await _register(client, "burst@example.com", sent)
    body = ResendVerificationRequest(email="burst@example.com")

    async def one_call() -> None:
        async with SessionLocal() as session:
            tasks = BackgroundTasks()
            await service.resend_verification(session, body, tasks)
            await tasks()

    await asyncio.gather(*[one_call() for _ in range(8)])

    assert sent == ["burst@example.com"]


async def test_resend_answers_the_same_whatever_happened(client, db_session, sent):
    await _register(client, "verified@example.com", sent)
    row = (
        await db_session.execute(select(User).where(User.email == "verified@example.com"))
    ).scalar_one()
    row.email_verified = True
    await db_session.commit()
    await _register(client, "pending@example.com", sent)

    responses = [
        await client.post(RESEND, json={"email": "nobody@example.com"}),  # unknown
        await client.post(RESEND, json={"email": "verified@example.com"}),  # verified
        await client.post(RESEND, json={"email": "pending@example.com"}),  # sent
        await client.post(RESEND, json={"email": "pending@example.com"}),  # throttled
    ]

    assert [r.status_code for r in responses] == [200] * 4
    assert all(r.json() == service.RESEND_VERIFICATION_ACK for r in responses)
    assert sent == ["pending@example.com"]


async def test_resend_needs_the_captcha_when_it_is_configured(client, monkeypatch, sent):
    await _register(client, "captcha@example.com", sent)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")

    resp = await client.post(RESEND, json={"email": "captcha@example.com"})

    assert resp.status_code == 400
    assert resp.json() == {"detail": "Turnstile token is missing"}
    assert sent == []


async def test_resend_passes_the_token_to_the_captcha_check(client, monkeypatch, sent):
    await _register(client, "token@example.com", sent)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")
    seen: list[str] = []

    class _Siteverify:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data):
            seen.append(data["response"])
            return SimpleNamespace(json=lambda: {"success": True}, status_code=200)

    # Only the captcha module's view of httpx, so nothing else is affected.
    monkeypatch.setattr(
        captcha, "httpx", SimpleNamespace(AsyncClient=_Siteverify, RequestError=httpx.RequestError)
    )

    resp = await client.post(
        RESEND, json={"email": "token@example.com", "turnstile_token": "tok-123"}
    )

    assert resp.status_code == 200, resp.text
    assert seen == ["tok-123"]
    assert sent == ["token@example.com"]


async def test_a_send_is_not_on_the_owners_security_history(client, make_user, sent):
    # The caller is anonymous; a resend is not something the owner did.
    user = await make_user()
    sent.clear()
    await client.post(RESEND, json={"email": user["email"]})
    assert sent == [user["email"]]

    events = await client.get("/auth/security-events", headers=user["headers"])

    assert service.VERIFICATION_EMAIL_SENT not in [e["action"] for e in events.json()]
