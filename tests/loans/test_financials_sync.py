"""A score run grades what the application already holds.

start_score_run() fills `loan_application_financials` from the parsed uploads,
the company form and the business verification (sync_financials) before it
grades — so an SME who completed the wizard gets a score without an operator
retyping anything. An operator-entered row is never overwritten, and nothing
missing is invented.
"""
from uuid import UUID

import pytest
from sqlalchemy import select

from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification
from app.lending.models import LoanApplicationFinancials
from conftest import SIGNED_AGREEMENT_PDF, auth_headers
from tests.loans.test_lite_grading_figures import figures
from tests.uploads.test_cic import blank_pdf, report_text
from tests.uploads.test_tax_filings import TAX, folder_zip, statements_xml, vat_months

FILINGS = folder_zip({**vat_months(), "B02.xml": statements_xml()})


@pytest.fixture
def storage(monkeypatch):
    # The stored object is chosen by the document type in its key.
    def get_bytes(key, **kw):
        if "sme_service_agreement" in key:
            return SIGNED_AGREEMENT_PDF
        return blank_pdf() if "cic_report" in key else FILINGS

    monkeypatch.setattr("app.uploads.parsers.cic._text", lambda raw: report_text())
    monkeypatch.setattr("app.uploads.service.presign_put", lambda key, ct, **kw: f"https://r2.test/{key}")
    monkeypatch.setattr("app.uploads.service.head_object", lambda key, **kw: {"size": 1024, "content_type": None})
    monkeypatch.setattr("app.uploads.service.get_bytes", get_bytes)
    monkeypatch.setattr("app.uploads.service.delete_object", lambda key: None)


async def _apply(client, sme, *, employee_count=25, duration_months=3):
    """The project-application form, as the frontend sends it."""
    loan = {"requested_amount": 80_000_000, "purpose": "Working capital"}
    if duration_months is not None:
        loan["duration_months"] = duration_months
    body = {
        "legal_name": "Sync Co",
        "tax_id": TAX,
        "industry": "IT Services",
        "incorporation_date": "2019-01-15",
        "loan_application": loan,
    }
    if employee_count is not None:
        body["employee_count"] = employee_count
    resp = await client.post("/projects", json=body, headers=auth_headers(sme))
    assert resp.status_code == 201, resp.text
    return resp.json()["loan_application"]["id"]


async def _upload(client, sme, app_id, document_type, filename, content_type):
    init = await client.post(
        "/uploads/init-upload",
        json={"loan_application_id": app_id, "document_type": document_type, "filename": filename,
              "content_type": content_type, "size": 1024},
        headers=auth_headers(sme),
    )
    assert init.status_code == 201, init.text
    confirm = await client.post(
        "/uploads/confirm", json={"loan_application_id": app_id, "file_keys": [init.json()["file_key"]]},
        headers=auth_headers(sme),
    )
    assert confirm.status_code == 200, confirm.text


async def _verified(db_session, sme, tax_code=TAX):
    db_session.add(GVerifyKybVerification(
        user_id=UUID(sme["id"]), status=STATUS_APPROVED, document_type="COMPANY", tax_code=tax_code,
    ))
    await db_session.commit()


async def _complete(client, db_session, sme, **form):
    app_id = await _apply(client, sme, **form)
    await _verified(db_session, sme)
    resp = await client.put(
        f"/loans/applications/{app_id}/figures",
        json=figures(fixed_cost_y1=600_000_000, variable_cost_excl_cogs_y1=0),
        headers=auth_headers(sme),
    )
    assert resp.status_code == 200, resp.text
    await _upload(client, sme, app_id, "tax_filings", "filings.zip", "application/zip")
    await _upload(client, sme, app_id, "cic_report", "cic.pdf", "application/pdf")
    await _upload(client, sme, app_id, "sme_service_agreement", "signed.pdf", "application/pdf")
    submit = await client.post(f"/loans/applications/{app_id}/submit", headers=auth_headers(sme))
    assert submit.status_code == 200, submit.text
    return app_id


async def _score(client, admin, app_id):
    resp = await client.post(
        "/underwriting/score-runs", json={"application_id": app_id, "mode": None}, headers=auth_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _financials(db_session, app_id):
    db_session.expire_all()
    return (
        await db_session.execute(
            select(LoanApplicationFinancials).where(LoanApplicationFinancials.application_id == UUID(app_id))
        )
    ).scalar_one_or_none()


async def test_a_completed_application_is_graded_from_its_own_evidence(
    client, db_session, make_user, make_admin, storage
):
    sme, admin = await make_user(role="SME"), await make_admin()
    app_id = await _complete(client, db_session, sme)

    run = await _score(client, admin, app_id)
    assert run["decision"] != "INSUFFICIENT_DATA", run

    row = await _financials(db_session, app_id)
    assert row.source == "evidence"
    assert (row.company_size, row.duration_months) == ("small", 3)  # 25 employees
    assert len(row.monthly_revenue_vnd) == 24  # the signed VAT series
    assert int(row.cogs_y1_vnd) == 6_400_000_000  # the statements, not the typed 2.4bn
    # Fixed and variable cost are the statements' as well, not the 600m and 0
    # the applicant saved: the wizard reads both from the statements and locks
    # them, so grading does the same. Fixed = line 24 (300m) + line 22 (40m);
    # a TT133 statement has no selling line, so variable is 0.
    assert int(row.fixed_cost_y1_vnd) == 340_000_000
    assert int(row.variable_cost_excl_cogs_y1_vnd) == 0
    assert row.cic_score == 629 and row.kyc_aml_passed is True


async def test_an_operator_entered_row_is_not_overwritten(client, db_session, make_user, make_admin, storage):
    from tests.underwriting.test_score_run_persistence import FULL_FINANCIALS

    sme, admin = await make_user(role="SME"), await make_admin()
    app_id = await _complete(client, db_session, sme)
    typed = await client.put(
        f"/underwriting/applications/{app_id}/financials",
        json={**FULL_FINANCIALS, "cic_score": 700},
        headers=auth_headers(admin),
    )
    assert typed.status_code == 200, typed.text

    await _score(client, admin, app_id)
    row = await _financials(db_session, app_id)
    assert row.source == "admin" and row.cic_score == 700


async def test_missing_verification_is_left_missing_and_named(client, db_session, make_user, make_admin, storage):
    sme, admin = await make_user(role="SME"), await make_admin()
    app_id = await _apply(client, sme)  # no business verification
    await _upload(client, sme, app_id, "tax_filings", "filings.zip", "application/zip")
    await _upload(client, sme, app_id, "cic_report", "cic.pdf", "application/pdf")
    await client.put(f"/loans/applications/{app_id}/figures", json=figures(), headers=auth_headers(sme))
    await _upload(client, sme, app_id, "sme_service_agreement", "signed.pdf", "application/pdf")
    assert (await client.post(f"/loans/applications/{app_id}/submit", headers=auth_headers(sme))).status_code == 200

    run = await _score(client, admin, app_id)
    assert run["decision"] == "INSUFFICIENT_DATA"
    # Written, but with the verification absent — not assumed to have passed.
    row = await _financials(db_session, app_id)
    assert row is not None and row.kyc_aml_passed is None

    # The admin panel says what is missing.
    from app.lending.models import LoanApplication

    project_id = (
        await db_session.execute(select(LoanApplication.project_id).where(LoanApplication.id == UUID(app_id)))
    ).scalar_one()
    detail = (await client.get(f"/admin/projects/{project_id}", headers=auth_headers(admin))).json()
    assert "kyc_aml_passed" in detail["applications"][0]["score_run"]["missing_inputs"]


async def test_without_headcount_or_term_no_row_is_written(client, db_session, make_user, make_admin, storage):
    sme, admin = await make_user(role="SME"), await make_admin()
    app_id = await _apply(client, sme, employee_count=None, duration_months=None)
    await _upload(client, sme, app_id, "sme_service_agreement", "signed.pdf", "application/pdf")
    assert (await client.post(f"/loans/applications/{app_id}/submit", headers=auth_headers(sme))).status_code == 200

    run = await _score(client, admin, app_id)
    assert run["decision"] == "INSUFFICIENT_DATA"
    assert await _financials(db_session, app_id) is None
