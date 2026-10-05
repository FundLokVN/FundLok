"""What the applicant sees once an operator approves their request.

An approval does not move `status` (it is one half of the two-approval gate),
so the SME dashboard learns of it through `loan_application.approval`: the
stored business score and reference rate of the run the decision rests on,
and the repayment they imply (INV-2). Never on the public listing.
"""
from decimal import Decimal

from conftest import auth_headers
from tests.underwriting.test_score_run_persistence import FULL_FINANCIALS


async def _scored_and_approved(client, make_user, make_admin, make_project, make_loan_application,
                               submit_loan_application, attach_required_documents, *, score=True):
    sme, admin = await make_user(role="SME"), await make_admin()
    project = await make_project(sme)
    application = await make_loan_application(sme, project["id"], requested_amount="80000000")
    assert (await submit_loan_application(sme, application["id"])).status_code == 200
    run = None
    if score:
        put = await client.put(
            f"/underwriting/applications/{application['id']}/financials",
            json=FULL_FINANCIALS, headers=auth_headers(admin),
        )
        assert put.status_code == 200, put.text
        resp = await client.post(
            "/underwriting/score-runs", json={"application_id": application["id"], "mode": None},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 201, resp.text
        run = resp.json()
    await attach_required_documents(application["id"])
    decided = await client.post(
        f"/admin/applications/{application['id']}/decision",
        json={"decision": "APPROVED", "note": None}, headers=auth_headers(admin),
    )
    assert decided.status_code == 200, decided.text
    return sme, project, run


async def _mine(client, sme, project_id):
    projects = (await client.get("/projects", headers=auth_headers(sme))).json()
    return next(p for p in projects if p["id"] == project_id)["loan_application"]


async def test_an_approved_request_shows_its_score_rate_and_repayment(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents,
):
    sme, project, run = await _scored_and_approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    application = await _mine(client, sme, project["id"])
    approval = application["approval"]

    assert approval["approved_at"] is not None
    assert approval["business_score"] is not None and 0 <= approval["business_score"] <= 100
    assert 12 <= approval["reference_rate_pct"] <= 20
    assert approval["duration_months"] == FULL_FINANCIALS["duration_months"]  # the term it was priced on

    # INV-2: flat interest on the principal, charged once.
    months = approval["duration_months"]
    rate = Decimal(str(approval["reference_rate_pct"])) / 100
    expected = Decimal(80_000_000) * (1 + rate * months / 12)
    assert abs(approval["total_repayment_vnd"] - int(expected)) <= 80_000_000 * 0.0001 * months
    assert approval["estimated_daily_repayment_vnd"] == round(approval["total_repayment_vnd"] / (months * 22))

    # The applicant-facing status still says "under review": approval is one
    # half of the gate, and no internal pricing input is exposed.
    assert application["status"] == "UNDER_REVIEW"
    assert "bank_rate_pct" not in approval and "factor_results" not in approval


async def test_an_undecided_request_has_no_approval(
    client, make_user, make_project, make_loan_application, submit_loan_application
):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    application = await make_loan_application(sme, project["id"])
    await submit_loan_application(sme, application["id"])

    assert (await _mine(client, sme, project["id"]))["approval"] is None


async def test_an_approval_without_a_graded_run_has_no_score(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents,
):
    sme, project, _ = await _scored_and_approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents, score=False,
    )
    approval = (await _mine(client, sme, project["id"]))["approval"]
    assert approval["approved_at"] is not None
    assert approval["business_score"] is None and approval["reference_rate_pct"] is None
    assert approval["total_repayment_vnd"] is None


async def test_the_public_listing_never_carries_an_approval(
    client, make_user, make_admin, make_project, make_loan_application,
    submit_loan_application, attach_required_documents,
):
    await _scored_and_approved(
        client, make_user, make_admin, make_project, make_loan_application,
        submit_loan_application, attach_required_documents,
    )
    investor = await make_user(role="INVESTOR")
    resp = await client.get("/projects/public", headers=auth_headers(investor))
    assert resp.status_code == 200, resp.text
    public = resp.json()
    assert all((p.get("loan_application") or {}).get("approval") is None for p in public)
