"""A password reset locks everyone else out, and its link works once.

reset_password used to swap the hash and nothing else:
  * every existing session kept working, so whoever was already in the account
    stayed in after the owner reset the password to get rid of them;
  * the emailed token was a plain one-hour JWT, so the same link (from the
    inbox, a forwarded mail, browser history) could reset the password again
    within the hour, including after the owner had already used it.

These tests pin the fix: all refresh sessions are revoked, and the token is
tied to the password it was issued for, so any change to the password (this
reset or another) spends every outstanding link.
"""
import pytest
import jwt
from sqlalchemy import select

from app.core.config import settings
from app.lending.models import AuditLog
from app.users.models import RefreshToken
from app.utils.jwt import create_password_reset_token
from conftest import DEFAULT_PASSWORD, auth_headers

NEW_PASSWORD = "Brand-New-Passw0rd!"
OTHER_PASSWORD = "Another-Passw0rd!2"


@pytest.fixture
def reset_links(monkeypatch):
    """Captures the token from each reset email instead of sending it."""
    links: list[str] = []
    monkeypatch.setattr(
        "app.auth.service.send_password_reset_email",
        lambda to_email, token: links.append(token),
    )
    return links


async def _request_link(client, email, links) -> str:
    resp = await client.post("/auth/forgot-password", json={"email": email})
    assert resp.status_code == 200, resp.text
    return links[-1]


async def _reset(client, token, password=NEW_PASSWORD):
    return await client.post("/auth/reset-password", json={"token": token, "new_password": password})


async def _login(client, email, password):
    client.cookies.clear()
    return await client.post("/auth/login", json={"email": email, "password": password})


# --- sessions ------------------------------------------------------------------


async def test_a_reset_signs_out_every_existing_session(client, make_user, reset_links, db_session):
    user = await make_user()  # signs in once: the "intruder's" session
    token = await _request_link(client, user["email"], reset_links)

    assert (await _reset(client, token)).status_code == 200

    client.cookies.clear()
    resp = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert resp.status_code == 401, "the old session must not survive the reset"
    live = (
        await db_session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user["id"], RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    assert live == []


async def test_signing_in_with_the_new_password_still_works(client, make_user, reset_links):
    user = await make_user()
    await _reset(client, await _request_link(client, user["email"], reset_links))

    assert (await _login(client, user["email"], NEW_PASSWORD)).status_code == 200
    assert (await _login(client, user["email"], DEFAULT_PASSWORD)).status_code == 401


async def test_the_reset_is_on_the_security_screen(client, make_user, reset_links):
    user = await make_user()
    await _reset(client, await _request_link(client, user["email"], reset_links))
    fresh = await _login(client, user["email"], NEW_PASSWORD)
    headers = {"Authorization": f"Bearer {fresh.cookies.get('access_token')}"}

    events = (await client.get("/auth/security-events", headers=headers)).json()

    [event] = [e for e in events if e["action"] == "PASSWORD_RESET"]
    assert event["severity"] == "warning"


async def test_the_reset_audit_records_how_many_sessions_ended(
    client, make_user, reset_links, db_session
):
    user = await make_user()
    await _login(client, user["email"], DEFAULT_PASSWORD)  # a second session
    await _reset(client, await _request_link(client, user["email"], reset_links))

    row = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "PASSWORD_RESET"))
    ).scalar_one()
    assert str(row.entity_id) == user["id"]
    assert row.after_state == {"sessions_revoked": 2}


# --- the link is single-use -------------------------------------------------------


async def test_the_same_link_cannot_be_used_twice(client, make_user, reset_links):
    user = await make_user()
    token = await _request_link(client, user["email"], reset_links)
    assert (await _reset(client, token)).status_code == 200

    resp = await _reset(client, token, OTHER_PASSWORD)

    assert resp.status_code == 400
    assert resp.json() == {"detail": "Invalid or expired reset token"}
    # The first reset stands.
    assert (await _login(client, user["email"], NEW_PASSWORD)).status_code == 200


async def test_using_one_link_spends_every_other_outstanding_link(client, make_user, reset_links):
    user = await make_user()
    first = await _request_link(client, user["email"], reset_links)
    second = await _request_link(client, user["email"], reset_links)

    assert (await _reset(client, second)).status_code == 200
    assert (await _reset(client, first, OTHER_PASSWORD)).status_code == 400


async def test_changing_the_password_another_way_spends_the_link(client, make_user, reset_links):
    user = await make_user()
    token = await _request_link(client, user["email"], reset_links)

    changed = await client.post(
        "/users/me/password/change",
        json={"current_password": DEFAULT_PASSWORD, "new_password": OTHER_PASSWORD},
        headers=auth_headers(user),
    )
    assert changed.status_code == 200, changed.text

    assert (await _reset(client, token)).status_code == 400


async def test_a_token_without_a_password_fingerprint_is_refused(client, make_user):
    # The old token shape, e.g. a link emailed before this check existed.
    user = await make_user()

    resp = await _reset(client, create_password_reset_token(user["id"]))

    assert resp.status_code == 400
    assert (await _login(client, user["email"], DEFAULT_PASSWORD)).status_code == 200


async def test_the_token_does_not_carry_the_password_hash(client, make_user, reset_links, db_session):
    from app.users.models import User

    user = await make_user()
    token = await _request_link(client, user["email"], reset_links)
    claims = jwt.decode(token, options={"verify_signature": False})
    row = (await db_session.execute(select(User).where(User.email == user["email"]))).scalar_one()

    assert claims["purpose"] == "password_reset"
    assert len(claims["pwv"]) == 32
    # Readable by whoever holds the link, so it must reveal nothing usable.
    assert claims["pwv"] not in row.password_hash
    assert settings.SECRET_KEY not in token
