"""Google sign-in passes the same gates as the password form.

The OAuth path issued a session as soon as the provider token checked out. The
password path (app/auth/service.login) refuses a SUSPENDED account and, for an
account with 2FA on, issues only a challenge redeemed at /auth/login/2fa. The
OAuth path had neither, so 2FA protected the password form and nothing else,
and a suspended user could sign straight back in with Google.
"""
import pyotp
import pytest
from sqlalchemy import select

from app.oauth.schema import OAuthProvider
from app.oauth.service import GoogleOAuthProvider
from app.users.models import USER_STATUS_SUSPENDED, RefreshToken, User
from tests.auth.test_totp import enrol, enrol_legacy_unverified_totp


@pytest.fixture
def stub_google(monkeypatch):
    def _stub(email: str):
        monkeypatch.setattr(
            GoogleOAuthProvider,
            "verify_token",
            lambda self, token: {
                "sub": "google-subject",
                "email": email,
                "email_verified": True,
                "name": "OAuth User",
            },
        )

    return _stub


async def _google_login(client):
    client.cookies.clear()
    return await client.post(
        "/auth/oauth/login",
        json={"provider": OAuthProvider.GOOGLE.value, "token": "google-token"},
    )


async def _set(db_session, user_id, **fields) -> None:
    user = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    for name, value in fields.items():
        setattr(user, name, value)
    await db_session.commit()


async def _session_count(db_session, user_id) -> int:
    db_session.expire_all()
    rows = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_id)
    )
    return len(rows.scalars().all())


# --- 2FA ---------------------------------------------------------------------


@pytest.fixture
async def two_factor_user(client, make_user, db_session, stub_google):
    """A verified account with 2FA on: its credentials are the owner's own."""
    user = await make_user()
    secret, codes = await enrol(client, user)
    await _set(db_session, user["id"], email_verified=True)
    stub_google(user["email"])
    return {**user, "secret": secret, "codes": codes}


async def test_google_sign_in_on_a_2fa_account_returns_only_a_challenge(
    client, two_factor_user, db_session
):
    before = await _session_count(db_session, two_factor_user["id"])

    resp = await _google_login(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totp_required"] is True
    assert body["challenge_token"]
    assert "access_token" not in body and "refresh_token" not in body
    assert "access_token" not in resp.cookies and "refresh_token" not in resp.cookies
    assert await _session_count(db_session, two_factor_user["id"]) == before


async def test_the_oauth_challenge_is_not_a_session(client, two_factor_user):
    challenge = (await _google_login(client)).json()["challenge_token"]

    resp = await client.get("/users/me", headers={"Authorization": f"Bearer {challenge}"})

    assert resp.status_code == 401


async def test_the_oauth_challenge_completes_with_a_valid_code(client, two_factor_user):
    challenge = (await _google_login(client)).json()["challenge_token"]

    resp = await client.post(
        "/auth/login/2fa",
        json={
            "challenge_token": challenge,
            "code": pyotp.TOTP(two_factor_user["secret"]).now(),
            "remember_me": True,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.cookies.get("access_token")


async def test_the_oauth_challenge_rejects_a_wrong_code(client, two_factor_user):
    challenge = (await _google_login(client)).json()["challenge_token"]

    resp = await client.post(
        "/auth/login/2fa", json={"challenge_token": challenge, "code": "000000"}
    )

    assert resp.status_code == 401
    assert "access_token" not in resp.cookies


async def test_the_oauth_challenge_accepts_a_recovery_code(client, two_factor_user):
    challenge = (await _google_login(client)).json()["challenge_token"]

    resp = await client.post(
        "/auth/login/2fa",
        json={"challenge_token": challenge, "code": two_factor_user["codes"][0]},
    )

    assert resp.status_code == 200, resp.text


async def test_account_without_2fa_still_signs_straight_in(
    client, make_user, db_session, stub_google
):
    user = await make_user()
    await _set(db_session, user["id"], email_verified=True)
    stub_google(user["email"])

    resp = await _google_login(client)

    assert resp.status_code == 200, resp.text
    assert "totp_required" not in resp.json()
    assert resp.cookies.get("access_token")


async def test_a_strangers_2fa_on_an_unverified_account_does_not_challenge_the_owner(
    client, make_user, stub_google, db_session
):
    # The reclaim runs before the 2FA gate: a squatter's authenticator is
    # removed, so the owner who just proved the email is not locked out by it.
    squatter = await make_user()
    await enrol_legacy_unverified_totp(client, db_session, squatter)
    stub_google(squatter["email"])

    resp = await _google_login(client)

    assert resp.status_code == 200, resp.text
    assert "totp_required" not in resp.json()
    assert resp.cookies.get("access_token")


# --- suspension --------------------------------------------------------------


async def test_suspended_account_cannot_sign_in_with_google(
    client, make_user, db_session, stub_google
):
    user = await make_user()
    await _set(db_session, user["id"], email_verified=True, status=USER_STATUS_SUSPENDED)
    stub_google(user["email"])
    before = await _session_count(db_session, user["id"])

    resp = await _google_login(client)

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "This account has been suspended"}
    assert "access_token" not in resp.cookies
    assert await _session_count(db_session, user["id"]) == before


async def test_suspended_2fa_account_gets_no_challenge_either(
    client, two_factor_user, db_session
):
    # Suspension is checked first, so a suspended account can't even obtain
    # a challenge token to try codes against.
    await _set(db_session, two_factor_user["id"], status=USER_STATUS_SUSPENDED)

    resp = await _google_login(client)

    assert resp.status_code == 403
    assert "challenge_token" not in resp.text
