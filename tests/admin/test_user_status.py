"""PATCH /admin/users/{id}/status and what SUSPENDED actually does.

Before this endpoint existed, `users.status` was written by seeds and read by
nothing: a SUSPENDED account logged in and worked exactly like an active one.
So these tests cover two things that have to be true together — an operator
can set the status, and the status is enforced. A suspend button that only
writes a column is worse than no button, because it looks like a control.
"""
import uuid

from app.users.models import (
    USER_STATUS_ACTIVE,
    USER_STATUS_PENDING_KYC,
    USER_STATUS_SUSPENDED,
)


# --------------------------------------------------------------------------- #
# Setting the status
# --------------------------------------------------------------------------- #


async def test_admin_can_suspend_a_member_account(client, make_admin, make_user):
    sme = await make_user(role="SME")
    admin = await make_admin()

    resp = await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_SUSPENDED, "note": "Fraud review"},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == USER_STATUS_SUSPENDED


async def test_admin_can_restore_a_suspended_account(client, make_admin, make_user):
    """Reversible, unlike the approval decisions — a suspension is a state, not
    a recorded verdict."""
    sme = await make_user(role="SME")
    admin = await make_admin()
    await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )

    resp = await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_ACTIVE},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == USER_STATUS_ACTIVE


async def test_pending_kyc_is_a_settable_status(client, make_admin, make_user):
    sme = await make_user(role="SME")
    admin = await make_admin()

    resp = await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_PENDING_KYC},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text


async def test_an_unknown_status_is_rejected(client, make_admin, make_user):
    sme = await make_user(role="SME")
    admin = await make_admin()

    resp = await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": "DELETED"},
        headers=admin["headers"],
    )

    assert resp.status_code == 422


async def test_unknown_user_is_404(client, make_admin):
    admin = await make_admin()
    resp = await client.patch(
        f"/admin/users/{uuid.uuid4()}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# The two guards
# --------------------------------------------------------------------------- #


async def test_an_admin_cannot_suspend_themselves(client, make_admin):
    """Unrecoverable if allowed: the endpoint that would undo it is the one
    they just locked themselves out of."""
    admin = await make_admin()

    resp = await client.patch(
        f"/admin/users/{admin['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )

    assert resp.status_code == 409, resp.text


async def test_a_plain_admin_cannot_suspend_another_admin(
    client, make_admin
):
    """Otherwise any ADMIN can suspend every peer and the system admin, which
    is a takeover rather than moderation."""
    victim = await make_admin(email=f"victim-{uuid.uuid4().hex[:8]}@example.com")
    actor = await make_admin(email=f"actor-{uuid.uuid4().hex[:8]}@example.com")

    resp = await client.patch(
        f"/admin/users/{victim['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=actor["headers"],
    )

    assert resp.status_code == 403, resp.text


async def test_a_system_admin_may_suspend_an_admin(
    client, make_admin, make_system_admin
):
    """The escape hatch the guard above leaves open, so an admin account can
    still be dealt with by someone."""
    victim = await make_admin(email=f"victim-{uuid.uuid4().hex[:8]}@example.com")
    actor = await make_system_admin()

    resp = await client.patch(
        f"/admin/users/{victim['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=actor["headers"],
    )

    assert resp.status_code == 200, resp.text


async def test_an_sme_cannot_change_anyone_s_status(client, make_user):
    sme = await make_user(role="SME")
    victim = await make_user(role="INVESTOR")

    resp = await client.patch(
        f"/admin/users/{victim['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=sme["headers"],
    )

    assert resp.status_code == 403, resp.text


async def test_an_anonymous_caller_cannot_change_a_status(client, make_user):
    victim = await make_user(role="INVESTOR")

    resp = await client.patch(
        f"/admin/users/{victim['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
    )

    assert resp.status_code in (401, 403), resp.text


# --------------------------------------------------------------------------- #
# Enforcement — the half that makes the button real
# --------------------------------------------------------------------------- #


async def test_a_suspended_account_cannot_use_its_existing_session(
    client, make_admin, make_user
):
    """The token was minted before the suspension and is still unexpired. It
    must stop working anyway, or a suspension only takes effect whenever the
    holder next happens to log out."""
    sme = await make_user(role="SME")
    before = await client.get("/users/me", headers=sme["headers"])
    assert before.status_code == 200

    admin = await make_admin()
    await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )

    after = await client.get("/users/me", headers=sme["headers"])
    assert after.status_code == 403, after.text


async def test_a_suspended_account_cannot_log_in(client, make_admin, make_user):
    from conftest import DEFAULT_PASSWORD

    sme = await make_user(role="SME")
    admin = await make_admin()
    await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )

    resp = await client.post(
        "/auth/login",
        json={"email": sme["email"], "password": DEFAULT_PASSWORD},
    )

    assert resp.status_code == 403, resp.text


async def test_a_pending_kyc_account_still_works(client, make_admin, make_user):
    """Deny-list, not allow-list. PENDING_KYC is a normal onboarding state and
    locking it out would strand every investor mid-signup."""
    investor = await make_user(role="INVESTOR")
    admin = await make_admin()
    await client.patch(
        f"/admin/users/{investor['id']}/status",
        json={"status": USER_STATUS_PENDING_KYC},
        headers=admin["headers"],
    )

    resp = await client.get("/users/me", headers=investor["headers"])

    assert resp.status_code == 200, resp.text


async def test_restoring_an_account_lets_it_back_in(client, make_admin, make_user):
    sme = await make_user(role="SME")
    admin = await make_admin()
    await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )
    assert (await client.get("/users/me", headers=sme["headers"])).status_code == 403

    await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_ACTIVE},
        headers=admin["headers"],
    )

    assert (await client.get("/users/me", headers=sme["headers"])).status_code == 200


# --------------------------------------------------------------------------- #
# The routes that do NOT go through get_current_user
#
# Everything role-gated (require_admin / require_sme / authorized / …) is built
# from require_roles, which depends on get_current_user, so all of it inherits
# the suspension check. These two authenticate by token instead and had to be
# closed by hand — they are exactly the holes a "suspended" account would have
# slipped through.
# --------------------------------------------------------------------------- #


async def test_a_suspended_account_cannot_refresh_its_tokens(
    client, make_admin, make_user
):
    """Otherwise the account keeps minting fresh credentials and its session
    row stays alive indefinitely through rotation."""
    from conftest import DEFAULT_PASSWORD

    sme = await make_user(role="SME")
    login = await client.post(
        "/auth/login",
        json={"email": sme["email"], "password": DEFAULT_PASSWORD},
    )
    assert login.status_code == 200, login.text
    refresh_token = login.cookies.get("refresh_token")
    assert refresh_token

    admin = await make_admin()
    await client.patch(
        f"/admin/users/{sme['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )

    resp = await client.post(
        "/auth/refresh", json={"refresh_token": refresh_token}
    )

    assert resp.status_code == 401, resp.text


async def test_a_suspended_investor_cannot_submit_via_the_phone_handoff(
    client, make_admin, make_user
):
    """The QR handoff authenticates by a short-lived token, not a cookie, so it
    never passes through get_current_user."""
    investor = await make_user(role="INVESTOR")
    handoff = await client.post(
        "/gverify/kyc/handoff", headers=investor["headers"]
    )
    assert handoff.status_code == 201, handoff.text
    token = handoff.json()["token"]

    admin = await make_admin()
    await client.patch(
        f"/admin/users/{investor['id']}/status",
        json={"status": USER_STATUS_SUSPENDED},
        headers=admin["headers"],
    )

    resp = await client.post(
        "/gverify/kyc/handoff/verify",
        json={
            "id_front_b64": "aGVsbG8=",
            "id_back_b64": "aGVsbG8=",
            "portrait_b64": "aGVsbG8=",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403, resp.text
