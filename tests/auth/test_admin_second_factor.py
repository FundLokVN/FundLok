"""ADMIN and SYSTEM_ADMIN sessions must have used a second factor.

Two-factor sign-in was optional for every account, and admin routes checked
only the role, so a password alone opened every admin endpoint. Now a session
records whether it was opened with a TOTP code or a user-verified passkey
(the `mfa` claim), and get_current_user refuses an admin session without one,
with 403 MFA_REQUIRED.

Before a factor exists, an admin can still read their own profile and enrol
the first factor. Once one exists, adding another needs a session that used
it, or the password alone could enrol an attacker's authenticator.
"""
from sqlalchemy import select

from app.auth.passkeys import UV_REQUIRED_FOR_ADMIN
from app.users.models import User
from app.utils.jwt import SECOND_FACTOR_REQUIRED_DETAIL
from tests.auth.test_passkey_2fa_user_verification import SoftAuthenticator, _register, _sign_in
from tests.auth.test_totp import code_for, enrol


async def _password_only(make_user, db_session, role: str = "ADMIN") -> dict:
    """An admin signed in with the password alone: make_user's own session,
    promoted out of band. make_admin is not used because it swaps in a
    second-factor session."""
    user = await make_user(role=None)
    row = (await db_session.execute(select(User).where(User.id == user["id"]))).scalar_one()
    row.role = role
    # Passkeys can only be added once the address is proven.
    row.email_verified = True
    await db_session.commit()
    return user


def _bearer(resp) -> dict:
    return {"Authorization": f"Bearer {resp.cookies.get('access_token')}"}


def _assert_second_factor_required(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": SECOND_FACTOR_REQUIRED_DETAIL}
    assert resp.headers["X-Error-Code"] == "MFA_REQUIRED"


# --- refused without a second factor ----------------------------------------------


async def test_admin_password_session_is_refused_on_admin_routes(client, make_user, db_session):
    admin = await _password_only(make_user, db_session)

    _assert_second_factor_required(await client.get("/admin/overview", headers=admin["headers"]))


async def test_system_admin_password_session_is_refused(client, make_user, db_session):
    admin = await _password_only(make_user, db_session, role="SYSTEM_ADMIN")

    _assert_second_factor_required(await client.get("/admin/overview", headers=admin["headers"]))


async def test_admin_password_session_is_refused_outside_admin_routes(client, make_user, db_session):
    # Some admin access is decided inside services on the role alone, so the
    # check sits on every authenticated endpoint, not only on admin routes.
    admin = await _password_only(make_user, db_session)

    _assert_second_factor_required(await client.get("/auth/sessions", headers=admin["headers"]))


async def test_sme_password_session_is_unaffected(client, make_user):
    sme = await make_user(role="SME")

    resp = await client.get("/auth/sessions", headers=sme["headers"])

    assert resp.status_code == 200, resp.text


# --- what an admin can do before a factor exists ----------------------------------


async def test_admin_without_second_factor_can_read_own_profile_and_2fa_status(
    client, make_user, db_session
):
    admin = await _password_only(make_user, db_session)

    for path in ("/users/me", "/auth/2fa", "/auth/passkeys"):
        resp = await client.get(path, headers=admin["headers"])
        assert resp.status_code == 200, f"{path}: {resp.text}"

    overview = await client.get("/auth/security-overview", headers=admin["headers"])
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["second_factor_required"] is True
    assert body["two_factor"]["enabled"] is False
    assert body["passkeys"] == []
    assert body["sessions"] == []
    assert body["security_events"] == []
    assert body["security_preferences"] is None



async def test_admin_enrols_totp_then_signs_in_with_it_to_reach_admin_routes(
    client, make_user, db_session
):
    admin = await _password_only(make_user, db_session)
    secret, _ = await enrol(client, admin)

    # Enrolling does not upgrade the session it was done from.
    _assert_second_factor_required(await client.get("/admin/overview", headers=admin["headers"]))

    login = await client.post("/auth/login", json={"email": admin["email"], "password": admin["password"]})
    assert login.status_code == 200, login.text
    second = await client.post(
        "/auth/login/2fa",
        json={"challenge_token": login.json()["challenge_token"], "code": code_for(secret)},
    )
    assert second.status_code == 200, second.text

    resp = await client.get("/admin/overview", headers=_bearer(second))
    assert resp.status_code == 200, resp.text


async def test_second_factor_survives_refresh(client, make_user, db_session):
    admin = await _password_only(make_user, db_session)
    secret, _ = await enrol(client, admin)
    login = await client.post("/auth/login", json={"email": admin["email"], "password": admin["password"]})
    second = await client.post(
        "/auth/login/2fa",
        json={"challenge_token": login.json()["challenge_token"], "code": code_for(secret)},
    )
    assert second.status_code == 200, second.text

    refreshed = await client.post(
        "/auth/refresh", json={"refresh_token": second.cookies.get("refresh_token")}
    )
    assert refreshed.status_code == 200, refreshed.text

    resp = await client.get(
        "/admin/overview",
        headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
    )
    assert resp.status_code == 200, resp.text


async def test_admin_with_a_factor_cannot_add_another_on_a_password_session(
    client, make_user, db_session
):
    # A passkey-only admin still has a password sign-in, and it opens a
    # session without a second factor. That session must not be able to add a
    # factor the attacker controls.
    admin = await _password_only(make_user, db_session)
    key = SoftAuthenticator()
    assert (await _register(client, admin, key, user_verified=True)).status_code == 201

    _assert_second_factor_required(await client.post("/auth/2fa/setup", headers=admin["headers"]))
    _assert_second_factor_required(
        await client.post("/auth/passkeys/register/options", headers=admin["headers"])
    )


# --- passkeys ----------------------------------------------------------------------


async def test_admin_passkey_must_verify_the_user_at_registration(client, make_user, db_session):
    admin = await _password_only(make_user, db_session)

    resp = await _register(client, admin, SoftAuthenticator(), user_verified=False)

    assert resp.status_code == 400
    assert resp.json() == {"detail": UV_REQUIRED_FOR_ADMIN}


async def test_admin_passkey_sign_in_without_user_verification_is_refused(
    client, make_user, db_session
):
    admin = await _password_only(make_user, db_session)
    key = SoftAuthenticator()
    assert (await _register(client, admin, key, user_verified=True)).status_code == 201

    resp = await _sign_in(client, key, user_verified=False)

    assert resp.status_code == 401
    assert resp.json() == {"detail": UV_REQUIRED_FOR_ADMIN}
    assert "access_token" not in resp.cookies


async def test_admin_user_verified_passkey_sign_in_reaches_admin_routes(
    client, make_user, db_session
):
    admin = await _password_only(make_user, db_session)
    key = SoftAuthenticator()
    assert (await _register(client, admin, key, user_verified=True)).status_code == 201

    signed_in = await _sign_in(client, key, user_verified=True)
    assert signed_in.status_code == 200, signed_in.text

    resp = await client.get("/admin/overview", headers=_bearer(signed_in))
    assert resp.status_code == 200, resp.text
