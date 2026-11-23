"""PATCH /admin/users/{id}/role: promote an existing account to admin, or demote one.

docs/specs/admin/user-role-change.md. Granting admin access is the most
consequential thing the admin panel does, so the guards are tested as hard as
the happy path: who may grant what, the accounts that may not be promoted yet,
and that a refused change leaves no trace while an accepted one leaves exactly
one audit row and no live session behind.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.admin import role_change
from app.lending.models import AuditLog, Project, ProjectOwnership
from app.users.models import USER_STATUS_SUSPENDED, RefreshToken, User
from tests.direct_transfer.helpers import make_investor, make_world, open_window, reserve


def _role_url(user_id) -> str:
    return f"/admin/users/{user_id}/role"


async def _verified(db_session, user: dict) -> dict:
    """Register leaves the email unverified; a real member clicks the link. Promotion needs it (rule 6.2)."""
    row = (await db_session.execute(select(User).where(User.id == user["id"]))).scalar_one()
    row.email_verified = True
    await db_session.commit()
    return user


@pytest.fixture
def make_member(make_user, db_session):
    async def _make(role: str | None = None, email: str | None = None):
        return await _verified(db_session, await make_user(role=role, email=email))

    return _make


async def _role_audits(db_session, user_id) -> list[AuditLog]:
    return (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == uuid.UUID(str(user_id)), AuditLog.action == "USER_ROLE_CHANGED"
            )
        )
    ).scalars().all()


async def _role_of(db_session, user_id) -> str | None:
    row = (
        await db_session.execute(
            select(User).where(User.id == uuid.UUID(str(user_id))).execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.role


def _code(resp) -> str | None:
    return resp.headers.get("x-error-code")


# --------------------------------------------------------------------------- #
# Who may grant what
# --------------------------------------------------------------------------- #


async def test_admin_promotes_a_roleless_account_to_admin(client, make_admin, make_member, db_session):
    member = await make_member(role=None)
    admin = await make_admin()

    resp = await client.patch(_role_url(member["id"]), json={"role": "ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "ADMIN"
    assert await _role_of(db_session, member["id"]) == "ADMIN"


async def test_admin_promotes_an_investor_with_no_live_money(client, make_admin, make_member):
    investor = await make_member(role="INVESTOR")
    admin = await make_admin()

    resp = await client.patch(_role_url(investor["id"]), json={"role": "ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "ADMIN"


async def test_system_admin_promotes_a_member_to_system_admin(client, make_system_admin, make_member):
    member = await make_member(role="SME")
    sysadmin = await make_system_admin()

    resp = await client.patch(_role_url(member["id"]), json={"role": "SYSTEM_ADMIN"}, headers=sysadmin["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "SYSTEM_ADMIN"


async def test_system_admin_promotes_an_admin_to_system_admin(client, make_admin, make_system_admin):
    admin = await make_admin()
    sysadmin = await make_system_admin()

    resp = await client.patch(_role_url(admin["id"]), json={"role": "SYSTEM_ADMIN"}, headers=sysadmin["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "SYSTEM_ADMIN"


async def test_admin_cannot_grant_system_admin(client, make_admin, make_member, db_session):
    """An ADMIN never grants a role above their own: that would be escalation by proxy."""
    member = await make_member(role=None)
    admin = await make_admin()

    resp = await client.patch(_role_url(member["id"]), json={"role": "SYSTEM_ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 403, resp.text
    assert _code(resp) == "ROLE_CHANGE_FORBIDDEN"
    assert await _role_of(db_session, member["id"]) is None


async def test_admin_cannot_change_another_admins_role(client, make_admin):
    """Same line as the status route: admin-level accounts are a SYSTEM_ADMIN's to change."""
    peer = await make_admin(email=f"peer-{uuid.uuid4().hex[:8]}@example.com")
    actor = await make_admin(email=f"actor-{uuid.uuid4().hex[:8]}@example.com")

    resp = await client.patch(_role_url(peer["id"]), json={"role": "INVESTOR"}, headers=actor["headers"])

    assert resp.status_code == 403, resp.text
    assert _code(resp) == "ROLE_CHANGE_FORBIDDEN"


async def test_system_admin_demotes_an_admin_to_investor(client, make_admin, make_system_admin, db_session):
    admin = await make_admin()
    sysadmin = await make_system_admin()

    resp = await client.patch(_role_url(admin["id"]), json={"role": "INVESTOR"}, headers=sysadmin["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "INVESTOR"
    [audit] = await _role_audits(db_session, admin["id"])
    assert audit.after_state["direction"] == "DEMOTION"


async def test_system_admin_demotes_a_system_admin_to_admin(client, make_system_admin):
    other = await make_system_admin(email=f"other-{uuid.uuid4().hex[:8]}@example.com")
    actor = await make_system_admin(email=f"actor-{uuid.uuid4().hex[:8]}@example.com")

    resp = await client.patch(_role_url(other["id"]), json={"role": "ADMIN"}, headers=actor["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "ADMIN"


@pytest.mark.parametrize("caller_role", ["INVESTOR", "SME", None])
async def test_investor_and_sme_cannot_change_roles(client, make_member, caller_role, db_session):
    caller = await make_member(role=caller_role)
    target = await make_member(role=None)

    resp = await client.patch(_role_url(target["id"]), json={"role": "ADMIN"}, headers=caller["headers"])

    assert resp.status_code == 403, resp.text
    assert await _role_of(db_session, target["id"]) is None
    # Nor on themselves.
    own = await client.patch(_role_url(caller["id"]), json={"role": "ADMIN"}, headers=caller["headers"])
    assert own.status_code == 403
    assert await _role_of(db_session, caller["id"]) == caller_role


async def test_anonymous_caller_is_refused(client, make_member):
    target = await make_member(role=None)
    client.cookies.clear()  # the shared client keeps the last sign-in's cookies

    resp = await client.patch(_role_url(target["id"]), json={"role": "ADMIN"})

    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


async def test_nobody_changes_their_own_role(client, make_admin, make_system_admin):
    admin = await make_admin()
    sysadmin = await make_system_admin()

    up = await client.patch(_role_url(admin["id"]), json={"role": "SYSTEM_ADMIN"}, headers=admin["headers"])
    down = await client.patch(_role_url(sysadmin["id"]), json={"role": "ADMIN"}, headers=sysadmin["headers"])

    assert up.status_code == 409 and _code(up) == "ROLE_CHANGE_SELF"
    assert down.status_code == 409 and _code(down) == "ROLE_CHANGE_SELF"


async def test_unknown_user_is_404(client, make_admin):
    admin = await make_admin()

    resp = await client.patch(_role_url(uuid.uuid4()), json={"role": "ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 404
    assert _code(resp) == "USER_NOT_FOUND"


async def test_same_role_is_refused(client, make_admin, make_system_admin):
    admin = await make_admin()
    sysadmin = await make_system_admin()

    resp = await client.patch(_role_url(admin["id"]), json={"role": "ADMIN"}, headers=sysadmin["headers"])

    assert resp.status_code == 409
    assert _code(resp) == "ROLE_UNCHANGED"


@pytest.mark.parametrize(("from_role", "to_role"), [("INVESTOR", "SME"), ("SME", "INVESTOR"), (None, "INVESTOR")])
async def test_member_to_member_moves_are_refused(client, make_system_admin, make_member, from_role, to_role):
    """Members pick investor or business themselves at /select-role; this route only grants admin roles."""
    member = await make_member(role=from_role)
    sysadmin = await make_system_admin()

    resp = await client.patch(_role_url(member["id"]), json={"role": to_role}, headers=sysadmin["headers"])

    assert resp.status_code == 409, resp.text
    assert _code(resp) == "ROLE_CHANGE_NOT_SUPPORTED"


@pytest.mark.parametrize(
    "body",
    [{"role": "OWNER"}, {"role": None}, {}, {"role": "ADMIN", "extra": 1}, {"role": "ADMIN", "note": "x" * 501}],
)
async def test_unknown_role_and_extra_fields_are_422(client, make_system_admin, make_member, body):
    member = await make_member(role=None)
    sysadmin = await make_system_admin()

    resp = await client.patch(_role_url(member["id"]), json=body, headers=sysadmin["headers"])

    assert resp.status_code == 422


async def test_suspended_account_cannot_be_promoted(client, make_admin, make_member, db_session):
    member = await make_member(role="INVESTOR")
    row = (await db_session.execute(select(User).where(User.id == member["id"]))).scalar_one()
    row.status = USER_STATUS_SUSPENDED
    await db_session.commit()
    admin = await make_admin()

    resp = await client.patch(_role_url(member["id"]), json={"role": "ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 409
    assert _code(resp) == "ROLE_CHANGE_SUSPENDED"


async def test_unverified_email_cannot_be_promoted(client, make_admin, make_user, db_session):
    """An unverified staff-looking address may not belong to whoever registered it."""
    squatter = await make_user(role=None, email=f"ops-{uuid.uuid4().hex[:6]}@fundlok.com")
    admin = await make_admin()

    resp = await client.patch(_role_url(squatter["id"]), json={"role": "ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 409
    assert _code(resp) == "ROLE_CHANGE_EMAIL_UNVERIFIED"
    assert await _role_of(db_session, squatter["id"]) is None


async def test_sme_with_a_project_in_progress_cannot_be_promoted(client, make_admin, make_member, db_session):
    sme = await make_member(role="SME")
    project = Project(legal_name="Cong Ty Dang Vay", tax_id=None)
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectOwnership(project_id=project.id, user_id=uuid.UUID(sme["id"])))
    await db_session.commit()
    admin = await make_admin()

    resp = await client.patch(_role_url(sme["id"]), json={"role": "ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 409, resp.text
    assert _code(resp) == "ROLE_CHANGE_OPEN_PROJECT"

    # Once the project has ended the same account can be promoted.
    project.status = "CLOSED"
    await db_session.commit()
    again = await client.patch(_role_url(sme["id"]), json={"role": "ADMIN"}, headers=admin["headers"])
    assert again.status_code == 200, again.text


async def test_investor_in_a_live_round_cannot_be_promoted(client, make_admin, db_session):
    world = await make_world(db_session)
    await open_window(world)
    lender = await make_investor(world)
    await reserve(world, lender, 10_000_000)
    await db_session.commit()
    admin = await make_admin()

    resp = await client.patch(_role_url(lender.user.id), json={"role": "ADMIN"}, headers=admin["headers"])

    assert resp.status_code == 409, resp.text
    assert _code(resp) == "ROLE_CHANGE_LIVE_INVESTMENT"
    assert await _role_of(db_session, lender.user.id) == "INVESTOR"


async def test_last_system_admin_cannot_be_demoted(make_admin, make_system_admin, db_session):
    """Rule 2 means nobody demotes themselves, so this guard only trips in a race: two system admins demote each
    other at once, and the second request passed the route guard before the first committed. Called with that stale
    caller directly, since over HTTP its session would already be gone."""
    target = await make_system_admin()
    stale_caller = await make_admin()  # a SYSTEM_ADMIN when its request passed the guard, demoted since

    with pytest.raises(HTTPException) as refused:
        await role_change.change_user_role(
            db_session, uuid.UUID(target["id"]), new_role="ADMIN", note=None,
            actor=SimpleNamespace(id=uuid.UUID(stale_caller["id"]), role="SYSTEM_ADMIN"),
        )

    assert refused.value.status_code == 409
    assert refused.value.headers["X-Error-Code"] == "LAST_SYSTEM_ADMIN"
    assert await _role_of(db_session, target["id"]) == "SYSTEM_ADMIN"


# --------------------------------------------------------------------------- #
# What an accepted change leaves behind
# --------------------------------------------------------------------------- #


async def test_promotion_writes_one_audit_row_with_actor_roles_and_ip(client, make_admin, make_member, db_session):
    investor = await make_member(role="INVESTOR")
    admin = await make_admin()

    resp = await client.patch(
        _role_url(investor["id"]), json={"role": "ADMIN", "note": "Joins operations"}, headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text

    [audit] = await _role_audits(db_session, investor["id"])
    assert audit.entity_type == "USER"
    assert str(audit.actor_id) == admin["id"]
    assert audit.before_state == {"role": "INVESTOR"}
    assert audit.after_state["role"] == "ADMIN"
    assert audit.after_state["direction"] == "PROMOTION"
    assert audit.after_state["note"] == "Joins operations"
    assert audit.after_state["sessions_revoked"] == 1
    assert audit.after_state["is_external_domain"] is True
    assert audit.ip_address == "127.0.0.1"


async def test_refused_change_writes_nothing(client, make_admin, make_member, db_session):
    member = await make_member(role=None)
    admin = await make_admin()

    resp = await client.patch(_role_url(member["id"]), json={"role": "SYSTEM_ADMIN"}, headers=admin["headers"])
    assert resp.status_code == 403

    assert await _role_audits(db_session, member["id"]) == []
    me = await client.get("/users/me", headers=member["headers"])
    assert me.status_code == 200  # the session survived


async def test_role_change_signs_the_account_out_everywhere(client, make_admin, make_member, db_session):
    investor = await make_member(role="INVESTOR")
    admin = await make_admin()
    assert (await client.get("/users/me", headers=investor["headers"])).status_code == 200

    resp = await client.patch(_role_url(investor["id"]), json={"role": "ADMIN"}, headers=admin["headers"])
    assert resp.status_code == 200, resp.text

    assert (await client.get("/users/me", headers=investor["headers"])).status_code == 401
    live = (
        await db_session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == uuid.UUID(investor["id"]), RefreshToken.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    assert live == []
    refreshed = await client.post("/auth/refresh", json={"refresh_token": investor["refresh_token"]})
    assert refreshed.status_code == 401


async def test_promoted_account_needs_a_second_factor_after_signing_in_again(client, make_admin, make_member):
    member = await make_member(role=None)
    admin = await make_admin()
    assert (
        await client.patch(_role_url(member["id"]), json={"role": "ADMIN"}, headers=admin["headers"])
    ).status_code == 200

    login = await client.post("/auth/login", json={"email": member["email"], "password": member["password"]})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.cookies.get('access_token')}"}

    resp = await client.get("/admin/audit-logs", headers=headers)
    assert resp.status_code == 403
    assert _code(resp) == "MFA_REQUIRED"
