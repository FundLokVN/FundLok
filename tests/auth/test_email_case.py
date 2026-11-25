"""Email addresses match whatever their letter case (backend audit 25 Nov 2026, finding 10).

Addresses used to be stored as typed and found by exact match, with only the domain lowercased by EmailStr. Someone
registered as "Lan.Nguyen@gmail.com" who then used "Sign in with Google" (Google sends "lan.nguyen@gmail.com") got a
second account, without their KYC or investments; typing the address in lowercase failed at login and forgot-password
silently sent nothing. Every sign-in path now finds accounts through auth.service.user_by_email, and new accounts are
stored lowercase. Accounts stored before that keep their case, so these tests write such rows directly.
Google's token verification is stubbed; the account handling is real.
"""
import pytest
from sqlalchemy import func, select, update

from app.oauth.schema import OAuthProvider
from app.oauth.service import GoogleOAuthProvider
from app.users.models import User
from conftest import DEFAULT_PASSWORD


@pytest.fixture
def stub_google(monkeypatch):
    def _stub(email: str):
        monkeypatch.setattr(
            GoogleOAuthProvider,
            "verify_token",
            lambda self, token: {"sub": "google-lan", "email": email, "email_verified": True, "name": "Lan Nguyen"},
        )

    return _stub


async def _login(client, email: str):
    client.cookies.clear()
    return await client.post("/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})


async def _stored_as(db_session, user_id, email: str) -> None:
    """Give an account the address it would have kept from before addresses were stored lowercase."""
    await db_session.execute(update(User).where(User.id == user_id).values(email=email))
    await db_session.commit()


async def _accounts_for(db_session, email: str) -> list[User]:
    db_session.expire_all()
    rows = await db_session.execute(select(User).where(func.lower(User.email) == email.lower()))
    return list(rows.scalars())


async def test_a_new_account_is_stored_lowercase_and_signs_in_with_any_case(client, make_user, db_session):
    user = await make_user(email="Lan.Nguyen@Example.com")
    (account,) = await _accounts_for(db_session, "lan.nguyen@example.com")
    assert account.email == "lan.nguyen@example.com"
    resp = await _login(client, "LAN.NGUYEN@EXAMPLE.COM")
    assert resp.status_code == 200 and resp.json()["id"] == user["id"]


async def test_an_old_mixed_case_account_is_one_person_on_every_path(client, make_user, db_session, stub_google, monkeypatch):
    user = await make_user()
    await _stored_as(db_session, user["id"], "Lan.Nguyen@gmail.com")
    resets, notices = [], []
    monkeypatch.setattr("app.auth.service.send_password_reset_email", lambda to_email, token: resets.append(to_email))
    monkeypatch.setattr("app.auth.service.send_existing_account_notice", lambda to_email: notices.append(to_email))

    # Password sign-in and forgot-password with the address typed in lowercase.
    resp = await _login(client, "lan.nguyen@gmail.com")
    assert resp.status_code == 200 and resp.json()["id"] == user["id"]
    client.cookies.clear()
    assert (await client.post("/auth/forgot-password", json={"email": "lan.nguyen@gmail.com"})).status_code == 200
    assert resets == ["Lan.Nguyen@gmail.com"]

    # Signing up again in another case is the existing account: nothing new, and its owner is told.
    resp = await client.post(
        "/auth/register",
        json={"email": "LAN.NGUYEN@gmail.com", "password": DEFAULT_PASSWORD, "full_name": "Someone"},
    )
    assert resp.status_code == 201 and notices == ["Lan.Nguyen@gmail.com"]

    # Google sends the lowercase address: the same account, not a second one without KYC or investments.
    stub_google("lan.nguyen@gmail.com")
    client.cookies.clear()
    resp = await client.post("/auth/oauth/login", json={"provider": OAuthProvider.GOOGLE.value, "token": "google-token"})
    assert resp.status_code == 200, resp.text
    assert [str(a.id) for a in await _accounts_for(db_session, "lan.nguyen@gmail.com")] == [user["id"]]


async def test_two_old_accounts_differing_only_in_case_still_sign_in(client, make_user, db_session):
    # Possible in data stored before addresses were lowercased: the exact spelling wins, and nothing fails.
    first, second = await make_user(), await make_user()
    await _stored_as(db_session, first["id"], "Twin@example.com")
    await _stored_as(db_session, second["id"], "twin@example.com")
    assert (await _login(client, "Twin@example.com")).json()["id"] == first["id"]
    assert (await _login(client, "twin@example.com")).json()["id"] == second["id"]
    resp = await _login(client, "TWIN@example.com")  # neither spelling: the older account
    assert resp.status_code == 200 and resp.json()["id"] == first["id"]
