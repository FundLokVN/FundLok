"""A Google token only signs you in if it was issued to FundLok.

Google sign-in used to call userinfo with the access token and look the user
up by the email it returned. userinfo answers for any valid Google access
token, whichever app it belongs to, so a token a victim granted to an
attacker's own "Sign in with Google" site could be replayed at
/auth/oauth/login and exchanged for a full session on the victim's account.

These tests pin the fix: an access token must be reported by tokeninfo as
issued to our client ID (aud and azp) with a verified email, and a JWT is
routed through the signed ID-token path. Google's HTTP endpoints are stubbed.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.oauth.schema import OAuthProvider
from app.oauth.service import GoogleOAuthProvider
from app.users.models import RefreshToken

OUR_CLIENT_ID = "fundlok-client.apps.googleusercontent.com"
ATTACKER_CLIENT_ID = "attacker-client.apps.googleusercontent.com"


def _tokeninfo(email: str, *, aud: str = OUR_CLIENT_ID, azp: str | None = None, **overrides):
    info = {
        "aud": aud,
        "azp": azp if azp is not None else aud,
        "sub": "google-subject-123",
        "email": email,
        "email_verified": "true",
        "expires_in": "3599",
        "scope": "openid email profile",
    }
    info.update(overrides)
    return info


@pytest.fixture
def google(monkeypatch):
    """Stub Google's tokeninfo/userinfo responses; records every URL called."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", OUR_CLIENT_ID)
    state = {"tokeninfo": None, "userinfo": None, "calls": []}

    def fake_get(self, url, *, params=None, headers=None):
        state["calls"].append(url)
        if url == GoogleOAuthProvider.tokeninfo_url:
            return state["tokeninfo"]
        if url == GoogleOAuthProvider.userinfo_url:
            return state["userinfo"]
        raise AssertionError(f"unexpected Google call: {url}")

    monkeypatch.setattr(GoogleOAuthProvider, "_google_get", fake_get)
    return state


def _provider() -> GoogleOAuthProvider:
    return GoogleOAuthProvider(OUR_CLIENT_ID)


# --- provider level ---------------------------------------------------------


def test_access_token_issued_to_fundlok_is_accepted(google):
    google["tokeninfo"] = _tokeninfo("victim@example.com")
    google["userinfo"] = {"sub": "google-subject-123", "name": "Victim Name"}

    claims = _provider().verify_token("ya29.fundlok-token")

    assert claims == {
        "sub": "google-subject-123",
        "email": "victim@example.com",
        "email_verified": True,
        "name": "Victim Name",
    }


def test_access_token_issued_to_another_app_is_rejected(google):
    # The exploit: a real, valid Google token, just not ours.
    google["tokeninfo"] = _tokeninfo("victim@example.com", aud=ATTACKER_CLIENT_ID)
    google["userinfo"] = {"sub": "google-subject-123", "email": "victim@example.com"}

    with pytest.raises(HTTPException) as exc:
        _provider().verify_token("ya29.attacker-token")

    assert exc.value.status_code == 401
    # userinfo must never be the thing that decides; it isn't even consulted.
    assert GoogleOAuthProvider.userinfo_url not in google["calls"]


def test_access_token_with_foreign_azp_is_rejected(google):
    google["tokeninfo"] = _tokeninfo(
        "victim@example.com", aud=OUR_CLIENT_ID, azp=ATTACKER_CLIENT_ID
    )

    with pytest.raises(HTTPException) as exc:
        _provider().verify_token("ya29.mixed-token")
    assert exc.value.status_code == 401


def test_access_token_without_audience_is_rejected(google):
    info = _tokeninfo("victim@example.com")
    del info["aud"]
    google["tokeninfo"] = info

    with pytest.raises(HTTPException) as exc:
        _provider().verify_token("ya29.no-aud")
    assert exc.value.status_code == 401


@pytest.mark.parametrize("flag", ["false", False, None, "", "True", "1"])
def test_unverified_google_email_is_rejected(google, flag):
    google["tokeninfo"] = _tokeninfo("victim@example.com", email_verified=flag)

    with pytest.raises(HTTPException) as exc:
        _provider().verify_token("ya29.unverified")
    assert exc.value.status_code == 401


def test_invalid_or_expired_access_token_is_rejected(google):
    google["tokeninfo"] = None  # tokeninfo answered non-200

    with pytest.raises(HTTPException) as exc:
        _provider().verify_token("ya29.expired")
    assert exc.value.status_code == 401


def test_tokeninfo_missing_email_is_rejected(google):
    info = _tokeninfo("victim@example.com")
    del info["email"]
    google["tokeninfo"] = info

    with pytest.raises(HTTPException) as exc:
        _provider().verify_token("ya29.no-email")
    assert exc.value.status_code == 401


def test_userinfo_for_a_different_account_does_not_supply_the_name(google):
    google["tokeninfo"] = _tokeninfo("victim@example.com")
    google["userinfo"] = {"sub": "someone-else", "name": "Wrong Person"}

    claims = _provider().verify_token("ya29.fundlok-token")

    assert "name" not in claims
    assert claims["email"] == "victim@example.com"


def test_missing_client_id_fails_closed(google):
    google["tokeninfo"] = _tokeninfo("victim@example.com")

    with pytest.raises(HTTPException) as exc:
        GoogleOAuthProvider(None).verify_token("ya29.any")
    assert exc.value.status_code == 500
    assert google["calls"] == []


def test_jwt_is_verified_as_an_id_token(google, monkeypatch):
    seen = {}

    def fake_verify_id_token(self, token):
        seen["token"] = token
        return {"sub": "s", "email": "e@example.com", "email_verified": True}

    monkeypatch.setattr(GoogleOAuthProvider, "verify_id_token", fake_verify_id_token)

    _provider().verify_token("header.payload.signature")

    assert seen["token"] == "header.payload.signature"
    assert google["calls"] == [], "an ID token must not fall back to tokeninfo/userinfo"


# --- endpoint level ---------------------------------------------------------


async def _oauth_login(client, token: str):
    return await client.post(
        "/auth/oauth/login",
        json={"provider": OAuthProvider.GOOGLE.value, "access_token": token},
    )


async def test_replayed_foreign_token_cannot_take_over_an_account(
    client, make_user, google, db_session
):
    victim = await make_user()
    google["tokeninfo"] = _tokeninfo(victim["email"], aud=ATTACKER_CLIENT_ID)
    google["userinfo"] = {"sub": "google-subject-123", "email": victim["email"]}

    async def session_count() -> int:
        rows = await db_session.execute(
            select(RefreshToken).where(RefreshToken.user_id == victim["id"])
        )
        return len(rows.scalars().all())

    # make_user signs the victim in once to mint auth headers.
    before = await session_count()

    resp = await _oauth_login(client, "ya29.attacker-token")

    assert resp.status_code == 401, resp.text
    assert "access_token" not in resp.cookies
    assert await session_count() == before, "no session may be issued for a foreign token"


async def test_fundlok_token_signs_the_user_in(client, make_user, google):
    user = await make_user()
    google["tokeninfo"] = _tokeninfo(user["email"])
    google["userinfo"] = {"sub": "google-subject-123", "name": "OAuth User"}

    resp = await _oauth_login(client, "ya29.fundlok-token")

    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]
