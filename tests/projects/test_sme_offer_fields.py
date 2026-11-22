"""DRR, revenue share and the guarantor on the SME's offer (docs/specs/projects/sme-offer-fields.md section 8).

The offer is `loan_application.approval` of GET /projects: the same figures the Standing Offer prints and the
investor listing shows, now on the SME's own screen.
"""
from uuid import UUID

from conftest import auth_headers
from sqlalchemy import func, select, update

from app.gverify.models import STATUS_APPROVED, GVerifyVerification
from app.lending.models import ScoreRun
from app.loans.agreement.terms import locked_terms
from app.projects.service import locked_repayment_rates
from tests.projects.test_approval_summary import _mine, _scored_and_approved

EKYC_NAME = "NGUYEN VAN GUARANTOR"
ID_NUMBER = "079123456789"


async def _approved(client, make_user, make_admin, make_project, make_loan_application,
                    submit_loan_application, attach_required_documents, **kw):
    return await _scored_and_approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents, **kw,
    )


async def _run(db_session, run) -> ScoreRun:
    return (await db_session.execute(select(ScoreRun).where(ScoreRun.id == UUID(run["id"])))).scalar_one()


async def test_the_offer_carries_drr_revenue_share_and_the_guarantor_name(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents,
):
    sme, project, _ = await _approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    approval = (await _mine(client, sme, project["id"]))["approval"]
    assert approval["daily_repayment_rate"] is not None and approval["revenue_share"] is not None
    assert approval["guarantor_name"]


async def test_revenue_share_is_the_locked_runs_daily_repayment_rate_as_a_fraction(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents, db_session,
):
    sme, project, run = await _approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    stored = float((await _run(db_session, run)).daily_repayment_rate)
    approval = (await _mine(client, sme, project["id"]))["approval"]
    assert approval["daily_repayment_rate"] == stored == approval["revenue_share"]
    assert 0 < stored < 1  # a fraction (0.085 is 8.5%), never a percentage


async def test_the_offer_figures_match_the_standing_offer_and_the_investor_listing(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents, db_session,
):
    sme, project, run = await _approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    # The listing and the Standing Offer read the LOCKED run; the offer reads the LOCKED run, else the latest graded.
    await db_session.execute(
        update(ScoreRun).where(ScoreRun.id == UUID(run["id"])).values(status="LOCKED", locked_at=func.now())
    )
    await db_session.commit()
    application = await _mine(client, sme, project["id"])
    approval = application["approval"]
    application_id = UUID(application["id"])
    listing = await locked_repayment_rates(db_session, [application_id])  # what the investor listing shows
    assert listing[application_id] == approval["revenue_share"]
    from app.lending.models import LoanApplication

    await db_session.execute(
        update(LoanApplication).where(LoanApplication.id == application_id).values(
            duration_months=approval["duration_months"]  # a real request carries the term it was priced on
        )
    )
    await db_session.commit()
    row = await db_session.get(LoanApplication, application_id)
    await db_session.refresh(row)
    terms = await locked_terms(db_session, row)  # what the Standing Offer prints
    assert float(terms.revenue_share) == approval["revenue_share"]


async def test_a_run_scored_before_the_rate_was_stored_gives_null_drr_and_revenue_share(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents, db_session,
):
    sme, project, run = await _approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    await db_session.execute(update(ScoreRun).where(ScoreRun.id == UUID(run["id"])).values(daily_repayment_rate=None))
    await db_session.commit()
    approval = (await _mine(client, sme, project["id"]))["approval"]
    assert approval["daily_repayment_rate"] is None and approval["revenue_share"] is None
    assert approval["business_score"] is not None  # the rest of the offer still loads


async def test_the_guarantor_name_is_the_approved_ekyc_name_else_the_account_name(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents, db_session,
):
    sme, project, _ = await _approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    assert (await _mine(client, sme, project["id"]))["approval"]["guarantor_name"] == "Test User"  # no eKYC yet
    db_session.add(
        GVerifyVerification(
            user_id=UUID(sme["id"]), verification_type="KYC", status=STATUS_APPROVED,
            full_name=EKYC_NAME, person_number=ID_NUMBER,
        )
    )
    await db_session.commit()
    assert (await _mine(client, sme, project["id"]))["approval"]["guarantor_name"] == EKYC_NAME


async def test_the_guarantor_id_number_and_address_are_never_in_the_offer(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents, db_session,
):
    sme, project, _ = await _approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    db_session.add(
        GVerifyVerification(
            user_id=UUID(sme["id"]), verification_type="KYC", status=STATUS_APPROVED,
            full_name=EKYC_NAME, person_number=ID_NUMBER,
        )
    )
    await db_session.commit()
    approval = (await _mine(client, sme, project["id"]))["approval"]
    assert ID_NUMBER not in str(approval)
    assert not {"guarantor_id_number", "guarantor_address", "id_number", "address"} & set(approval)


async def test_an_unapproved_request_has_no_offer_and_so_no_new_fields(
    client, make_user, make_project, make_loan_application, submit_loan_application,
):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    application = await make_loan_application(sme, project["id"])
    await submit_loan_application(sme, application["id"])
    assert (await _mine(client, sme, project["id"]))["approval"] is None


async def test_existing_offer_fields_are_unchanged(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents,
):
    sme, project, _ = await _approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    approval = (await _mine(client, sme, project["id"]))["approval"]
    for key in (
        "approved_at", "business_score", "reference_rate_pct", "duration_months", "total_repayment_vnd",
        "estimated_daily_repayment_vnd", "engine_version", "params_version",
    ):
        assert key in approval and approval[key] is not None, key
    assert approval["estimated_daily_repayment_vnd"] == approval["total_repayment_vnd"] // (approval["duration_months"] * 21)
