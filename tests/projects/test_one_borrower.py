"""One borrower across its projects (docs/specs/projects/one-borrower.md, D-016).

At most 100M outstanding per borrower and one listing per SME until its loan is repaid in full used to be checked
per project: an SME repaying 100M opened a second 50M listing under another project (Appendix M 2.6). The database
now holds one open project per business (however its tax code is written) and one live listing per SME account; the
code holds the rest: a valid tax code, the business the SME verified, no new project while one is in progress.

v1.1 (D-040, D-050): a project is CLOSED when its request ends, so a business that repaid in full, or whose round
failed or request was refused, borrows again on a new project under its own (verified) tax code.
"""
import datetime as dt
import uuid

import pytest

from sqlalchemy import select, update

from app.direct_transfer import close, repayment
from app.direct_transfer.models import FundingWindow, Tranche
from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification
from app.lending.models import AuditLog, LoanApplication, Project
from app.models import RepaymentDay
from app.projects.borrower import close_project_if_finished
from app.repayment_schedule.models import Facility
from app.users.models import User
from conftest import attach_signed_listing_documents as conftest_signed, auth_headers
from tests.admin.test_repayment_receipts import _repaying
from tests.direct_transfer.helpers import at, close_now, fund_window, make_world, open_window
from tests.direct_transfer.test_commit_then_accept import SME_DEADLINE, _awaiting_sme, _decide
from tests.market.test_funding_flow import _project
from tests.tax_codes import fake_tax_code

M = 1_000_000
LOAN = {"requested_amount": 80_000_000, "duration_months": 4, "repayment_preference": "MONTHLY"}


async def _create(client, sme, tax_id, **extra):
    return await client.post(
        "/projects",
        json={"legal_name": f"Co {uuid.uuid4().hex[:6]}", "tax_id": tax_id} | extra,
        headers=auth_headers(sme),
    )


def _code(resp) -> str | None:
    return resp.headers.get("X-Error-Code")


async def _verified(db_session, sme, tax_code):
    db_session.add(
        GVerifyKybVerification(
            user_id=uuid.UUID(sme["id"]), status=STATUS_APPROVED, document_type="COMPANY", tax_code=tax_code,
            business_name="CONG TY VERIFIED",
        )
    )
    await db_session.commit()


async def _open(client, admin, project_id):
    return await client.post(f"/admin/projects/{project_id}/funding-window", headers=admin["headers"])


async def _status(db_session, project_id) -> str:
    return (await db_session.execute(select(Project.status).where(Project.id == project_id))).scalar_one()


async def _closings(db_session, project_id) -> list[dict]:
    return [
        row.after_state
        for row in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.entity_id == project_id, AuditLog.action == "PROJECT_CLOSED")
            )
        ).scalars()
    ]


async def _verified_world(db_session, make_user, **kwargs):
    """An SME signed in over HTTP, verified as the business of a project with an approved request and a listing
    (tests/direct_transfer/helpers.make_world)."""
    sme = await make_user(role="SME")
    world = await make_world(db_session, sme=await db_session.get(User, uuid.UUID(sme["id"])), **kwargs)
    await _verified(db_session, sme, world.project.tax_id)
    return sme, world


# --------------------------------------------------------------------------- #
# the tax code
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint", ["/projects", "/sme/businesses"])
async def test_an_unverified_sme_cannot_reserve_a_business_tax_code(endpoint, client, db_session, make_user):
    unverified = await make_user(role="SME")
    owner = await make_user(role="SME")
    code = fake_tax_code()
    await _verified(db_session, owner, code)
    payload = {"legal_name": "Verified business", "tax_id": code}

    denied = await client.post(endpoint, json=payload, headers=auth_headers(unverified))
    assert denied.status_code == 409 and _code(denied) == "BUSINESS_VERIFICATION_REQUIRED"
    created = await client.post(endpoint, json=payload, headers=auth_headers(owner))
    assert created.status_code == 201, created.text


async def test_a_project_needs_a_valid_tax_code_kept_in_one_form(client, db_session, make_user):
    for bad in (None, "", "TAX-123", "12345", "0123456789-01", "01234567890"):
        sme = await make_user(role="SME")
        resp = await _create(client, sme, bad)
        assert resp.status_code == 422 and _code(resp) == "INVALID_TAX_CODE", (bad, resp.text)

    company, branch = fake_tax_code(), fake_tax_code()
    sme = await make_user(role="SME")
    await _verified(db_session, sme, company)
    spaced = await _create(client, sme, f" {company[:4]} {company[4:]} ")
    assert spaced.status_code == 201, spaced.text
    assert spaced.json()["tax_id"] == company
    sme = await make_user(role="SME")
    await _verified(db_session, sme, branch)
    dashless = await _create(client, sme, f"{branch}002")
    assert dashless.status_code == 201, dashless.text
    assert dashless.json()["tax_id"] == f"{branch}-002"
    sme = await make_user(role="SME")
    await _verified(db_session, sme, "079123456789")
    household = await _create(client, sme, "079123456789")
    assert household.status_code == 201 and household.json()["tax_id"] == "079123456789"

    stored = (
        await db_session.execute(select(Project.tax_business).where(Project.tax_id == f"{branch}-002"))
    ).scalar_one()
    assert stored == branch  # a branch is its company


async def test_a_business_has_one_project_however_its_tax_code_is_written(client, db_session, make_user):
    code = fake_tax_code()
    sme = await make_user(role="SME")
    await _verified(db_session, sme, code)
    assert (await _create(client, sme, code)).status_code == 201

    for variant in (code, f"{code}-001", f"{code[:5]} {code[5:]}"):
        sme = await make_user(role="SME")
        await _verified(db_session, sme, code)
        resp = await _create(client, sme, variant)
        assert resp.status_code == 409 and _code(resp) == "DUPLICATE_TAX_CODE", (variant, resp.text)


async def test_a_project_must_be_the_business_the_sme_verified(client, db_session, make_user):
    sme = await make_user(role="SME")
    verified = fake_tax_code()
    await _verified(db_session, sme, verified)

    other = await _create(client, sme, fake_tax_code())
    assert other.status_code == 409 and _code(other) == "TAX_CODE_NOT_VERIFIED_BUSINESS"

    # Its own branch is the business it verified.
    own = await _create(client, sme, f"{verified}-003")
    assert own.status_code == 201, own.text


# --------------------------------------------------------------------------- #
# one project in progress
# --------------------------------------------------------------------------- #
async def test_an_sme_cannot_start_a_second_project_while_one_is_in_progress(client, db_session, make_user):
    sme = await make_user(role="SME")
    code = fake_tax_code()
    await _verified(db_session, sme, code)
    first = await _create(client, sme, code, loan_application=LOAN)
    assert first.status_code == 201, first.text

    second = await _create(client, sme, code)
    assert second.status_code == 409 and _code(second) == "SME_HAS_OPEN_PROJECT"

    # Once the first ends (here: its application was rejected), another can start.
    await db_session.execute(
        update(LoanApplication)
        .where(LoanApplication.project_id == uuid.UUID(first.json()["id"]))
        .values(status="REJECTED", admin_approval="REJECTED")
    )
    await close_project_if_finished(db_session, uuid.UUID(first.json()["id"]), "REJECTED")
    await db_session.commit()
    third = await _create(client, sme, code)
    assert third.status_code == 201, third.text


# --------------------------------------------------------------------------- #
# borrowing again (v1.1): the same business, its own tax code, a new project
# --------------------------------------------------------------------------- #
async def test_business_can_borrow_again_after_full_repayment(
    client, db_session, make_admin, make_user, submit_loan_application, attach_required_documents
):
    sme, world = await _verified_world(db_session, make_user, target=20 * M, term=1)
    code = world.project.tax_id
    await fund_window(world, [20 * M])
    window = await close_now(world, now=at(days=1))
    assert window.status == "REPAYING"

    # Still repaying: no new request, however it is asked for.
    blocked = await _create(client, sme, code)
    assert blocked.status_code == 409 and _code(blocked) == "SME_HAS_OPEN_PROJECT", blocked.text
    assert await _status(db_session, world.project.id) == "ACTIVE"

    # Every day paid: the facility settles, the round is REPAID, and the project closes with it.
    day_ids = (
        await db_session.execute(
            select(RepaymentDay.id).join(Tranche, Tranche.id == RepaymentDay.tranche_id).where(Tranche.window_id == window.id)
        )
    ).scalars().all()
    for day_id in day_ids:
        await repayment.match_day(db_session, day_id=day_id, staff_id=world.admin.id, reason="paid", now=at(days=60))
    await db_session.refresh(window)
    assert window.status == "REPAID"
    assert (await db_session.get(Facility, window.facility_id)).status == "SETTLED"
    assert await _status(db_session, world.project.id) == "CLOSED"
    assert await _closings(db_session, world.project.id) == [{"status": "CLOSED", "reason": "REPAID"}]
    await db_session.commit()

    # The new request: same business, same tax code, started, submitted and approved.
    new = await _create(client, sme, code, loan_application=LOAN)
    assert new.status_code == 201, new.text
    assert new.json()["tax_id"] == code and new.json()["status"] == "DRAFT"
    application_id = new.json()["loan_application"]["id"]
    submitted = await submit_loan_application(sme, application_id)
    assert submitted.status_code == 200, submitted.text
    await attach_required_documents(application_id)
    admin = await make_admin()
    approved = await client.post(
        f"/admin/applications/{application_id}/decision", json={"decision": "APPROVED", "note": "ok"},
        headers=admin["headers"],
    )
    assert approved.status_code == 200 and approved.json()["admin_approval"] == "APPROVED", approved.text

    # The dashboard gets the new request first and keeps the repaid one after it.
    mine = (await client.get("/projects", headers=auth_headers(sme))).json()
    assert [(p["id"], p["status"]) for p in mine] == [(new.json()["id"], "DRAFT"), (str(world.project.id), "CLOSED")]


async def test_business_can_reapply_straight_away_after_a_failed_round(client, db_session, make_user):
    sme, world = await _verified_world(db_session, make_user)
    await _awaiting_sme(world, [40 * M])

    blocked = await _create(client, sme, world.project.tax_id)
    assert blocked.status_code == 409 and _code(blocked) == "SME_HAS_OPEN_PROJECT", blocked.text

    declined = await _decide(world, "DECLINE", now=SME_DEADLINE - dt.timedelta(hours=1))
    assert declined.status == "FAILED"
    assert await _status(db_session, world.project.id) == "CLOSED"
    assert await _closings(db_session, world.project.id) == [{"status": "CLOSED", "reason": "ROUND_FAILED"}]
    await db_session.commit()

    again = await _create(client, sme, world.project.tax_id, loan_application=LOAN)
    assert again.status_code == 201, again.text


async def test_project_closes_when_its_round_fails_below_the_minimum_or_is_cancelled(db_session):
    below = await make_world(db_session)
    await fund_window(below, [10 * M])  # 10% of the target: below the minimum raise
    assert (await close_now(below, now=at(days=14))).status == "FAILED"
    assert await _status(db_session, below.project.id) == "CLOSED"

    cancelled = await make_world(db_session)
    await open_window(cancelled)
    await close.cancel_window(db_session, window_id=cancelled.window.id, staff_id=cancelled.admin.id, reason="KYB", now=at(hours=1))
    assert await _status(db_session, cancelled.project.id) == "CLOSED"
    assert await _closings(db_session, cancelled.project.id) == [{"status": "CLOSED", "reason": "ROUND_CANCELLED"}]


async def test_business_can_reapply_after_its_application_is_rejected(
    client, db_session, make_admin, make_user, submit_loan_application
):
    sme = await make_user(role="SME")
    code = fake_tax_code()
    await _verified(db_session, sme, code)
    first = await _create(client, sme, code, loan_application=LOAN)
    assert first.status_code == 201, first.text
    project_id = uuid.UUID(first.json()["id"])
    assert (await submit_loan_application(sme, first.json()["loan_application"]["id"])).status_code == 200

    # Under review: a second request for the same business is refused.
    second = await _create(client, sme, code)
    assert second.status_code == 409 and _code(second) == "SME_HAS_OPEN_PROJECT"

    admin = await make_admin()
    rejected = await client.post(
        f"/admin/applications/{first.json()['loan_application']['id']}/decision",
        json={"decision": "REJECTED", "note": "revenue does not support it"}, headers=admin["headers"],
    )
    assert rejected.status_code == 200, rejected.text
    assert await _status(db_session, project_id) == "CLOSED"

    # Closing again changes nothing and writes no second audit row.
    assert await close_project_if_finished(db_session, project_id, "REJECTED") is False
    assert await _closings(db_session, project_id) == [{"status": "CLOSED", "reason": "REJECTED"}]

    again = await _create(client, sme, code)
    assert again.status_code == 201, again.text
    # Another account for the same business, while the new project is open: the database refuses it.
    other_sme = await make_user(role="SME")
    await _verified(db_session, other_sme, code)
    other = await _create(client, other_sme, f"{code}-002")
    assert other.status_code == 409 and _code(other) == "DUPLICATE_TAX_CODE", other.text


async def test_an_sme_repaying_a_loan_can_neither_start_nor_list_another_project(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    # The QA case: an investment repaying on the SME's first project.
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)

    # No new project while it repays.
    code = (await db_session.get(Project, uuid.UUID(str(s["project"])))).tax_id
    await _verified(db_session, s["sme"], code)
    blocked = await _create(client, s["sme"], code)
    assert blocked.status_code == 409 and _code(blocked) == "SME_HAS_OPEN_PROJECT"

    # A second historical project had no KYB check when it was created.
    kyb = (await db_session.execute(select(GVerifyKybVerification).where(
        GVerifyKybVerification.user_id == uuid.UUID(s["sme"]["id"]),
    ))).scalars().all()
    for attempt in kyb:
        await db_session.delete(attempt)
    await db_session.commit()
    second = await _project(db_session, s["sme"])
    refused = await _open(client, s["admin"], second)
    assert refused.status_code == 409 and _code(refused) == "SME_HAS_LIVE_LISTING", refused.text
    # The database refused the insert; the request's session (shared with the test here) is rolled back.
    await db_session.rollback()
    assert (
        await db_session.execute(select(FundingWindow.id).where(FundingWindow.project_id == second))
    ).first() is None

    # Once the first loan is repaid in full, the second can list.
    await db_session.execute(
        update(FundingWindow).where(FundingWindow.project_id == s["project"]).values(status="REPAID")
    )
    await db_session.commit()
    opened = await _open(client, s["admin"], second)
    assert opened.status_code == 200, opened.text


async def test_a_listing_cannot_open_for_a_business_other_than_the_one_verified(
    client, db_session, make_admin, make_user
):
    admin = await make_admin()
    sme = await make_user(role="SME")
    project = await _project(db_session, sme)  # created before the SME verified its business
    await _verified(db_session, sme, fake_tax_code())

    resp = await _open(client, admin, project)

    assert resp.status_code == 409 and _code(resp) == "TAX_CODE_NOT_VERIFIED_BUSINESS", resp.text


async def test_the_database_holds_one_live_listing_per_sme(client, db_session, make_admin, make_user, make_eligible_investor):
    """Whatever path inserts it, a second live window for the same SME account is refused by the database."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    db_session.expire_all()
    window = (
        await db_session.execute(select(FundingWindow).where(FundingWindow.project_id == s["project"]))
    ).scalar_one()
    second = await _project(db_session, s["sme"])
    clone = {c.name: getattr(window, c.name) for c in FundingWindow.__table__.columns if c.name != "id"}
    db_session.add(
        FundingWindow(
            **clone | {"project_id": second, "listing_id": uuid.uuid4(), "idempotency_key": str(uuid.uuid4()),
                       "window_start": dt.datetime.now(dt.timezone.utc)}
        )
    )
    with pytest.raises(IntegrityError, match="uq_funding_windows_one_live_per_sme"):
        await db_session.flush()
    await db_session.rollback()


# --------------------------------------------------------------------------- #
# repeat request: only the amount and term are asked again
# --------------------------------------------------------------------------- #
async def _repeat(client, sme, **loan):
    return await client.post("/projects/repeat", json=loan, headers=auth_headers(sme))


async def test_a_repeat_request_copies_the_last_approved_request_and_goes_to_review(
    client, db_session, make_admin, make_user, attach_required_documents, monkeypatch
):
    from app.lending.models import LoanApplicationDocument
    from app.uploads.models import CicSummary

    copies = []
    monkeypatch.setattr("app.projects.service.copy_object", lambda source, target: copies.append((source, target)))
    sme, world = await _verified_world(db_session, make_user, term=2)
    world.application.self_reported_figures = {"revenue_last_12m": 1_200_000_000}
    world.application.guarantor_address = "12 Nguyễn Huệ, Quận 1"
    await attach_required_documents(world.application.id)
    cic = (
        await db_session.execute(
            select(LoanApplicationDocument).where(
                LoanApplicationDocument.loan_application_id == world.application.id,
                LoanApplicationDocument.document_type == "cic_report",
            )
        )
    ).scalar_one()
    db_session.add(CicSummary(
        loan_application_id=world.application.id, document_id=cic.id, parser_version="cic-1", subject_type="business",
        score=612, report={"debt": 0},
    ))
    await conftest_signed(db_session, world.application.id)
    await _awaiting_sme(world, [40 * M])

    # Still in progress: no repeat either.
    early = await _repeat(client, sme, requested_amount=70_000_000)
    assert early.status_code == 409 and _code(early) == "SME_HAS_OPEN_PROJECT", early.text

    await _decide(world, "DECLINE", now=SME_DEADLINE - dt.timedelta(hours=1))
    await db_session.commit()

    resp = await _repeat(client, sme, requested_amount=70_000_000)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tax_id"] == world.project.tax_id and body["legal_name"] == world.project.legal_name
    application = body["loan_application"]
    assert application["status"] == "SUBMITTED" and application["requested_amount"] == 70_000_000
    assert application["duration_months"] == 2  # the term carries over when none is given
    # The evidence is copied, each file to the new application's own key; the signed listing documents are not.
    copied = sorted(d["document_type"] for d in application["documents"])
    from app.admin.service import REQUIRED_DOCUMENT_TYPES
    assert copied == sorted(REQUIRED_DOCUMENT_TYPES)
    assert len(copies) == len(REQUIRED_DOCUMENT_TYPES)
    assert all(target.startswith(f"application_documents/{sme['id']}/{application['id']}/") for _, target in copies)
    new = await db_session.get(LoanApplication, uuid.UUID(application["id"]))
    assert new.self_reported_figures == {"revenue_last_12m": 1_200_000_000}
    assert new.guarantor_address == "12 Nguyễn Huệ, Quận 1"
    summary = (
        await db_session.execute(select(CicSummary).where(CicSummary.loan_application_id == new.id))
    ).scalar_one()
    assert summary.score == 612 and summary.document_id != cic.id

    # Staff review it as any request: nothing is missing, so it can be approved.
    admin = await make_admin()
    approved = await client.post(
        f"/admin/applications/{application['id']}/decision", json={"decision": "APPROVED", "note": "ok"},
        headers=admin["headers"],
    )
    assert approved.status_code == 200, approved.text


async def test_a_repeat_request_takes_a_new_term_and_needs_an_approved_request_to_copy(
    client, db_session, make_admin, make_user, submit_loan_application, monkeypatch
):
    monkeypatch.setattr("app.projects.service.copy_object", lambda source, target: None)
    sme = await make_user(role="SME")
    code = fake_tax_code()
    await _verified(db_session, sme, code)
    first = await _create(client, sme, code, loan_application=LOAN)
    assert (await submit_loan_application(sme, first.json()["loan_application"]["id"])).status_code == 200
    admin = await make_admin()
    await client.post(
        f"/admin/applications/{first.json()['loan_application']['id']}/decision",
        json={"decision": "REJECTED", "note": "no"}, headers=admin["headers"],
    )

    # Only a rejected request behind it: the full application, not a copy.
    refused = await _repeat(client, sme, requested_amount=50_000_000, duration_months=3)
    assert refused.status_code == 409 and _code(refused) == "NO_APPROVED_REQUEST", refused.text

    # The amount and term are checked like any request's.
    sme2, world = await _verified_world(db_session, make_user)
    await _awaiting_sme(world, [40 * M])
    await _decide(world, "DECLINE", now=SME_DEADLINE - dt.timedelta(hours=1))
    await db_session.commit()
    bad = await _repeat(client, sme2, requested_amount=50_000_000, duration_months=12)
    assert bad.status_code == 422, bad.text
    ok = await _repeat(client, sme2, requested_amount=50_000_000, duration_months=3)
    assert ok.status_code == 201 and ok.json()["loan_application"]["duration_months"] == 3, ok.text
