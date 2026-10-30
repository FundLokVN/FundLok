"""Investors only see a project whose owner has saved a bank account (HANDOFF-04 Epic 0)."""
import uuid

from sqlalchemy import select, update

from app.direct_transfer.models import PaymentAccount
from app.lending.models import Project
from conftest import auth_headers


async def _activate(db_session, project_id: str) -> None:
    # An approved, locked application is what activates a project in the app
    # (test_project_activation.py); here only the visibility rule is under test.
    await db_session.execute(update(Project).where(Project.id == uuid.UUID(project_id)).values(status="ACTIVE"))
    await db_session.commit()


async def _listed(client, make_user, project_id: str) -> bool:
    investor = await make_user(role="INVESTOR")
    resp = await client.get("/projects/public", headers=auth_headers(investor))
    assert resp.status_code == 200, resp.text
    return any(p["id"] == project_id for p in resp.json())


async def _save_account(client, sme) -> dict:
    resp = await client.post(
        "/payment-accounts",
        json={"holder_type": "ENTITY", "bank_bin": "970407", "account_number": "19036789012", "account_name": "TEST CO"},
        headers=auth_headers(sme),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_project_is_hidden_until_the_owner_saves_a_bank_account(client, make_user, make_project, db_session):
    sme = await make_user(role="SME", bank_account=False)
    project = await make_project(sme)
    await _activate(db_session, project["id"])

    assert not await _listed(client, make_user, project["id"])

    account = await _save_account(client, sme)
    # An SME account with no approved KYB name to match saves PENDING (staff review). A pending account still
    # counts here: only a rejected one hides the project.
    assert account["status"] == "PENDING"
    assert await _listed(client, make_user, project["id"])


async def test_a_rejected_bank_account_does_not_make_the_project_visible(client, make_user, make_project, db_session):
    sme = await make_user(role="SME", bank_account=False)
    project = await make_project(sme)
    await _activate(db_session, project["id"])
    account = await _save_account(client, sme)
    await db_session.execute(
        update(PaymentAccount).where(PaymentAccount.id == uuid.UUID(account["id"])).values(status="REJECTED")
    )
    await db_session.commit()

    assert not await _listed(client, make_user, project["id"])


async def test_an_inactive_project_stays_hidden_even_with_a_bank_account(client, make_user, make_project, db_session):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    status = (await db_session.execute(select(Project.status).where(Project.id == uuid.UUID(project["id"])))).scalar_one()
    assert status != "ACTIVE"

    assert not await _listed(client, make_user, project["id"])
