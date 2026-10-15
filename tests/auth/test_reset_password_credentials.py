"""A password reset must not leave a stranger's passkey (or 2FA) behind.

/auth/register signs in an account for any address without proving the
registrant owns it. The attack: register the victim's email, add a passkey,
wait. The real owner is told "you already have an account, reset your
password", does so, verifies, and starts using the account (KYC, bank, money).
The reset swapped the password and ended sessions but left the passkey, and a
passkey sign-in never asks for the password: the stranger walked back in.

These tests pin the fix:
  * an unverified account cannot add a passkey at all;
  * a reset removes every passkey, and proves the address (email_verified);
  * on an account nobody had verified, a reset also clears 2FA, which was
    enrolled by an unproven party; on a verified account 2FA is kept.
"""
import pytest
from sqlalchemy import select

from app.lending.models import AuditLog
from app.users.models import TotpRecoveryCode, User, WebAuthnChallenge, WebAuthnCredential
from tests.auth.test_totp import enrol

NEW_PASSWORD = "Brand-New-Passw0rd!"


@pytest.fixture
def reset_links(monkeypatch):
    """Captures the token from each reset email instead of sending it."""
    links: list[str] = []
    monkeypatch.setattr(
        "app.auth.service.send_password_reset_email",
        lambda to_email, token: links.append(token),
    )
    return links


async def _reset(client, email, links):
    resp = await client.post("/auth/forgot-password", json={"email": email})
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        "/auth/reset-password", json={"token": links[-1], "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 200, resp.text


async def _load_user(db_session, user_id) -> User:
    db_session.expire_all()
    return (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()


async def _mark_verified(db_session, user_id) -> None:
    user = await _load_user(db_session, user_id)
    user.email_verified = True
    await db_session.commit()


async def _plant_passkey(db_session, user_id) -> None:
    db_session.add(
        WebAuthnCredential(
            user_id=user_id,
            credential_id=f"planted-{user_id}",
            public_key="planted-public-key",
            name="Stranger's key",
        )
    )
    await db_session.commit()


async def _passkeys(db_session, user_id) -> list:
    db_session.expire_all()
    return (
        await db_session.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.user_id == user_id)
        )
    ).scalars().all()


# --- enrolment ------------------------------------------------------------------


async def test_an_unverified_account_cannot_start_adding_a_passkey(client, make_user):
    user = await make_user()  # registered, signed in, never verified

    resp = await client.post("/auth/passkeys/register/options", headers=user["headers"])

    assert resp.status_code == 403, resp.text


async def test_an_unverified_account_cannot_finish_adding_a_passkey(client, make_user):
    user = await make_user()

    resp = await client.post(
        "/auth/passkeys/register/verify",
        json={"credential": {"id": "x", "response": {}}},
        headers=user["headers"],
    )

    assert resp.status_code == 403, resp.text


async def test_a_verified_account_can_start_adding_a_passkey(client, make_user, db_session):
    user = await make_user()
    await _mark_verified(db_session, user["id"])

    resp = await client.post("/auth/passkeys/register/options", headers=user["headers"])

    assert resp.status_code == 200, resp.text
    assert "challenge" in resp.json()


# --- reset ----------------------------------------------------------------------


async def test_a_reset_removes_a_planted_passkey(client, make_user, reset_links, db_session):
    squatter = await make_user(email="victim@example.com")
    await _plant_passkey(db_session, squatter["id"])

    await _reset(client, squatter["email"], reset_links)

    assert await _passkeys(db_session, squatter["id"]) == []


async def test_a_reset_removes_passkeys_on_a_verified_account_too(
    client, make_user, reset_links, db_session
):
    # The owner verified the address, but a passkey was added from a session
    # that was not theirs. The reset is how they get rid of it.
    user = await make_user()
    await _mark_verified(db_session, user["id"])
    await _plant_passkey(db_session, user["id"])

    await _reset(client, user["email"], reset_links)

    assert await _passkeys(db_session, user["id"]) == []


async def test_a_reset_drops_pending_passkey_ceremonies(client, make_user, reset_links, db_session):
    user = await make_user()
    await _mark_verified(db_session, user["id"])
    resp = await client.post("/auth/passkeys/register/options", headers=user["headers"])
    assert resp.status_code == 200, resp.text

    await _reset(client, user["email"], reset_links)

    db_session.expire_all()
    challenges = (
        await db_session.execute(
            select(WebAuthnChallenge).where(WebAuthnChallenge.user_id == user["id"])
        )
    ).scalars().all()
    assert challenges == []


async def test_a_reset_proves_the_address(client, make_user, reset_links, db_session):
    user = await make_user()

    await _reset(client, user["email"], reset_links)

    assert (await _load_user(db_session, user["id"])).email_verified is True


async def test_a_reset_clears_2fa_enrolled_before_anyone_verified(
    client, make_user, reset_links, db_session
):
    # Left in place, the squatter's authenticator would lock the owner out.
    squatter = await make_user()
    await enrol(client, squatter)

    await _reset(client, squatter["email"], reset_links)

    user = await _load_user(db_session, squatter["id"])
    assert user.totp_enabled is False
    assert user.totp_secret is None
    assert user.totp_confirmed_at is None
    codes = (
        await db_session.execute(
            select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == squatter["id"])
        )
    ).scalars().all()
    assert codes == []


async def test_a_reset_keeps_2fa_on_a_verified_account(client, make_user, reset_links, db_session):
    # Here 2FA is what stops a reset link alone from taking the account over.
    user = await make_user()
    await _mark_verified(db_session, user["id"])
    await enrol(client, user)

    await _reset(client, user["email"], reset_links)

    assert (await _load_user(db_session, user["id"])).totp_enabled is True


async def test_the_reset_audit_row_records_what_was_removed(
    client, make_user, reset_links, db_session
):
    user = await make_user()
    await _plant_passkey(db_session, user["id"])
    await enrol(client, user)

    await _reset(client, user["email"], reset_links)

    db_session.expire_all()
    row = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "PASSWORD_RESET"))
    ).scalar_one()
    assert str(row.entity_id) == user["id"]
    assert row.after_state["passkeys_removed"] == 1
    assert row.after_state["totp_cleared"] is True
