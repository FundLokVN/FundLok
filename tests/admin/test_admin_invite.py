"""POST /admin/users/admins: a SYSTEM_ADMIN adds an operator.

The invitee gets an ADMIN account with no password and an emailed
single-use link to choose one, so the person creating the account never
knows the new admin's password. Granting admin access is SYSTEM_ADMIN only,
the same line PATCH /admin/users/{id}/status draws for admin-level accounts.
"""
import pytest
from sqlalchemy import select

from app.lending.models import AuditLog
from app.users.models import User

NEW_PASSWORD = "An0ther-Str0ng-Passw0rd!"


@pytest.fixture
def sent_invites(monkeypatch):
    """Capture invitation emails instead of sending them."""
    sent: list[dict] = []
    monkeypatch.setattr("app.admin.service.send_admin_invite_email", lambda **kw: sent.append(kw))
    return sent


def _body(email: str = "new.operator@example.com", full_name: str = "New Operator") -> dict:
    return {"email": email, "full_name": full_name}


async def test_system_admin_creates_an_admin_and_sends_an_invite(
    client, make_system_admin, db_session, sent_invites
):
    sysadmin = await make_system_admin()
    resp = await client.post("/admin/users/admins", json=_body(), headers=sysadmin["headers"])

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "new.operator@example.com"
    assert body["full_name"] == "New Operator"
    assert body["role"] == "ADMIN"
    assert body["status"] == "ACTIVE"
    assert body["email_verified"] is True

    row = (await db_session.execute(select(User).where(User.id == body["id"]))).scalar_one()
    assert row.password_hash is None

    assert len(sent_invites) == 1
    assert sent_invites[0]["to_email"] == "new.operator@example.com"
    assert sent_invites[0]["expires_hours"] == 24

    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == row.id, AuditLog.action == "ADMIN_INVITED")
        )
    ).scalar_one()
    assert str(audit.actor_id) == sysadmin["id"]


async def test_invitee_sets_a_password_with_the_link_and_signs_in_as_admin(
    client, make_system_admin, sent_invites
):
    sysadmin = await make_system_admin()
    created = await client.post("/admin/users/admins", json=_body(), headers=sysadmin["headers"])
    assert created.status_code == 201, created.text

    # No password yet: a password login cannot succeed.
    before = await client.post(
        "/auth/login", json={"email": "new.operator@example.com", "password": NEW_PASSWORD}
    )
    assert before.status_code == 401

    token = sent_invites[0]["token"]
    reset = await client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert reset.status_code == 200, reset.text

    login = await client.post("/auth/login", json={"email": "new.operator@example.com", "password": NEW_PASSWORD})
    assert login.status_code == 200, login.text
    me = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {login.cookies.get('access_token')}"}
    )
    assert me.status_code == 200
    assert me.json()["role"] == "ADMIN"


async def test_invite_link_works_only_once(client, make_system_admin, sent_invites):
    sysadmin = await make_system_admin()
    await client.post("/admin/users/admins", json=_body(), headers=sysadmin["headers"])
    token = sent_invites[0]["token"]

    first = await client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert first.status_code == 200
    again = await client.post("/auth/reset-password", json={"token": token, "new_password": "Y3t-An0ther-Passw0rd!"})
    assert again.status_code == 400


async def test_regular_admin_cannot_create_admins(client, make_admin, sent_invites):
    admin = await make_admin()
    resp = await client.post("/admin/users/admins", json=_body(), headers=admin["headers"])
    assert resp.status_code == 403
    assert sent_invites == []


async def test_member_cannot_create_admins(client, make_user, sent_invites):
    sme = await make_user(role="SME")
    resp = await client.post("/admin/users/admins", json=_body(), headers=sme["headers"])
    assert resp.status_code == 403
    assert sent_invites == []


async def test_existing_email_is_refused_case_insensitively(client, make_system_admin, make_user, sent_invites):
    sysadmin = await make_system_admin()
    member = await make_user(role="SME", email="taken.address@example.com")

    resp = await client.post(
        "/admin/users/admins", json=_body(email="Taken.Address@example.com"), headers=sysadmin["headers"]
    )
    assert resp.status_code == 409
    assert sent_invites == []

    # The existing member was not promoted.
    me = await client.get("/users/me", headers=member["headers"])
    assert me.json()["role"] == "SME"


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "full_name": "X"},
        {"email": "ok@example.com", "full_name": "   "},
        {"email": "ok@example.com"},
        # The endpoint only ever creates ADMIN; asking for more is refused, not ignored.
        {"email": "ok@example.com", "full_name": "X", "role": "SYSTEM_ADMIN"},
        {"email": "ok@example.com", "full_name": "X", "password": "chosen-by-someone-else"},
    ],
)
async def test_invalid_requests_are_rejected(client, make_system_admin, sent_invites, body):
    sysadmin = await make_system_admin()
    resp = await client.post("/admin/users/admins", json=body, headers=sysadmin["headers"])
    assert resp.status_code == 422
    assert sent_invites == []
