"""The two-approval gate (migration 9f2c4a7d1e30).

A funding request needs BOTH:
  * the verification engine's word — GVerify KYB, plus a LOCKED score run;
  * an operator's word — loan_applications.admin_approval.

These tests hold the second half, and the point where the two meet: contract
creation. The RBAC cases matter as much as the happy paths — both endpoints
let one caller change another company's fate.
"""
import uuid

import pytest
from sqlalchemy import select

from app.gverify.models import (
    STATUS_APPROVED,
    STATUS_MANUAL_REVIEW,
    GVerifyKybVerification,
)
from app.lending.models import LoanApplication


@pytest.fixture
def make_submitted_application(
    make_user, make_project, make_loan_application, submit_loan_application
):
    """An SME with a submitted application — the state an operator reviews."""

    async def _make():
        sme = await make_user(role="SME")
        project = await make_project(sme)
        application = await make_loan_application(
            sme, project["id"], requested_amount="500000000"
        )
        resp = await submit_loan_application(sme, application["id"])
        assert resp.status_code == 200, resp.text
        return sme, project, application

    return _make


# --------------------------------------------------------------------------- #
# POST /admin/applications/{id}/decision
# --------------------------------------------------------------------------- #


async def test_admin_can_approve_an_application(
    client, make_admin, make_submitted_application, db_session
):
    _, _, application = await make_submitted_application()
    admin = await make_admin()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED", "note": "Revenue checks out"},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["admin_approval"] == "APPROVED"

    row = (
        await db_session.execute(
            select(LoanApplication).where(LoanApplication.id == uuid.UUID(application["id"]))
        )
    ).scalar_one()
    assert row.admin_approval == "APPROVED"
    # Who decided and when are recorded, not just the verdict — this is the
    # basis a contract gets created on.
    assert row.admin_approved_by == uuid.UUID(admin["id"])
    assert row.decided_at is not None
    assert row.decision_note == "Revenue checks out"


async def test_admin_can_reject_an_application(
    client, make_admin, make_submitted_application, db_session
):
    _, _, application = await make_submitted_application()
    admin = await make_admin()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED", "note": "Concentration too high"},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["admin_approval"] == "REJECTED"

    # The applicant-facing status moves with it. `admin_approval` is an
    # internal gate column and the SME dashboard renders `status`, so a
    # rejection that only set the gate left the applicant reading
    # "awaiting review" after the answer had already been given.
    row = (
        await db_session.execute(
            select(LoanApplication).where(LoanApplication.id == uuid.UUID(application["id"]))
        )
    ).scalar_one()
    assert row.status == "REJECTED"


async def test_approval_alone_does_not_tell_the_applicant_they_are_approved(
    client, make_admin, make_submitted_application, db_session
):
    """The operator's yes is one half of the gate, not the outcome.

    create_contract() still requires the engine's half, so moving `status` to
    APPROVED here would promise the applicant something the other half can
    still refuse. It stays where it was until there is an offer.
    """
    _, _, application = await make_submitted_application()
    admin = await make_admin()
    before = (
        await db_session.execute(
            select(LoanApplication).where(LoanApplication.id == uuid.UUID(application["id"]))
        )
    ).scalar_one().status

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED", "note": None},
        headers=admin["headers"],
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(LoanApplication).where(LoanApplication.id == uuid.UUID(application["id"]))
        )
    ).scalar_one()
    assert row.admin_approval == "APPROVED"
    assert row.status == before


async def test_a_decision_cannot_be_changed(
    client, make_admin, make_submitted_application
):
    """One-way by design: a contract may already have been created on the back
    of the first decision, and flipping it would rewrite that basis."""
    _, _, application = await make_submitted_application()
    admin = await make_admin()
    body = {"decision": "APPROVED"}

    first = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json=body,
        headers=admin["headers"],
    )
    assert first.status_code == 200

    second = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED"},
        headers=admin["headers"],
    )
    assert second.status_code == 409, second.text


async def test_decision_rejects_an_unknown_verdict(
    client, make_admin, make_submitted_application
):
    _, _, application = await make_submitted_application()
    admin = await make_admin()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "MAYBE"},
        headers=admin["headers"],
    )

    assert resp.status_code == 422


async def test_decision_on_an_unknown_application_is_404(client, make_admin):
    admin = await make_admin()
    resp = await client.post(
        f"/admin/applications/{uuid.uuid4()}/decision",
        json={"decision": "APPROVED"},
        headers=admin["headers"],
    )
    assert resp.status_code == 404


async def test_an_sme_cannot_approve_their_own_application(
    client, make_submitted_application
):
    """The whole point of the operator half — otherwise the applicant approves
    themselves and the gate is decorative."""
    sme, _, application = await make_submitted_application()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED"},
        headers=sme["headers"],
    )

    assert resp.status_code == 403, resp.text


async def test_an_investor_cannot_approve_an_application(
    client, make_user, make_submitted_application
):
    _, _, application = await make_submitted_application()
    investor = await make_user(role="INVESTOR")

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED"},
        headers=investor["headers"],
    )

    assert resp.status_code == 403, resp.text


async def test_an_anonymous_caller_cannot_approve_an_application(
    client, make_submitted_application
):
    _, _, application = await make_submitted_application()

    resp = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED"},
    )

    assert resp.status_code in (401, 403), resp.text


# --------------------------------------------------------------------------- #
# The gate itself: create_contract needs BOTH approvals
# --------------------------------------------------------------------------- #


async def test_contract_is_refused_while_admin_approval_is_pending(
    client, make_admin, make_submitted_application, run_and_lock_score
):
    """A LOCKED score run is the ENGINE's yes. On its own it is not enough."""
    _, _, application = await make_submitted_application()
    admin = await make_admin()
    score_run_id = await run_and_lock_score(admin, application["id"])

    resp = await client.post(
        "/contracts/",
        json={"application_id": application["id"], "score_run_id": score_run_id},
        headers=admin["headers"],
    )

    assert resp.status_code == 409, resp.text
    assert "admin approval" in resp.json()["detail"].lower()


async def test_contract_is_refused_when_the_admin_rejected(
    client, make_admin, make_submitted_application, run_and_lock_score
):
    _, _, application = await make_submitted_application()
    admin = await make_admin()
    score_run_id = await run_and_lock_score(admin, application["id"])
    await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "REJECTED"},
        headers=admin["headers"],
    )

    resp = await client.post(
        "/contracts/",
        json={"application_id": application["id"], "score_run_id": score_run_id},
        headers=admin["headers"],
    )

    assert resp.status_code == 409, resp.text


async def test_contract_is_created_once_both_approvals_are_in(
    client, make_admin, make_submitted_application, run_and_lock_score
):
    _, _, application = await make_submitted_application()
    admin = await make_admin()
    score_run_id = await run_and_lock_score(admin, application["id"])
    approved = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED"},
        headers=admin["headers"],
    )
    assert approved.status_code == 200, approved.text

    resp = await client.post(
        "/contracts/",
        json={"application_id": application["id"], "score_run_id": score_run_id},
        headers=admin["headers"],
    )

    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# POST /admin/kyb-verifications/{id}/resolve
# --------------------------------------------------------------------------- #


@pytest.fixture
def make_kyb_attempt(db_session):
    """A KYB attempt in a chosen state, written directly.

    Reaching MANUAL_REVIEW through the API means driving the provider mock to
    a borderline OCR confidence, which is tests/gverify's job. Here the state
    is the precondition, not the thing under test.
    """

    async def _make(user_id, status: str = STATUS_MANUAL_REVIEW):
        row = GVerifyKybVerification(
            user_id=uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
            status=status,
            document_type="COMPANY",
            business_name="CÔNG TY CỔ PHẦN FUNDLOK",
            tax_code="1501167629",
        )
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _make


async def test_admin_resolves_a_parked_verification(
    client, make_admin, make_user, make_kyb_attempt, db_session
):
    sme = await make_user(role="SME")
    attempt = await make_kyb_attempt(sme["id"])
    admin = await make_admin()

    resp = await client.post(
        f"/admin/kyb-verifications/{attempt.id}/resolve",
        json={"decision": "APPROVED", "note": "Certificate legible, registry matches"},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED"
    assert resp.json()["is_approved"] is True


async def test_resolving_records_the_reason_on_a_rejection(
    client, make_admin, make_user, make_kyb_attempt
):
    sme = await make_user(role="SME")
    attempt = await make_kyb_attempt(sme["id"])
    admin = await make_admin()

    resp = await client.post(
        f"/admin/kyb-verifications/{attempt.id}/resolve",
        json={"decision": "REJECTED", "note": "Tax code does not match the registry"},
        headers=admin["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "REJECTED"
    assert resp.json()["rejection_reason"] == "Tax code does not match the registry"


async def test_only_a_parked_verification_can_be_resolved(
    client, make_admin, make_user, make_kyb_attempt
):
    """An APPROVED attempt is the provider's own verdict. An operator must not
    quietly overwrite it — retrying creates a new attempt instead."""
    sme = await make_user(role="SME")
    attempt = await make_kyb_attempt(sme["id"], status=STATUS_APPROVED)
    admin = await make_admin()

    resp = await client.post(
        f"/admin/kyb-verifications/{attempt.id}/resolve",
        json={"decision": "REJECTED"},
        headers=admin["headers"],
    )

    assert resp.status_code == 409, resp.text


async def test_resolving_an_unknown_verification_is_404(client, make_admin):
    admin = await make_admin()
    resp = await client.post(
        f"/admin/kyb-verifications/{uuid.uuid4()}/resolve",
        json={"decision": "APPROVED"},
        headers=admin["headers"],
    )
    assert resp.status_code == 404


async def test_an_sme_cannot_resolve_their_own_verification(
    client, make_user, make_kyb_attempt
):
    """Otherwise the applicant approves their own KYB and the engine's
    borderline verdict means nothing."""
    sme = await make_user(role="SME")
    attempt = await make_kyb_attempt(sme["id"])

    resp = await client.post(
        f"/admin/kyb-verifications/{attempt.id}/resolve",
        json={"decision": "APPROVED"},
        headers=sme["headers"],
    )

    assert resp.status_code == 403, resp.text


async def test_an_anonymous_caller_cannot_resolve_a_verification(
    client, make_user, make_kyb_attempt
):
    sme = await make_user(role="SME")
    attempt = await make_kyb_attempt(sme["id"])

    resp = await client.post(
        f"/admin/kyb-verifications/{attempt.id}/resolve",
        json={"decision": "APPROVED"},
    )

    assert resp.status_code in (401, 403), resp.text


# --------------------------------------------------------------------------- #
# Both halves visible in one place
# --------------------------------------------------------------------------- #


async def test_project_detail_shows_both_halves_of_the_gate(
    client, make_admin, make_user, make_project, make_loan_application, make_kyb_attempt
):
    """The preview exists so an operator can see WHY an application is stuck
    without leaving the panel."""
    sme = await make_user(role="SME")
    project = await make_project(sme)
    await make_loan_application(sme, project["id"], requested_amount="500000000")
    await make_kyb_attempt(sme["id"])
    admin = await make_admin()

    resp = await client.get(
        f"/admin/projects/{project['id']}", headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["kyb"]["status"] == STATUS_MANUAL_REVIEW
    assert data["applications"][0]["admin_approval"] == "PENDING"
