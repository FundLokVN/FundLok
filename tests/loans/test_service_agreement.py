"""The SME Service Agreement (Contract 2) in the loan application: the SME
downloads it filled in with its own details, signs it with its enterprise
digital signature, uploads it back, and cannot submit without it.
"""
import io
from types import SimpleNamespace
from uuid import UUID

import pytest
from pypdf import PdfReader

from app.direct_transfer.models import PaymentAccount
from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification
from app.loans.agreement import text
from app.loans.agreement.signature import has_digital_signature
from app.projects.service import verified_document_types
from conftest import SIGNED_AGREEMENT_PDF, auth_headers

TAX = "0319911223"


@pytest.fixture
def storage(monkeypatch):
    """R2, holding whatever the test says the SME uploaded (`storage.stored`)."""
    held = SimpleNamespace(stored=SIGNED_AGREEMENT_PDF)
    monkeypatch.setattr("app.uploads.service.presign_put", lambda key, ct, **kw: f"https://r2.test/{key}")
    monkeypatch.setattr("app.uploads.service.head_object", lambda key, **kw: {"size": 2048, "content_type": None})
    monkeypatch.setattr("app.uploads.service.get_bytes", lambda key, **kw: held.stored)
    monkeypatch.setattr("app.uploads.service.delete_object", lambda key: None)
    return held


async def _apply(client, sme, *, tax_id=TAX, amount=80_000_000, months=4) -> str:
    resp = await client.post(
        "/projects",
        json={
            "legal_name": "Project Name From Form",
            "tax_id": tax_id,
            "industry": "IT Services",
            "incorporation_date": "2019-01-15",
            "employee_count": 12,
            "loan_application": {"requested_amount": amount, "purpose": "Working capital", "duration_months": months},
        },
        headers=auth_headers(sme),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["loan_application"]["id"]


async def _verified_with_bank(db_session, sme, *, tax_id=TAX):
    db_session.add(
        GVerifyKybVerification(
            user_id=UUID(sme["id"]),
            status=STATUS_APPROVED,
            document_type="COMPANY",
            tax_code=tax_id,
            business_name="CONG TY TNHH AGREEMENT TEST",
            company_address="12 Nguyen Hue, Ben Nghe, Ho Chi Minh City",
            representatives=[{"name": "Tran Van Owner", "position": "Giam doc"}],
        )
    )
    db_session.add(
        PaymentAccount(
            user_id=UUID(sme["id"]),
            holder_role="SME",
            holder_type="ENTITY",
            bank_name="Vietcombank",
            account_number="0071000123456",
            account_name="CONG TY TNHH AGREEMENT TEST",
            status="APPROVED",
        )
    )
    await db_session.commit()


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


async def _upload(client, sme, app_id, *, document_type="sme_service_agreement", filename="signed.pdf",
                  content_type="application/pdf"):
    init = await client.post(
        "/uploads/init-upload",
        json={"loan_application_id": app_id, "document_type": document_type, "filename": filename,
              "content_type": content_type, "size": 2048},
        headers=auth_headers(sme),
    )
    if init.status_code != 201:
        return init
    return await client.post(
        "/uploads/confirm",
        json={"loan_application_id": app_id, "file_keys": [init.json()["file_key"]]},
        headers=auth_headers(sme),
    )


async def test_the_agreement_is_filled_from_the_smes_verified_details(client, db_session, make_user):
    sme = await make_user(role="SME")
    app_id = await _apply(client, sme)
    await _verified_with_bank(db_session, sme)

    resp = await client.get(f"/loans/applications/{app_id}/service-agreement", headers=auth_headers(sme))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline;")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content.startswith(b"%PDF")

    body = _pdf_text(resp.content)
    # Party B, from the verified business and the approved bank account.
    for expected in (
        "CONG TY TNHH AGREEMENT TEST",
        TAX,
        "12 Nguyen Hue",
        "Tran Van Owner",
        "0071000123456",
        "Vietcombank",
    ):
        assert expected in body, expected
    # The Annex, from the application; the version on every page.
    assert "80.000.000" in body and "4 th" in body
    assert body.count(text.VERSION) >= 2
    # Party A and the last article are there: the whole contract, not a stub.
    assert "1501167629" in body and "11." in body

    download = await client.get(
        f"/loans/applications/{app_id}/service-agreement", params={"download": "true"}, headers=auth_headers(sme)
    )
    assert download.headers["content-disposition"].startswith("attachment;")
    assert TAX in download.headers["content-disposition"]


async def test_what_is_not_known_prints_as_a_blank(client, make_user):
    # No business verification and no bank account yet: the project's own
    # name and tax id, and dotted blanks for the rest.
    sme = await make_user(role="SME")
    app_id = await _apply(client, sme, tax_id="0319911224")
    body = _pdf_text(
        (await client.get(f"/loans/applications/{app_id}/service-agreement", headers=auth_headers(sme))).content
    )
    assert "Project Name From Form" in body and "0319911224" in body
    assert text.BLANK in body


async def test_only_the_owning_sme_gets_the_agreement(client, make_user):
    owner = await make_user(role="SME")
    other_sme = await make_user(role="SME")
    investor = await make_user(role="INVESTOR")
    app_id = await _apply(client, owner)
    path = f"/loans/applications/{app_id}/service-agreement"

    assert (await client.get(path, headers=auth_headers(other_sme))).status_code == 403
    assert (await client.get(path, headers=auth_headers(investor))).status_code == 403
    missing = "/loans/applications/00000000-0000-0000-0000-000000000000/service-agreement"
    assert (await client.get(missing, headers=auth_headers(owner))).status_code == 404
    client.cookies.clear()
    assert (await client.get(path)).status_code == 401


async def test_submitting_needs_the_signed_agreement(client, make_user, storage):
    sme = await make_user(role="SME")
    app_id = await _apply(client, sme)
    submit = f"/loans/applications/{app_id}/submit"

    refused = await client.post(submit, headers=auth_headers(sme))
    assert refused.status_code == 400
    assert refused.headers["X-Error-Code"] == "SERVICE_AGREEMENT_REQUIRED"

    # Only a PDF: what an enterprise digital signature signs.
    wrong = await _upload(client, sme, app_id, filename="signed.docx",
                          content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert wrong.status_code in (400, 422), wrong.text

    confirmed = await _upload(client, sme, app_id)
    assert confirmed.status_code == 200, confirmed.text
    accepted = await client.post(submit, headers=auth_headers(sme))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "SUBMITTED"


async def test_the_unsigned_agreement_sent_straight_back_is_refused(client, make_user, storage):
    # The likeliest mistake: the PDF as downloaded, never signed.
    sme = await make_user(role="SME")
    app_id = await _apply(client, sme)
    storage.stored = (
        await client.get(f"/loans/applications/{app_id}/service-agreement", headers=auth_headers(sme))
    ).content

    refused = await _upload(client, sme, app_id)
    assert refused.status_code == 422
    assert refused.headers["X-Error-Code"] == "SERVICE_AGREEMENT_UNSIGNED"
    # Nothing committed: the application still cannot be submitted.
    still = await client.post(f"/loans/applications/{app_id}/submit", headers=auth_headers(sme))
    assert still.headers["X-Error-Code"] == "SERVICE_AGREEMENT_REQUIRED"

    # The same file once a signing tool has appended its signature is accepted.
    storage.stored = storage.stored + SIGNED_AGREEMENT_PDF[SIGNED_AGREEMENT_PDF.index(b"9 0 obj"):]
    assert (await _upload(client, sme, app_id)).status_code == 200


def test_the_signature_check_needs_a_pdf_with_a_signature_dictionary():
    assert has_digital_signature(SIGNED_AGREEMENT_PDF)
    assert not has_digital_signature(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n")
    # A PDF signature dictionary pasted into something that is not a PDF.
    assert not has_digital_signature(b"PK\x03\x04 /ByteRange [0 1 2 3]")


def test_investors_are_not_shown_the_agreement():
    # The due-diligence list names the document types FundLok reviewed; the
    # agreement between the SME and FundLok is not one of them for investors.
    application = SimpleNamespace(
        admin_approval="APPROVED",
        documents=[
            SimpleNamespace(document_type="legal_charter", status="UPLOADED"),
            SimpleNamespace(document_type="sme_service_agreement", status="UPLOADED"),
            SimpleNamespace(document_type="financial_report", status="UPLOADED"),
        ],
    )
    shown = verified_document_types(application)
    assert "sme_service_agreement" not in shown
    assert "financial_report" in shown
