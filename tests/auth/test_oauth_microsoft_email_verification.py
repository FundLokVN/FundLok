"""Microsoft sign-in must not take over an account by an unverified email.

nOAuth: with the "common" tenant any Entra tenant can sign in, and its admin
can set a user's `mail` (sent as `email`) or UPN (`preferred_username`) to any
address. map_claims took `email`, fell back to `preferred_username`, and the
account lookup by email returned whatever account held that address,
password-less admins included. These tests pin the fix: only `email` backed
by Microsoft's `xms_edov` claim counts, and no provider can sign anyone into
(or create) an account on an address it has not verified.

Microsoft's token verification is stubbed; the account handling is real.
"""
import pytest
from sqlalchemy import select

from app.oauth.schema import OAuthProvider
from app.oauth.service import MicrosoftOAuthProvider
from app.users.models import User


@pytest.fixture
def stub_microsoft(monkeypatch):
    def _stub(**claims):
        token_claims = {"sub": "entra-subject", "name": "Someone", **claims}
        monkeypatch.setattr(MicrosoftOAuthProvider, "verify_token", lambda self, token: token_claims)

    return _stub


async def _microsoft_login(client):
    client.cookies.clear()
    return await client.post(
        "/auth/oauth/login",
        json={"provider": OAuthProvider.MICROSOFT.value, "token": "entra-id-token"},
    )


async def _users_with_email(db_session, email) -> list:
    db_session.expire_all()
    return (await db_session.execute(select(User).where(User.email == email))).scalars().all()


async def test_an_unverified_email_cannot_sign_into_an_existing_account(
    client, make_user, stub_microsoft
):
    victim = await make_user(email="victim@example.com")
    stub_microsoft(email=victim["email"])  # attacker tenant set mail; no xms_edov

    resp = await _microsoft_login(client)

    assert resp.status_code == 401, resp.text
    assert "access_token" not in resp.cookies


async def test_an_upn_alone_is_not_an_email(client, make_user, stub_microsoft):
    victim = await make_user(email="victim@example.com")
    stub_microsoft(preferred_username=victim["email"], xms_edov=True)

    resp = await _microsoft_login(client)

    assert resp.status_code == 400, resp.text
    assert "access_token" not in resp.cookies


async def test_an_unverified_email_does_not_create_an_account(client, stub_microsoft, db_session):
    stub_microsoft(email="nobody-yet@example.com", xms_edov=False)

    resp = await _microsoft_login(client)

    assert resp.status_code == 401, resp.text
    assert await _users_with_email(db_session, "nobody-yet@example.com") == []


async def test_a_verified_email_signs_in_and_creates_the_account(client, stub_microsoft, db_session):
    stub_microsoft(email="owner@example.com", xms_edov=True)

    resp = await _microsoft_login(client)

    assert resp.status_code == 200, resp.text
    [user] = await _users_with_email(db_session, "owner@example.com")
    assert user.email_verified is True


async def test_a_verified_email_still_reclaims_an_unverified_squat(
    client, make_user, stub_microsoft, db_session
):
    squatter = await make_user(email="owner@example.com")
    stub_microsoft(email=squatter["email"], xms_edov="true")

    resp = await _microsoft_login(client)

    assert resp.status_code == 200, resp.text
    [user] = await _users_with_email(db_session, squatter["email"])
    assert user.email_verified is True
    assert user.password_hash is None
