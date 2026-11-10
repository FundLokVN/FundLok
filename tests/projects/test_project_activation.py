"""A project goes on the marketplace (ACTIVE) once BOTH approvals land and the
SME has signed its listing documents.

The two-approval gate: the operator's (admin_approval == APPROVED) and the
engine's (a LOCKED score run). Nothing used to set a project ACTIVE: creation
leaves it DRAFT, and ACTIVE was only the column's server default, so a
genuinely approved SME never reached /projects/public.

Either half can come second, so both paths are tested: approve then lock, and
lock then approve. Those use applications whose listing documents are already
on file (make_submitted_application's default); the signatures as the last
part of the gate are tested at the end, and the signing itself in
tests/loans/test_listing_signing.py.
"""
import uuid

import pytest
from sqlalchemy import select

from app.lending.models import AuditLog, Project, ScoreRun
from conftest import auth_headers
from tests.admin.test_admin_approval import make_submitted_application  # noqa: F401


@pytest.fixture
def add_run(db_session):
    async def _add(application_id, status="READY"):
        run = ScoreRun(application_id=uuid.UUID(application_id), status=status, final_grade_precise=72.0)
        db_session.add(run)
        await db_session.commit()
        await db_session.refresh(run)
        return run

    return _add


async def _project_status(db_session, project_id) -> str:
    db_session.expire_all()
    return (
        await db_session.execute(select(Project.status).where(Project.id == uuid.UUID(project_id)))
    ).scalar_one()


async def _approve(client, admin, application_id):
    resp = await client.post(
        f"/admin/applications/{application_id}/decision",
        json={"decision": "APPROVED", "note": "ok"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _lock(client, admin, run_id):
    resp = await client.post(f"/underwriting/score-runs/{run_id}/approve", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp


async def _listed(client, make_user, project_id) -> bool:
    investor = await make_user(role="INVESTOR")
    resp = await client.get("/projects/public", headers=auth_headers(investor))
    assert resp.status_code == 200, resp.text
    return any(p["id"] == project_id for p in resp.json())


async def test_a_new_project_starts_as_draft_and_is_not_listed(
    client, make_user, make_submitted_application, db_session  # noqa: F811
):
    _, project, _ = await make_submitted_application()

    assert await _project_status(db_session, project["id"]) == "DRAFT"
    assert not await _listed(client, make_user, project["id"])


async def test_lock_then_approve_activates_the_project(
    client, make_user, make_admin, make_submitted_application, add_run, db_session  # noqa: F811
):
    _, project, application = await make_submitted_application()
    admin = await make_admin()
    run = await add_run(application["id"])
    await _lock(client, admin, run.id)
    assert await _project_status(db_session, project["id"]) == "DRAFT", "one half is not enough"

    await _approve(client, admin, application["id"])

    assert await _project_status(db_session, project["id"]) == "ACTIVE"
    assert await _listed(client, make_user, project["id"])


async def test_approve_then_lock_activates_the_project(
    client, make_user, make_admin, make_submitted_application, add_run, db_session  # noqa: F811
):
    _, project, application = await make_submitted_application()
    admin = await make_admin()
    run_id = (await add_run(application["id"])).id
    await _approve(client, admin, application["id"])
    assert await _project_status(db_session, project["id"]) == "DRAFT", "one half is not enough"

    await _lock(client, admin, run_id)

    assert await _project_status(db_session, project["id"]) == "ACTIVE"
    assert await _listed(client, make_user, project["id"])


async def test_an_approval_with_only_a_ready_run_does_not_activate(
    client, make_admin, make_submitted_application, add_run, db_session  # noqa: F811
):
    _, project, application = await make_submitted_application()
    admin = await make_admin()
    await add_run(application["id"], status="READY")

    await _approve(client, admin, application["id"])

    assert await _project_status(db_session, project["id"]) == "DRAFT"


async def test_a_rejection_never_activates(
    client, make_admin, make_submitted_application, add_run, db_session  # noqa: F811
):
    _, project, application = await make_submitted_application()
    admin = await make_admin()
    await add_run(application["id"], status="LOCKED")

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED", "note": "no"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    assert await _project_status(db_session, project["id"]) == "DRAFT"


@pytest.mark.parametrize("taken_down", ["SUSPENDED", "CLOSED", "ARCHIVED"])
async def test_a_project_taken_off_the_marketplace_is_not_put_back(
    client, make_admin, make_submitted_application, add_run, db_session, taken_down  # noqa: F811
):
    _, project, application = await make_submitted_application()
    admin = await make_admin()
    row = (await db_session.execute(select(Project).where(Project.id == uuid.UUID(project["id"])))).scalar_one()
    row.status = taken_down
    await db_session.commit()
    await add_run(application["id"], status="LOCKED")

    await _approve(client, admin, application["id"])

    assert await _project_status(db_session, project["id"]) == taken_down


async def test_activation_is_audited(
    client, make_admin, make_submitted_application, add_run, db_session  # noqa: F811
):
    _, project, application = await make_submitted_application()
    admin = await make_admin()
    run = await add_run(application["id"], status="LOCKED")

    await _approve(client, admin, application["id"])

    row = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "PROJECT_ACTIVATED"))
    ).scalar_one()
    assert str(row.entity_id) == project["id"]
    assert str(row.actor_id) == admin["id"]
    assert row.after_state == {
        "status": "ACTIVE",
        "application_id": application["id"],
        "score_run_id": str(run.id),
    }


async def test_both_approvals_without_the_signed_documents_do_not_activate(
    client, make_user, make_admin, make_submitted_application, add_run, db_session  # noqa: F811
):
    # Signing publishes the listing (Contract 3): approved and locked is not yet live.
    _, project, application = await make_submitted_application(signed=False)
    admin = await make_admin()
    run = await add_run(application["id"])
    await _approve(client, admin, application["id"])
    await _lock(client, admin, run.id)

    assert await _project_status(db_session, project["id"]) == "DRAFT"
    assert not await _listed(client, make_user, project["id"])


async def test_the_second_signature_activates_an_approved_project(
    client, make_user, make_admin, make_submitted_application, add_run, db_session  # noqa: F811
):
    from app.projects.service import activate_project_if_approved
    from conftest import attach_signed_listing_documents

    _, project, application = await make_submitted_application(signed=False)
    admin = await make_admin()
    run = await add_run(application["id"])
    await _approve(client, admin, application["id"])
    await _lock(client, admin, run.id)

    # What the SME's second signed upload does (app/loans/signing.record_signed).
    await attach_signed_listing_documents(db_session, application["id"])
    assert await activate_project_if_approved(db_session, uuid.UUID(application["id"]))
    await db_session.commit()

    assert await _project_status(db_session, project["id"]) == "ACTIVE"
