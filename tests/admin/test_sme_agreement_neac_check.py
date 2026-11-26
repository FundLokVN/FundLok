"""The SME's signed listing documents (the Service Agreement and the Standing Offer), checked with NEAC for staff:
NEAC's own verification, whether it is this application's document with today's check code, whether the signer's
MST is the company's, and on the Standing Offer the Personal Guarantor's own signature, shown in the admin project
panel. Tests never call NEAC: neac.verify_document is replaced per test.
"""
import datetime as dt
import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.direct_transfer.models import PaymentAccount
from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification, GVerifyVerification
from app.lending.models import AuditLog, LoanApplication, LoanApplicationDocument, ScoreRun
from app.market.investor_agreement import neac
from conftest import SIGNED_AGREEMENT_PDF, auth_headers
from tests.loans.test_service_agreement import TAX, _apply, _verified_with_bank


GUARANTOR_ID = "079123456789"
GUARANTOR_NAME = "TRAN VAN OWNER"


def _signature(*, signer_mst=TAX, signer_name="CONG TY TNHH AGREEMENT TEST", case="VALID", cccd=None):
    return neac.NeacSignature(
        issuer={"cn": "FPT Certification Authority SHA256", "o": "FPT CORPORATION", "c": "VN"},
        signer={"cn": signer_name, "c": "VN", "cccd": f"CCCD:{cccd}" if cccd else f"MST:{signer_mst}"},
        intact="Bị thay đổi" if case == "MODIFIED" else "Không bị thay đổi",
        certificate_validity="Thời điểm có hiệu lực: 2026-03-18; Thời điểm hết hiệu lực: 2027-03-26",
        validity="Không hợp lệ" if case == "INVALID" else "Hợp lệ tại thời điểm ký",
        timestamp_status="Không",
        signed_time="Ngày 09 tháng 11 năm 2026, 11:00:00 [UTC+07:00]",
        reason_invalidity="Chứng thư số của tổ chức phát hành chưa được công nhận." if case == "INVALID" else "",
        ocsp="Chứng thư số chưa bị thu hồi. [http://ocsp3.fis.com.vn]",
        crl="Chứng thư số chưa bị thu hồi [http://crl3.fis.com.vn]",
    )


@pytest.fixture
def neac_answers(monkeypatch):
    """NEAC, faked: `neac_answers.next` is what it says (a NeacResult, or an exception to raise)."""
    state = SimpleNamespace(next=neac.NeacResult(message="Success", signatures=[_signature()]), calls=0)

    async def _verify(content, filename):
        state.calls += 1
        if isinstance(state.next, Exception):
            raise state.next
        return state.next

    monkeypatch.setattr(neac, "verify_document", _verify)
    return state


def _signed(pdf: bytes) -> bytes:
    """FundLok's download, as a signing tool leaves it (a signature dictionary added after it)."""
    startxref = re.findall(rb"startxref\s+(\d+)", pdf)[-1]
    return pdf + b"\n9 0 obj\n<< /Type /Sig /ByteRange [0 120 4216 380] /Contents <3082> >>\nendobj\nstartxref\n" + startxref + b"\n%%EOF\n"


async def _uploaded_agreement(client, db_session, make_user, monkeypatch, *, content=None, verified=True, tax_id=TAX):
    """An SME application whose signed Service Agreement is on file; (project id, document id)."""
    sme = await make_user(role="SME")
    if verified:
        await _verified_with_bank(db_session, sme, tax_id=tax_id)
    app_id = await _apply(client, db_session, sme, tax_id=tax_id)
    if not verified:
        # A historical project without KYB uses the project tax ID.
        await db_session.execute(delete(GVerifyKybVerification).where(
            GVerifyKybVerification.user_id == uuid.UUID(sme["id"])
        ))
        await db_session.commit()
    if content is None:
        download = await client.get(f"/loans/applications/{app_id}/service-agreement", headers=auth_headers(sme))
        assert download.status_code == 200, download.text
        content = _signed(download.content)
    document = LoanApplicationDocument(
        loan_application_id=uuid.UUID(app_id), document_type="sme_service_agreement",
        file_key=f"test/{uuid.uuid4()}.pdf", original_filename="hop-dong-da-ky.pdf",
        content_type="application/pdf", file_size_bytes=len(content), status="UPLOADED",
    )
    db_session.add(document)
    await db_session.commit()
    monkeypatch.setattr("app.loans.agreement.neac_check.get_bytes", lambda key, **kw: content)
    application = await db_session.get(LoanApplication, uuid.UUID(app_id))
    return application.project_id, document.id


async def _uploaded_offer(client, db_session, make_user, monkeypatch, *, kind="standing-offer"):
    """An approved application (locked terms, the owner's eKYC) whose signed Standing Offer (or, with
    kind="loan-contract", Loan Contract template) is on file; (project id, document id, the SME)."""
    sme = await make_user(role="SME")
    await _verified_with_bank(db_session, sme)
    app_id = await _apply(client, db_session, sme)
    db_session.add(GVerifyVerification(
        user_id=uuid.UUID(sme["id"]), verification_type="KYC", status=STATUS_APPROVED,
        full_name=GUARANTOR_NAME, person_number=GUARANTOR_ID,
    ))
    application = await db_session.get(LoanApplication, uuid.UUID(app_id))
    application.admin_approval = "APPROVED"
    db_session.add(ScoreRun(
        application_id=application.id, status="LOCKED", locked_at=dt.datetime.now(dt.timezone.utc),
        final_grade_precise=72.0, interest_rate_pct=14.5,
    ))
    application.guarantor_address = "45 Le Loi, Phuong 4, TP. Vung Tau"
    await db_session.commit()
    if kind == "loan-contract":
        # The Loan Contract cites the signed Offer: it is on file first.
        db_session.add(LoanApplicationDocument(
            loan_application_id=application.id, document_type="sme_standing_offer",
            file_key=f"test/{uuid.uuid4()}.pdf", original_filename="standing-offer-da-ky.pdf",
            content_type="application/pdf", file_size_bytes=1, status="UPLOADED",
            uploaded_at=dt.datetime.now(dt.timezone.utc),
        ))
        await db_session.commit()
    download = await client.get(f"/loans/applications/{app_id}/{kind}", headers=auth_headers(sme))
    assert download.status_code == 200, download.text
    content = _signed(download.content)
    document = LoanApplicationDocument(
        loan_application_id=application.id,
        document_type="sme_standing_offer" if kind == "standing-offer" else "sme_loan_contract",
        file_key=f"test/{uuid.uuid4()}.pdf", original_filename=f"{kind}-da-ky.pdf",
        content_type="application/pdf", file_size_bytes=len(content), status="UPLOADED",
    )
    db_session.add(document)
    await db_session.commit()
    monkeypatch.setattr("app.loans.agreement.neac_check.get_bytes", lambda key, **kw: content)
    return application.project_id, document.id, sme


def _company_and_guarantor():
    return neac.NeacResult(
        message="Success", signatures=[_signature(), _signature(signer_name=GUARANTOR_NAME, cccd=GUARANTOR_ID)]
    )


async def _check(client, admin, document_id):
    return await client.post(f"/admin/documents/{document_id}/neac-check", headers=admin["headers"])


async def _panel_document(client, admin, project_id, document_id):
    detail = await client.get(f"/admin/projects/{project_id}", headers=admin["headers"])
    assert detail.status_code == 200, detail.text
    for application in detail.json()["applications"]:
        for document in application["documents"]:
            if document["id"] == str(document_id):
                return document
    raise AssertionError("document not in the panel")


async def test_a_valid_agreement_signed_by_the_company_shows_as_verified_and_matching(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    project_id, document_id = await _uploaded_agreement(client, db_session, make_user, monkeypatch)
    before = await _panel_document(client, admin, project_id, document_id)
    assert before["signature_check"] is None  # not checked yet

    resp = await _check(client, admin, document_id)

    assert resp.status_code == 200, resp.text
    check = resp.json()
    assert check["status"] == "VERIFIED"
    assert check["right_document"] is True  # this application's Contract 2
    assert check["signer_matches"] is True and check["expected_tax_code"] == TAX
    assert check["expected_source"] == "KYB" and check["name_matches"] is True
    assert check["signer_id"] == f"MST:{TAX}"
    assert check["issuer_name"] == "FPT Certification Authority SHA256"
    assert check["problems"] == []
    # Its check code is the one the agreement carries today; no guarantor on Contract 2.
    assert check["terms_match"] is True and check["check_code"]
    assert check["guarantor_required"] is False and check["guarantor_matches"] is None
    # Recorded, and what the panel shows from now on.
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "NEAC_SIGNATURE_CHECKED"))
    ).scalar_one()
    assert audit.entity_id == document_id and str(audit.actor_id) == admin["id"]
    shown = await _panel_document(client, admin, project_id, document_id)
    assert shown["signature_check"]["status"] == "VERIFIED"
    assert shown["signature_check"]["signer_matches"] is True


async def test_a_signature_neac_does_not_accept_shows_its_reasons(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    _, document_id = await _uploaded_agreement(client, db_session, make_user, monkeypatch)

    for case, says, status in (
        ("INVALID", "Chữ ký: Không hợp lệ", "INVALID"),
        ("MODIFIED", "Tài liệu đã ký: Bị thay đổi", "INVALID"),
    ):
        neac_answers.next = neac.NeacResult(message="Success", signatures=[_signature(case=case)])
        check = (await _check(client, admin, document_id)).json()
        assert check["status"] == status and any(says in p for p in check["problems"]), (case, check)

    neac_answers.next = neac.NeacResult(message="Văn bản không có chữ ký số", signatures=[])
    check = (await _check(client, admin, document_id)).json()
    assert check["status"] == "NO_SIGNATURE" and check["problems"] == ["Văn bản không có chữ ký số"]


async def test_another_companys_signature_or_another_pdf_is_flagged(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    _, document_id = await _uploaded_agreement(client, db_session, make_user, monkeypatch)
    neac_answers.next = neac.NeacResult(
        message="Success", signatures=[_signature(signer_mst="1501167629", signer_name="CÔNG TY CỔ PHẦN FUNDLOK")]
    )

    check = (await _check(client, admin, document_id)).json()

    assert check["status"] == "VERIFIED"  # NEAC accepts the signature...
    assert check["signer_matches"] is False  # ...but it is not this company's
    assert check["signer_id"] == "MST:1501167629" and check["expected_tax_code"] == TAX

    # A validly signed PDF that is not the agreement (e.g. the company charter).
    _, charter_id = await _uploaded_agreement(
        client, db_session, make_user, monkeypatch, content=SIGNED_AGREEMENT_PDF, tax_id="0319911224"
    )
    charter = (await _check(client, admin, charter_id)).json()
    assert charter["right_document"] is False
    assert charter["terms_match"] is None  # no check code in it to compare


async def test_without_an_approved_kyb_the_project_tax_id_is_the_reference(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    _, document_id = await _uploaded_agreement(client, db_session, make_user, monkeypatch, verified=False)

    check = (await _check(client, admin, document_id)).json()

    assert check["expected_source"] == "PROJECT" and check["expected_tax_code"] == TAX
    assert check["signer_matches"] is True


async def test_neac_being_down_is_recorded_and_can_be_checked_again(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    project_id, document_id = await _uploaded_agreement(client, db_session, make_user, monkeypatch)
    neac_answers.next = neac.NeacUnavailable("timed out")

    down = await _check(client, admin, document_id)

    assert down.status_code == 200 and down.json()["status"] == "UNAVAILABLE"
    neac_answers.next = neac.NeacResult(message="Success", signatures=[_signature()])
    assert (await _check(client, admin, document_id)).json()["status"] == "VERIFIED"
    assert (await _panel_document(client, admin, project_id, document_id))["signature_check"]["status"] == "VERIFIED"


async def test_only_staff_check_and_only_the_signed_agreement(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    _, document_id = await _uploaded_agreement(client, db_session, make_user, monkeypatch)
    sme = await make_user(role="SME")

    assert (await client.post(f"/admin/documents/{document_id}/neac-check", headers=auth_headers(sme))).status_code == 403
    assert (await _check(client, admin, uuid.uuid4())).status_code == 404
    other = LoanApplicationDocument(
        loan_application_id=(await db_session.get(LoanApplicationDocument, document_id)).loan_application_id,
        document_type="legal_charter", file_key=f"test/{uuid.uuid4()}.pdf", original_filename="dieu-le.pdf",
        status="UPLOADED",
    )
    db_session.add(other)
    await db_session.commit()
    assert (await _check(client, admin, other.id)).status_code == 404
    assert neac_answers.calls == 0


async def test_a_standing_offer_signed_by_the_company_and_the_guarantor_is_verified(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    project_id, document_id, _ = await _uploaded_offer(client, db_session, make_user, monkeypatch)
    neac_answers.next = _company_and_guarantor()

    check = (await _check(client, admin, document_id)).json()

    assert check["document_type"] == "sme_standing_offer"
    assert check["status"] == "VERIFIED" and check["right_document"] is True
    assert check["signer_matches"] is True and check["signer_id"] == f"MST:{TAX}"
    assert check["guarantor_required"] is True and check["guarantor_matches"] is True
    assert check["guarantor_id"] == f"CCCD:{GUARANTOR_ID}" and check["guarantor_expected_id"] == GUARANTOR_ID
    assert check["guarantor_name_matches"] is True
    assert check["terms_match"] is True
    shown = await _panel_document(client, admin, project_id, document_id)
    assert shown["signature_check"]["guarantor_matches"] is True


async def test_a_standing_offer_without_the_guarantors_own_signature_is_flagged(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    _, document_id, _ = await _uploaded_offer(client, db_session, make_user, monkeypatch)

    # The company's signature only.
    neac_answers.next = neac.NeacResult(message="Success", signatures=[_signature()])
    only_company = (await _check(client, admin, document_id)).json()
    assert only_company["status"] == "VERIFIED" and only_company["signer_matches"] is True
    assert only_company["guarantor_matches"] is False and only_company["guarantor_id"] is None

    # The guarantor's signature is there, but NEAC does not accept it.
    neac_answers.next = neac.NeacResult(message="Success", signatures=[
        _signature(), _signature(signer_name=GUARANTOR_NAME, cccd=GUARANTOR_ID, case="INVALID"),
    ])
    invalid = (await _check(client, admin, document_id)).json()
    assert invalid["guarantor_matches"] is False and invalid["guarantor_problems"]


async def test_a_copy_whose_details_changed_since_signing_no_longer_matches(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    _, document_id, sme = await _uploaded_offer(client, db_session, make_user, monkeypatch)
    neac_answers.next = _company_and_guarantor()
    assert (await _check(client, admin, document_id)).json()["terms_match"] is True

    # The bank account investors pay into changed after signing: the signed copy names the old one.
    account = (
        await db_session.execute(select(PaymentAccount).where(PaymentAccount.user_id == uuid.UUID(sme["id"])))
    ).scalar_one()
    account.account_number = "0071000999999"
    await db_session.commit()

    assert (await _check(client, admin, document_id)).json()["terms_match"] is False


async def test_a_loan_contract_template_is_checked_like_the_standing_offer(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    admin = await make_admin()
    _, document_id, _ = await _uploaded_offer(client, db_session, make_user, monkeypatch, kind="loan-contract")
    neac_answers.next = _company_and_guarantor()

    check = (await _check(client, admin, document_id)).json()
    assert check["document_type"] == "sme_loan_contract"
    assert check["status"] == "VERIFIED" and check["right_document"] is True
    assert check["guarantor_required"] is True and check["guarantor_matches"] is True
    assert check["terms_match"] is True

    neac_answers.next = neac.NeacResult(message="Success", signatures=[_signature()])
    assert (await _check(client, admin, document_id)).json()["guarantor_matches"] is False


async def test_a_copy_of_an_earlier_version_is_still_the_right_document(
    client, db_session, make_user, make_admin, monkeypatch, neac_answers
):
    # Signed in production before the current text (sme-agreement-v0.3): still this application's Contract 2 and
    # its signatures still checked, but its check code was over that text, so there is nothing to compare.
    from app.loans.agreement import text

    admin = await make_admin()
    with monkeypatch.context() as old:
        old.setattr(text, "VERSION", "sme-agreement-v0.3")
        sme = await make_user(role="SME")
        app_id = await _apply(client, db_session, sme)
        await _verified_with_bank(db_session, sme)
        download = await client.get(f"/loans/applications/{app_id}/service-agreement", headers=auth_headers(sme))
        assert download.status_code == 200, download.text
    _, document_id = await _uploaded_agreement(
        client, db_session, make_user, monkeypatch, content=_signed(download.content), tax_id="0319911299"
    )
    # _uploaded_agreement made an application of its own: the document is this one's.
    document = await db_session.get(LoanApplicationDocument, document_id)
    document.loan_application_id = uuid.UUID(app_id)
    await db_session.commit()

    check = (await _check(client, admin, document_id)).json()
    assert check["status"] == "VERIFIED"
    assert check["right_document"] is True and check["agreement_version"] == "sme-agreement-v0.3"
    assert check["terms_match"] is None
