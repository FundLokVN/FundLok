"""The SME's signed Service Agreement (wizard step 4), checked with NEAC for staff: NEAC's own verification, whether
it is this application's agreement, and whether the signer's MST is the company's, shown in the admin project panel.
Tests never call NEAC: neac.verify_document is replaced per test.
"""
import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.lending.models import AuditLog, LoanApplication, LoanApplicationDocument
from app.market.investor_agreement import neac
from conftest import SIGNED_AGREEMENT_PDF, auth_headers
from tests.loans.test_service_agreement import TAX, _apply, _verified_with_bank


def _signature(*, signer_mst=TAX, signer_name="CONG TY TNHH AGREEMENT TEST", case="VALID"):
    return neac.NeacSignature(
        issuer={"cn": "FPT Certification Authority SHA256", "o": "FPT CORPORATION", "c": "VN"},
        signer={"cn": signer_name, "c": "VN", "cccd": f"MST:{signer_mst}"},
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
    app_id = await _apply(client, sme, tax_id=tax_id)
    if verified:
        await _verified_with_bank(db_session, sme, tax_id=tax_id)
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
