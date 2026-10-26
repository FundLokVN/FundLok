"""POST /users/me/password — set a FIRST password from the profile page.

*Changing* an existing password off a session would be account takeover, so
this endpoint refuses any account that already has one; that belongs to
/me/password/change or forgot-password.

A first password needs more than a session too. Once one exists,
require_step_up takes it as proof for enrolling 2FA or a passkey, so a stolen
session could set one, enrol its own 2FA and lock the owner out of their next
Google sign-in. Setting one therefore needs a recent sign-in, like those
enrolments on a passwordless account.

Google/Microsoft sign-in creates accounts with no password; the tests here
simulate one by clearing the hash a registration left behind.
"""
from datetime import timedelta

from sqlalchemy import select, update

from app.users.models import RefreshToken, User

NEW_PASSWORD = "A-Brand-New-Passw0rd!"


async def _make_passwordless(db_session, user) -> None:
    await db_session.execute(
        update(User).where(User.id == user["id"]).values(password_hash=None)
    )
    await db_session.commit()


async def _age_sessions(db_session, user, by: timedelta) -> None:
    await db_session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user["id"])
        .values(created_at=RefreshToken.created_at - by)
    )
    await db_session.commit()


async def test_set_password_needs_a_recent_sign_in(client, make_user, db_session, monkeypatch):
    """The takeover this closes: with an old stolen session, set a password,
    then use it to enrol 2FA the owner can't pass. Both steps must refuse."""
    sent: list[str] = []
    monkeypatch.setattr(
        "app.users.service.send_password_changed_notice",
        lambda to_email, first_time=False: sent.append(to_email),
    )
    user = await make_user()
    await _make_passwordless(db_session, user)
    # A stolen cookie keeps its session's original sign-in time through every
    # refresh, so an old session cannot pass however recently it rotated.
    await _age_sessions(db_session, user, timedelta(minutes=30))

    resp = await client.post(
        "/users/me/password",
        json={"new_password": NEW_PASSWORD},
        headers=user["headers"],
    )
    assert resp.status_code == 403, resp.text
    assert resp.headers["X-Error-Code"] == "REAUTH_REQUIRED"
    assert sent == []

    setup = await client.post(
        "/auth/2fa/setup", json={"password": NEW_PASSWORD}, headers=user["headers"]
    )
    assert setup.status_code == 403, setup.text
    assert setup.headers["X-Error-Code"] == "REAUTH_REQUIRED"

    login = await client.post(
        "/auth/login", json={"email": user["email"], "password": NEW_PASSWORD}
    )
    assert login.status_code == 401, "no password may have been set"


async def test_setting_a_first_password_shows_on_the_security_screen(
    client, make_user, db_session
):
    """If someone else did set one, the owner has to be able to see it."""
    user = await make_user()
    await _make_passwordless(db_session, user)

    resp = await client.post(
        "/users/me/password",
        json={"new_password": NEW_PASSWORD},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text

    events = await client.get("/auth/security-events", headers=user["headers"])
    assert events.status_code == 200, events.text
    assert "PASSWORD_SET" in [event["action"] for event in events.json()]


async def test_set_password_is_refused_when_one_already_exists(client, make_user):
    """The whole point of the endpoint: a session must never be able to
    overwrite an existing password. Those users go through forgot-password."""
    user = await make_user()

    resp = await client.post(
        "/users/me/password",
        json={"new_password": NEW_PASSWORD},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "detail": "This account already has a password. Use Forgot password to change it."
    }

    # Untouched — the original password still logs in.
    login = await client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert login.status_code == 200


async def test_set_password_succeeds_for_a_passwordless_account(client, make_user, db_session):
    """The OAuth case, simulated by clearing the hash a registration left behind."""
    user = await make_user()

    db_user = (
        await db_session.execute(select(User).where(User.email == user["email"]))
    ).scalar_one()
    db_user.password_hash = None
    await db_session.commit()

    resp = await client.post(
        "/users/me/password",
        json={"new_password": NEW_PASSWORD},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"

    login = await client.post(
        "/auth/login", json={"email": user["email"], "password": NEW_PASSWORD}
    )
    assert login.status_code == 200


async def test_setting_a_password_emails_the_owner(client, make_user, db_session, monkeypatch):
    """A credential change the owner didn't make has to be visible to them, so
    every successful set/change sends a notice."""
    sent: list[dict] = []
    monkeypatch.setattr(
        "app.users.service.send_password_changed_notice",
        lambda to_email, first_time=False: sent.append(
            {"to": to_email, "first_time": first_time}
        ),
    )

    user = await make_user()
    db_user = (
        await db_session.execute(select(User).where(User.email == user["email"]))
    ).scalar_one()
    db_user.password_hash = None
    await db_session.commit()

    resp = await client.post(
        "/users/me/password",
        json={"new_password": NEW_PASSWORD},
        headers=user["headers"],
    )
    assert resp.status_code == 200
    # first_time=True: "your password was changed" would be untrue here.
    assert sent == [{"to": user["email"], "first_time": True}]


async def test_a_refused_set_password_sends_no_email(client, make_user, monkeypatch):
    """No notice when nothing changed — a false 'your password changed' alert
    is exactly the noise that trains people to ignore the real one."""
    sent: list[str] = []
    monkeypatch.setattr(
        "app.users.service.send_password_changed_notice",
        lambda to_email, first_time=False: sent.append(to_email),
    )

    user = await make_user()  # already has a password -> refused
    resp = await client.post(
        "/users/me/password",
        json={"new_password": NEW_PASSWORD},
        headers=user["headers"],
    )
    assert resp.status_code == 400
    assert sent == []


async def test_passwordless_account_cannot_log_in_with_a_password(client, make_user, db_session):
    """password_hash became nullable, so authenticate_user has to reject a
    passwordless account itself -- handing None to passlib raises, which would
    turn an ordinary failed login into a 500."""
    user = await make_user()

    db_user = (
        await db_session.execute(select(User).where(User.email == user["email"]))
    ).scalar_one()
    db_user.password_hash = None
    await db_session.commit()

    resp = await client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert resp.status_code == 401


async def test_set_password_rejects_a_short_password(client, make_user):
    user = await make_user()

    resp = await client.post(
        "/users/me/password",
        json={"new_password": "short1"},
        headers=user["headers"],
    )
    assert resp.status_code == 422


async def test_set_password_requires_authentication(client):
    resp = await client.post("/users/me/password", json={"new_password": NEW_PASSWORD})
    assert resp.status_code == 401


async def test_users_me_reports_whether_a_password_is_set(client, make_user):
    """Drives the profile page's choice between "Set password" (opens the
    dialog) and "Change password" (sends the user to forgot-password)."""
    user = await make_user()
    resp = await client.get("/users/me", headers=user["headers"])
    assert resp.status_code == 200
    assert resp.json()["has_password"] is True
