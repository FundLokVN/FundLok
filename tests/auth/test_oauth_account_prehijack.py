"""Pre-registering someone's email must not survive their first social sign-in.

/auth/register creates a password account for any address without proving the
registrant owns it. The attack: register the victim's email first, then wait.
When the real owner signs in with Google, the OAuth lookup finds that account
by email. It used to be marked verified and returned untouched, so the
attacker's password, sessions and any 2FA they enrolled still worked on an
account the victim now uses (and puts KYC and money into).

These tests pin the fix: the first proven sign-in on an unverified account
removes every credential set before it, and leaves verified accounts alone.
Google's token verification is stubbed; the account handling is real.
"""
import pytest
from sqlalchemy import select

from app.lending.models import AuditLog
from app.oauth.schema import OAuthProvider
from app.oauth.service import GoogleOAuthProvider
from app.users.models import RefreshToken, TotpRecoveryCode, User, WebAuthnCredential
from conftest import DEFAULT_PASSWORD
from tests.auth.test_totp import enrol_legacy_unverified_totp


@pytest.fixture
def stub_google(monkeypatch):
    def _stub(email: str):
        monkeypatch.setattr(
            GoogleOAuthProvider,
            "verify_token",
            lambda self, token: {
                "sub": "google-victim-subject",
                "email": email,
                "email_verified": True,
                "name": "Real Owner",
            },
        )

    return _stub


async def _victim_signs_in_with_google(client):
    resp = await client.post(
        "/auth/oauth/login",
        json={"provider": OAuthProvider.GOOGLE.value, "token": "victims-google-token"},
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _password_login(client, email: str):
    client.cookies.clear()
    return await client.post("/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})


async def _load_user(db_session, user_id) -> User:
    db_session.expire_all()
    return (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()


async def test_attackers_password_stops_working(client, make_user, stub_google, db_session):
    # make_user registers WITHOUT verifying the email: the attacker's position.
    attacker = await make_user(email="victim@example.com")
    assert (await _password_login(client, attacker["email"])).status_code == 200

    stub_google(attacker["email"])
    await _victim_signs_in_with_google(client)

    resp = await _password_login(client, attacker["email"])
    assert resp.status_code == 401, "the pre-registered password must be gone"

    user = await _load_user(db_session, attacker["id"])
    assert user.email_verified is True
    assert user.password_hash is None


async def test_attackers_sessions_are_revoked(client, make_user, stub_google, db_session):
    attacker = await make_user()
    stub_google(attacker["email"])

    await _victim_signs_in_with_google(client)

    client.cookies.clear()
    resp = await client.post("/auth/refresh", json={"refresh_token": attacker["refresh_token"]})
    assert resp.status_code == 401, "the attacker's refresh session must not survive"

    # Exactly one live session remains: the one the victim just opened.
    live = (
        await db_session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == attacker["id"], RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    assert len(live) == 1


async def test_attackers_2fa_is_removed(client, make_user, stub_google, db_session):
    # Left in place, the attacker's authenticator would lock the owner out.
    attacker = await make_user()
    await enrol_legacy_unverified_totp(client, db_session, attacker)

    stub_google(attacker["email"])
    await _victim_signs_in_with_google(client)

    user = await _load_user(db_session, attacker["id"])
    assert user.totp_enabled is False
    assert user.totp_secret is None
    assert user.totp_confirmed_at is None
    codes = (
        await db_session.execute(
            select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == attacker["id"])
        )
    ).scalars().all()
    assert codes == []


async def test_attackers_passkeys_are_removed(client, make_user, stub_google, db_session):
    attacker = await make_user()
    db_session.add(
        WebAuthnCredential(
            user_id=attacker["id"],
            credential_id="attacker-passkey",
            public_key="attacker-public-key",
            name="Attacker's key",
        )
    )
    await db_session.commit()

    stub_google(attacker["email"])
    await _victim_signs_in_with_google(client)

    db_session.expire_all()
    keys = (
        await db_session.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.user_id == attacker["id"])
        )
    ).scalars().all()
    assert keys == []


async def test_reclaim_is_recorded_in_the_audit_log(client, make_user, stub_google, db_session):
    attacker = await make_user()
    stub_google(attacker["email"])

    await _victim_signs_in_with_google(client)

    rows = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == attacker["id"],
                AuditLog.action == "UNVERIFIED_ACCOUNT_RECLAIMED",
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].before_state == {"password": True, "totp": False}


async def test_verified_account_keeps_its_credentials(client, make_user, stub_google, db_session):
    # Someone who proved the email set this password: that is the owner, and
    # signing in with Google as well must not wipe it or end their sessions.
    owner = await make_user()
    user = await _load_user(db_session, owner["id"])
    user.email_verified = True
    await db_session.commit()

    stub_google(owner["email"])
    await _victim_signs_in_with_google(client)

    assert (await _password_login(client, owner["email"])).status_code == 200
    client.cookies.clear()
    resp = await client.post("/auth/refresh", json={"refresh_token": owner["refresh_token"]})
    assert resp.status_code == 200
    rows = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == owner["id"],
                AuditLog.action == "UNVERIFIED_ACCOUNT_RECLAIMED",
            )
        )
    ).scalars().all()
    assert rows == []


async def test_repeat_google_sign_in_does_not_revoke_the_owners_sessions(
    client, make_user, stub_google, db_session
):
    # The reclaim happens once. After it the account is verified, so the
    # owner's later Google sign-ins must not keep signing them out elsewhere.
    attacker = await make_user()
    stub_google(attacker["email"])

    first = await _victim_signs_in_with_google(client)
    owners_refresh = first.json()["refresh_token"]
    await _victim_signs_in_with_google(client)

    client.cookies.clear()
    resp = await client.post("/auth/refresh", json={"refresh_token": owners_refresh})
    assert resp.status_code == 200, resp.text
