"""Adding a sign-in method needs more than the session.

/auth/passkeys/register/options and /auth/2fa/setup only asked for a session,
so whoever held a stolen one could register their own passkey (which keeps
signing them in after the owner changes the password) or enrol their own
authenticator on an account without 2FA. They now need the account password,
or for OAuth-only accounts, which have none, a session that signed in moments
ago. /auth/2fa/disable already asked for the password and a code, but checked
any number of them; it now shares the same attempt limit.
"""
from datetime import timedelta

from sqlalchemy import select, update

from app.auth.service import MAX_STEP_UP_FAILURES, STEP_UP_FAILED
from app.lending.models import AuditLog
from app.users.models import RefreshToken, User
from tests.auth.test_totp import code_for, enrol

ENROL_STARTS = ("/auth/2fa/setup", "/auth/passkeys/register/options")


async def _verified(db_session, user) -> None:
    # Passkey registration is refused before email verification, which is a
    # separate gate: verify so these tests reach the step-up check.
    await db_session.execute(
        update(User).where(User.id == user["id"]).values(email_verified=True)
    )
    await db_session.commit()


async def _make_oauth_only(db_session, user) -> None:
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


async def test_starting_enrolment_without_the_password_is_refused(
    client, make_user, db_session
):
    user = await make_user(role="INVESTOR")
    await _verified(db_session, user)

    for path in ENROL_STARTS:
        resp = await client.post(path, headers=user["headers"])
        assert resp.status_code == 403, (path, resp.text)
        assert resp.headers["X-Error-Code"] == "PASSWORD_REQUIRED"


async def test_a_wrong_password_is_refused_and_recorded(client, make_user, db_session):
    user = await make_user(role="INVESTOR")
    await _verified(db_session, user)

    for path in ENROL_STARTS:
        resp = await client.post(
            path, json={"password": "not-the-password"}, headers=user["headers"]
        )
        assert resp.status_code == 400, (path, resp.text)

    misses = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == user["id"], AuditLog.action == STEP_UP_FAILED
            )
        )
    ).scalars().all()
    assert len(misses) == len(ENROL_STARTS)
    # On the owner's security screen, like a wrong 2FA code at sign-in.
    assert all(row.entity_type == "AUTH" for row in misses)


async def test_the_right_password_starts_enrolment(client, make_user, db_session):
    user = await make_user(role="INVESTOR")
    await _verified(db_session, user)

    setup = await client.post(
        "/auth/2fa/setup", json={"password": user["password"]}, headers=user["headers"]
    )
    options = await client.post(
        "/auth/passkeys/register/options",
        json={"password": user["password"]},
        headers=user["headers"],
    )

    assert setup.status_code == 201, setup.text
    assert options.status_code == 200, options.text


async def test_wrong_passwords_lock_step_up_even_for_the_right_one(
    client, make_user, db_session
):
    user = await make_user(role="INVESTOR")
    await _verified(db_session, user)

    for _ in range(MAX_STEP_UP_FAILURES):
        await client.post(
            "/auth/passkeys/register/options",
            json={"password": "guess"},
            headers=user["headers"],
        )

    resp = await client.post(
        "/auth/passkeys/register/options",
        json={"password": user["password"]},
        headers=user["headers"],
    )
    assert resp.status_code == 429, resp.text
    assert resp.headers["X-Error-Code"] == "STEP_UP_LOCKED"


async def test_oauth_only_account_passes_on_a_fresh_sign_in(
    client, make_user, db_session
):
    user = await make_user(role="INVESTOR")
    await _verified(db_session, user)
    await _make_oauth_only(db_session, user)

    resp = await client.post("/auth/passkeys/register/options", headers=user["headers"])

    assert resp.status_code == 200, resp.text


async def test_oauth_only_account_must_sign_in_again_once_the_session_is_old(
    client, make_user, db_session
):
    user = await make_user(role="INVESTOR")
    await _verified(db_session, user)
    await _make_oauth_only(db_session, user)
    # A stolen cookie keeps its session's original sign-in time through every
    # refresh, so an old session cannot pass however recently it rotated.
    await _age_sessions(db_session, user, timedelta(minutes=30))

    for path in ENROL_STARTS:
        resp = await client.post(path, headers=user["headers"])
        assert resp.status_code == 403, (path, resp.text)
        assert resp.headers["X-Error-Code"] == "REAUTH_REQUIRED"


async def test_disabling_2fa_is_limited_across_password_and_code_misses(
    client, make_user
):
    user = await make_user(role="INVESTOR")
    secret, _ = await enrol(client, user)

    for attempt in range(MAX_STEP_UP_FAILURES):
        # Alternate the two oracles: both count towards one limit.
        body = (
            {"password": "guess", "code": code_for(secret)}
            if attempt % 2
            else {"password": user["password"], "code": "000000"}
        )
        resp = await client.post("/auth/2fa/disable", json=body, headers=user["headers"])
        assert resp.status_code == 400, resp.text

    resp = await client.post(
        "/auth/2fa/disable",
        json={"password": user["password"], "code": code_for(secret)},
        headers=user["headers"],
    )
    assert resp.status_code == 429, resp.text

    status_resp = await client.get("/auth/2fa", headers=user["headers"])
    assert status_resp.json()["enabled"] is True


async def _second_session(client, user) -> dict:
    """The same account signed in again: a separate session, as on a second
    device. Cookies cleared so the new sign-in is not tied to the first."""
    client.cookies.clear()
    login = await client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.cookies.get('access_token')}"}


async def test_misses_on_one_session_do_not_lock_out_another(
    client, make_user, db_session
):
    # Counted per account, a stolen session could keep the owner from adding
    # a passkey or turning 2FA off by missing five times every 15 minutes.
    user = await make_user(role="INVESTOR")
    await _verified(db_session, user)
    owner_headers = await _second_session(client, user)

    for _ in range(MAX_STEP_UP_FAILURES):
        await client.post(
            "/auth/passkeys/register/options",
            json={"password": "guess"},
            headers=user["headers"],
        )
    thief = await client.post(
        "/auth/passkeys/register/options",
        json={"password": user["password"]},
        headers=user["headers"],
    )
    assert thief.status_code == 429, thief.text

    owner = await client.post(
        "/auth/passkeys/register/options",
        json={"password": user["password"]},
        headers=owner_headers,
    )
    assert owner.status_code == 200, owner.text


async def test_oauth_only_account_can_disable_2fa_on_a_fresh_sign_in(
    client, make_user, db_session
):
    user = await make_user(role="INVESTOR")
    secret, _ = await enrol(client, user)
    await _make_oauth_only(db_session, user)

    resp = await client.post(
        "/auth/2fa/disable", json={"code": code_for(secret)}, headers=user["headers"]
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False


async def test_oauth_only_account_must_sign_in_again_to_disable_2fa(
    client, make_user, db_session
):
    user = await make_user(role="INVESTOR")
    secret, _ = await enrol(client, user)
    await _make_oauth_only(db_session, user)
    await _age_sessions(db_session, user, timedelta(minutes=30))

    resp = await client.post(
        "/auth/2fa/disable", json={"code": code_for(secret)}, headers=user["headers"]
    )

    assert resp.status_code == 403, resp.text
    assert resp.headers["X-Error-Code"] == "REAUTH_REQUIRED"
    # Not a guess: nothing recorded towards the lockout.
    misses = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == user["id"], AuditLog.action == STEP_UP_FAILED
            )
        )
    ).scalars().all()
    assert misses == []
