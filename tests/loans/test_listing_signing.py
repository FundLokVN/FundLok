"""Signing the listing documents after approval (docs/specs/loans/listing-signing.md).

FundLok approves the request (the operator's approval and a LOCKED score run, which fixes the rate); the SME then
downloads the SME Service Agreement, the Standing Offer and the Loan Contract template filled in with those terms,
signs them with its enterprise digital signature and uploads them back. Each upload is checked with NEAC (faked:
conftest fake_neac). The Personal Guarantor then signs the Standing Offer and the Loan Contract by a Zalo one-time
code (Money flow FINAL v1.0); a copy that already carries the guarantor's own digital signature also counts. The set
complete publishes the project.
"""
import asyncio
import datetime as dt
import io
import re
from types import SimpleNamespace
from uuid import UUID

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.gverify.models import STATUS_APPROVED, GVerifyVerification
from app.lending.models import AuditLog, LoanApplication, LoanApplicationDocument, Project, ScoreRun
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject, DictionaryObject, NameObject, NumberObject

from app.direct_transfer.models import PaymentAccount
from app.loans.agreement import loan_contract_text, standing_offer_text
from app.loans.agreement import text as agreement_text
from app.core.config import settings
from app.loans.guarantor_models import GuarantorSignature, GuarantorSigningCode
from app.loans.signing import signature_fields
from app.phone_verification import zalo
from app.users.models import User
from conftest import auth_headers
from tests.loans.test_service_agreement import TAX, _apply, _verified_with_bank

GUARANTOR_ID = "079123456789"
GUARANTOR_NAME = "TRAN VAN OWNER"
GUARANTOR_ADDRESS = "45 Le Loi, Phuong 4, TP. Vung Tau"


@pytest.fixture
def stored(monkeypatch):
    """R2 for the signed copies: what was put, by key."""
    held: dict[str, bytes] = {}
    monkeypatch.setattr("app.loans.signing.put_bytes", lambda key, data, ct: held.__setitem__(key, data))
    monkeypatch.setattr("app.loans.signing.get_bytes", lambda key, **kw: held[key])
    return held


async def _kyc(db_session, sme, *, person_number=GUARANTOR_ID):
    db_session.add(
        GVerifyVerification(
            user_id=UUID(sme["id"]),
            verification_type="KYC",
            status=STATUS_APPROVED,
            full_name=GUARANTOR_NAME,
            person_number=person_number,
        )
    )
    await db_session.commit()


_PHONE_SEQ = iter(range(10_000_000, 99_999_999))


async def _verified_phone(db_session, sme):
    """The guarantor's own account has a verified phone (sign-up's Zalo code): where their signing code goes."""
    user = await db_session.get(User, UUID(sme["id"]))
    user.phone = f"+849{next(_PHONE_SEQ):08d}"
    user.phone_verified_at = dt.datetime.now(dt.timezone.utc)
    await db_session.commit()
    return user.phone


@pytest.fixture
def outbox(monkeypatch):
    """Every code handed to Zalo, in order."""
    sent: list[dict] = []

    async def _record(to, code, *, tracking_id):
        sent.append({"to": to, "code": code, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_otp", _record)
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "zalo")
    return sent


async def _guarantor_code(client, case):
    return await client.post(f"/loans/applications/{case.app_id}/guarantor-signature/code", headers=auth_headers(case.sme))


async def _guarantor_sign(client, case, code):
    return await client.post(
        f"/loans/applications/{case.app_id}/guarantor-signature", json={"code": code}, headers=auth_headers(case.sme)
    )


@pytest.fixture
def approved(client, db_session, make_user, make_admin, attach_required_documents):
    """An SME whose request FundLok approved, with locked terms (14.5% a year), its KYB, an approved bank account,
    its owner's eKYC and the Guarantor's address (entered on the signing step: the eKYC carries none): everything
    signing needs, nothing signed yet."""

    async def _make(*, lock=True, kyc=True, approve=True, tax_id=TAX, guarantor_address=True, phone=True):
        sme = await make_user(role="SME")
        if phone:
            await _verified_phone(db_session, sme)
        app_id = await _apply(client, sme, tax_id=tax_id)
        await _verified_with_bank(db_session, sme, tax_id=tax_id)
        if kyc:
            await _kyc(db_session, sme)
        if guarantor_address:
            entered = await client.put(
                f"/loans/applications/{app_id}/guarantor-address",
                json={"address": GUARANTOR_ADDRESS},
                headers=auth_headers(sme),
            )
            assert entered.status_code == 200, entered.text
        submitted = await client.post(f"/loans/applications/{app_id}/submit", headers=auth_headers(sme))
        assert submitted.status_code == 200, submitted.text
        await attach_required_documents(app_id)
        if approve:
            admin = await make_admin()
            decided = await client.post(
                f"/admin/applications/{app_id}/decision",
                json={"decision": "APPROVED", "note": "ok"},
                headers=admin["headers"],
            )
            assert decided.status_code == 200, decided.text
        if lock:
            db_session.add(
                ScoreRun(
                    application_id=UUID(app_id),
                    status="LOCKED",
                    locked_at=dt.datetime.now(dt.timezone.utc),
                    final_grade_precise=72.0,
                    interest_rate_pct=14.5,
                )
            )
            await db_session.commit()
        return SimpleNamespace(sme=sme, app_id=app_id)

    return _make


def _with_fake_signature(pdf: bytes, *markers: str) -> bytes:
    """`pdf` as a signing tool leaves it: a signature dictionary added after the original, then markers steering
    the fake NEAC, and the original startxref so it still reads (as tests/market/test_investor_service_agreement)."""
    added = b"\n9 0 obj\n<< /Type /Sig /ByteRange [0 120 4216 380] /Contents <3082> >>\nendobj\n"
    added += b"".join(f"%{m}\n".encode() for m in markers)
    startxref = re.findall(rb"startxref\s+(\d+)", pdf)
    if not startxref:
        return pdf + added
    return pdf + added + b"startxref\n" + startxref[-1] + b"\n%%EOF\n"


def _fill_signature_fields(pdf: bytes, names) -> bytes:
    """`pdf` with each named signature field signed (a value pointing at a signature dictionary), as a signing tool
    leaves it. The signature itself is not real: the fake NEAC decides whether it is valid."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    for page in writer.pages:
        for annot in page.get("/Annots") or []:
            widget = annot.get_object()
            if widget.get("/FT") == "/Sig" and widget.get("/T") in names:
                sig = DictionaryObject({
                    NameObject("/Type"): NameObject("/Sig"),
                    NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
                    NameObject("/SubFilter"): NameObject("/ETSI.CAdES.detached"),
                    NameObject("/ByteRange"): ArrayObject([NumberObject(n) for n in (0, 120, 4216, 380)]),
                    NameObject("/Contents"): ByteStringObject(b"\x30\x82"),
                })
                widget[NameObject("/V")] = writer._add_object(sig)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _markers(pdf: bytes, *markers: str) -> bytes:
    """Markers steering the fake NEAC (conftest.fake_neac) after the PDF, then its own startxref again."""
    startxref = re.findall(rb"startxref\s+(\d+)", pdf)[-1]
    return pdf + b"\n" + b"".join(f"%{m}\n".encode() for m in markers) + b"startxref\n" + startxref + b"\n%%EOF\n"


ALL_FIELDS = ("SME_Signature", "Guarantor_Signature")


async def _download(client, case, kind: str) -> bytes:
    path = kind  # service-agreement | standing-offer | loan-contract
    resp = await client.get(f"/loans/applications/{case.app_id}/{path}", headers=auth_headers(case.sme))
    assert resp.status_code == 200, resp.text
    return resp.content


async def _signed(client, case, kind: str, *, signers=(TAX,), extra=(), fields=ALL_FIELDS) -> bytes:
    """The document downloaded for this application with its signature `fields` signed, by `signers` (the IDs the
    fake NEAC's certificates carry)."""
    signed = _fill_signature_fields(await _download(client, case, kind), fields)
    return _markers(signed, *[f"FAKE-NEAC-SIGNER:{s}" for s in signers], *extra)


async def _upload(client, case, kind: str, content: bytes, filename="da-ky.pdf"):
    return await client.post(
        f"/loans/applications/{case.app_id}/listing-documents/{kind}",
        files={"file": (filename, content, "application/pdf")},
        headers=auth_headers(case.sme),
    )


async def _sign_offer(client, case):
    """The Standing Offer signed by the company and the Guarantor and on file: the Loan Contract cites it."""
    resp = await _upload(
        client, case, "standing-offer", await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _project_status(db_session, app_id) -> str:
    db_session.expire_all()
    return (
        await db_session.execute(
            select(Project.status)
            .join(LoanApplication, LoanApplication.project_id == Project.id)
            .where(LoanApplication.id == UUID(app_id))
        )
    ).scalar_one()


def _text(pdf: bytes) -> str:
    """The PDF's text, every run of whitespace one space: a value wrapped at the end of a line still reads whole."""
    return " ".join("\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages).split())


async def test_submitting_no_longer_needs_a_signed_agreement(client, make_user):
    sme = await make_user(role="SME")
    app_id = await _apply(client, sme)
    resp = await client.post(f"/loans/applications/{app_id}/submit", headers=auth_headers(sme))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SUBMITTED"


async def test_nothing_can_be_signed_before_fundlok_approves(client, approved, stored):
    case = await approved(approve=False, lock=False)

    status = await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    assert status.status_code == 200, status.text
    assert status.json()["ready"] is False and status.json()["published"] is False

    for path in ("standing-offer", "loan-contract"):
        early = await client.get(f"/loans/applications/{case.app_id}/{path}", headers=auth_headers(case.sme))
        assert early.status_code == 409, path
        assert early.headers["X-Error-Code"] == "SIGNING_NOT_OPEN"

    pdf = _with_fake_signature(await _download(client, case, "service-agreement"), f"FAKE-NEAC-SIGNER:{TAX}")
    refused = await _upload(client, case, "service-agreement", pdf)
    assert refused.status_code == 409
    assert refused.headers["X-Error-Code"] == "SIGNING_NOT_OPEN"


async def test_an_operator_approval_without_locked_terms_is_not_ready(client, approved):
    # The rate comes from the LOCKED score run: without it there are no terms to sign.
    case = await approved(lock=False)
    status = await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    assert status.json()["ready"] is False


async def test_the_agreement_and_the_offer_carry_the_approved_terms(client, approved):
    case = await approved()

    agreement = _text(await _download(client, case, "service-agreement"))
    assert "14,5%/năm" in agreement and "14.5% a year" in agreement
    assert agreement_text.RATE_AFTER_ASSESSMENT[0] not in agreement

    offer_pdf = await _download(client, case, "standing-offer")
    offer = _text(offer_pdf)
    # The Offeror, the Guarantor from the eKYC, and the Listing details.
    for expected in ("CONG TY TNHH AGREEMENT TEST", TAX, "0071000123456", GUARANTOR_NAME, GUARANTOR_ID,
                     "80.000.000 VND", "14,5", "4 tháng", "21 × 4 = 84", "10.000.000 VND"):
        assert expected in offer, expected
    # The assessment it rests on and what the loan costs, by the repayment schedule's rules: the score, interest
    # 80M × 14.5% × 4/12, the total, floor(total / 84) a day with the rest on the last day, backstop ceil(4 × 84 / 3).
    for expected in ("72,00 / 100", "3.866.667 VND", "83.866.667 VND", "998.412 VND × 84", "998.471", "112",
                     "không phải xếp hạng tín dụng"):
        assert expected in offer, expected

    # One empty signature field per signer; nothing signed yet.
    assert signature_fields(offer_pdf) == {"SME_Signature": False, "Guarantor_Signature": False}
    assert signature_fields(await _download(client, case, "service-agreement")) == {"SME_Signature": False}
    ref = case.app_id.split("-")[0].upper()
    assert ref in offer
    assert offer.count(standing_offer_text.VERSION) >= 2  # on every page
    # The whole offer, not a stub: the last article and the Annex.
    assert "Điều 8." in offer and "văn bản số 5" in offer
    # Laid out as a Vietnamese contract: the national title and motto, the date, the title and its number; every
    # party's details filled in (the one dotted blank left is the revenue share, which nothing sets yet), and who
    # signs under each box.
    for expected in ("CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM", "Độc lập - Tự do - Hạnh phúc",
                     "THE SOCIALIST REPUBLIC OF VIETNAM", "Independence - Freedom - Happiness",
                     "ĐỀ NGHỊ GIAO KẾT HỢP ĐỒNG CHO VAY", f"Số / No.: {ref}/ĐNGK-FL", "tháng", "năm",
                     GUARANTOR_ADDRESS, "12 Nguyen Hue", "Tran Van Owner (Giam doc)"):
        assert expected in offer, expected
    assert offer.count(agreement_text.BLANK) == 1
    assert "HỢP ĐỒNG DỊCH VỤ DÀNH CHO DOANH NGHIỆP" in agreement and f"Số / No.: {ref}/HĐDV-FL" in agreement
    assert agreement.count(agreement_text.BLANK) == 0 and agreement_text.PARTY_A_SIGNER in agreement


async def test_the_loan_contract_template_carries_the_parties_and_the_approved_terms(
    client, approved, stored, fake_neac
):
    case = await approved()
    await _sign_offer(client, case)
    contract_pdf = await _download(client, case, "loan-contract")
    contract = _text(contract_pdf)
    ref = case.app_id.split("-")[0].upper()
    offer_date = dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).strftime("%d/%m/%Y")
    # Borrower and Guarantor as on the Offer; the rate, term, N0 = 21 × 4 and backstop ceil(4 × 84 / 3) = 112; the
    # signed Offer it cites, by number and the day it was accepted.
    for expected in ("HỢP ĐỒNG CHO VAY", "CONG TY TNHH AGREEMENT TEST", TAX, "0071000123456", GUARANTOR_NAME,
                     GUARANTOR_ID, GUARANTOR_ADDRESS, "14,5%/năm", "4 tháng", "84 ngày làm việc", "112",
                     f"Đề nghị giao kết số {ref}/ĐNGK-FL ngày {offer_date}", "Điều 15.", "Lịch trả nợ",
                     "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM", f"Số / No.: {ref}/HĐCV-FL"):
        assert expected in contract, expected
    assert contract.count(loan_contract_text.VERSION) >= 2  # on every page
    # What each Investor's copy fills in, as the rule it follows, and the amounts at the 10M minimum ticket:
    # I = 10M × 14.5% × 4/12, T = P + I, A = floor(T / 84), the rest on the last day; cap = the approved 80M.
    for expected in ("Nhà đầu tư chấp nhận Đề nghị giao kết số", "từ 10.000.000 VND",
                     "không vượt 80.000.000 VND", "P × 14,5% × 4 / 12", "483.333 VND", "10.483.333 VND",
                     "124.801 VND", "124.850 VND", "Ngày đăng + 14 ngày", "thời điểm cam kết"):
        assert expected in contract, expected
    # The one dotted blank left is the revenue share, which nothing sets yet.
    assert contract.count(agreement_text.BLANK) == 1
    # One empty signature field per signer: the SME and the Guarantor sign this template now.
    assert signature_fields(contract_pdf) == {"SME_Signature": False, "Guarantor_Signature": False}


async def test_the_loan_contract_waits_for_the_signed_offer(client, approved, stored, fake_neac):
    case = await approved()
    status = (
        await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    ).json()
    assert status["loan_contract"]["waiting_for"] == "standing-offer"
    assert status["standing_offer"]["waiting_for"] is None

    early = await client.get(f"/loans/applications/{case.app_id}/loan-contract", headers=auth_headers(case.sme))
    assert early.status_code == 409 and early.headers["X-Error-Code"] == "SIGNING_OFFER_FIRST"
    # Nor is a copy accepted before then (a template from somewhere else, say).
    pdf = _with_fake_signature(await _download(client, case, "standing-offer"), f"FAKE-NEAC-SIGNER:{TAX}")
    refused = await _upload(client, case, "loan-contract", pdf)
    assert refused.status_code == 409 and refused.headers["X-Error-Code"] == "SIGNING_OFFER_FIRST"

    signed = await _sign_offer(client, case)
    assert signed.json()["loan_contract"]["waiting_for"] is None
    assert (await client.get(f"/loans/applications/{case.app_id}/loan-contract", headers=auth_headers(case.sme))
            ).status_code == 200


async def test_missing_details_must_be_completed_before_signing(client, db_session, approved, stored, fake_neac):
    # The Guarantor's eKYC carries no address: the Offer and the Loan Contract print it as a blank until the SME
    # enters it; the Service Agreement does not name the Guarantor and can be signed meanwhile.
    case = await approved(guarantor_address=False)
    status = (
        await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    ).json()
    assert status["standing_offer"]["missing_details"] == ["GUARANTOR_ADDRESS"]
    assert status["loan_contract"]["missing_details"] == ["GUARANTOR_ADDRESS"]
    assert status["service_agreement"]["missing_details"] == []
    assert status["guarantor_address"] is None and status["guarantor_address_source"] is None

    blank = await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    refused = await _upload(client, case, "standing-offer", blank)
    assert refused.status_code == 422 and refused.headers["X-Error-Code"] == "SIGNING_DETAILS_MISSING"
    assert "GUARANTOR_ADDRESS" in refused.json()["detail"]
    assert fake_neac.calls == []
    assert (await _upload(client, case, "service-agreement", await _signed(client, case, "service-agreement"))
            ).status_code == 200

    too_short = await client.put(
        f"/loans/applications/{case.app_id}/guarantor-address", json={"address": "  Hue  "},
        headers=auth_headers(case.sme),
    )
    assert too_short.status_code == 422
    entered = await client.put(
        f"/loans/applications/{case.app_id}/guarantor-address",
        json={"address": "  45 Le Loi,\n Phuong 4,   TP. Vung Tau "},
        headers=auth_headers(case.sme),
    )
    assert entered.status_code == 200, entered.text
    body = entered.json()
    assert body["guarantor_address"] == GUARANTOR_ADDRESS and body["guarantor_address_source"] == "SME"
    assert body["standing_offer"]["missing_details"] == [] and body["loan_contract"]["missing_details"] == []
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "GUARANTOR_ADDRESS_SET"))
    ).scalar_one()
    assert audit.after_state == {"guarantor_address": GUARANTOR_ADDRESS} and str(audit.actor_id) == case.sme["id"]

    # The copy signed with the blank is outdated now; a fresh one carries the address and is accepted.
    assert (await _upload(client, case, "standing-offer", blank)).headers["X-Error-Code"] == "SIGNING_DOCUMENT_OUTDATED"
    fresh = await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    assert GUARANTOR_ADDRESS in _text(fresh)
    assert (await _upload(client, case, "standing-offer", fresh)).status_code == 200

    # Signed with it: it no longer changes.
    late = await client.put(
        f"/loans/applications/{case.app_id}/guarantor-address", json={"address": "1 Tran Hung Dao, Ha Noi"},
        headers=auth_headers(case.sme),
    )
    assert late.status_code == 409 and late.headers["X-Error-Code"] == "SIGNING_ALREADY_SIGNED"


async def test_the_companys_address_comes_from_the_project_without_one_on_the_kyb(client, db_session, approved):
    from app.gverify.models import GVerifyKybVerification

    case = await approved()
    kyb = (
        await db_session.execute(
            select(GVerifyKybVerification).where(GVerifyKybVerification.user_id == UUID(case.sme["id"]))
        )
    ).scalar_one()
    kyb.company_address = None
    project = (
        await db_session.execute(
            select(Project).join(LoanApplication, LoanApplication.project_id == Project.id)
            .where(LoanApplication.id == UUID(case.app_id))
        )
    ).scalar_one()
    project.address = {"street": "88 Dong Khoi", "city": "Ho Chi Minh City", "state": "", "postal_code": "700000",
                       "country": "Vietnam"}
    await db_session.commit()
    agreement = _text(await _download(client, case, "service-agreement"))
    assert "88 Dong Khoi, Ho Chi Minh City, 700000, Vietnam" in agreement

    # Neither: a blank the SME must fill (on its project) before signing.
    project.address = None
    await db_session.commit()
    status = (
        await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    ).json()
    assert status["service_agreement"]["missing_details"] == ["COMPANY_ADDRESS"]
    assert "COMPANY_ADDRESS" in status["standing_offer"]["missing_details"]


async def test_signing_all_three_publishes_the_project(client, db_session, approved, stored, fake_neac, make_admin):
    case = await approved()
    assert await _project_status(db_session, case.app_id) == "DRAFT", "approved is not yet published"

    first = await _upload(client, case, "service-agreement", await _signed(client, case, "service-agreement"))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["service_agreement"]["signed"] is True
    assert body["standing_offer"]["signed"] is False and body["loan_contract"]["signed"] is False
    assert body["published"] is False
    assert await _project_status(db_session, case.app_id) == "DRAFT", "one of three is not enough"

    second = await _upload(
        client, case, "standing-offer",
        await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID)),
    )
    assert second.status_code == 200, second.text
    assert second.json()["published"] is False
    assert await _project_status(db_session, case.app_id) == "DRAFT", "two of three is not enough"

    third = await _upload(
        client, case, "loan-contract",
        await _signed(client, case, "loan-contract", signers=(TAX, GUARANTOR_ID)),
    )
    assert third.status_code == 200, third.text
    assert third.json()["published"] is True and third.json()["loan_contract"]["signed"] is True
    assert await _project_status(db_session, case.app_id) == "ACTIVE"

    # Kept: all three as documents of the application, each with NEAC's verdict in the audit log.
    docs = (
        await db_session.execute(
            select(LoanApplicationDocument).where(LoanApplicationDocument.loan_application_id == UUID(case.app_id))
        )
    ).scalars().all()
    kinds = ("sme_service_agreement", "sme_standing_offer", "sme_loan_contract")
    signed = {d.document_type: d for d in docs if d.document_type in kinds}
    assert set(signed) == set(kinds)
    assert all(d.status == "UPLOADED" and d.file_key in stored for d in signed.values())
    verdicts = (
        await db_session.execute(
            select(AuditLog.after_state).where(
                AuditLog.action == "NEAC_SIGNATURE_CHECKED",
                AuditLog.entity_id.in_([d.id for d in signed.values()]),
            )
        )
    ).scalars().all()
    assert len(verdicts) == 3 and all(v["status"] == "VERIFIED" and v["terms_match"] for v in verdicts)
    offer_verdict = next(v for v in verdicts if v["agreement_version"] == standing_offer_text.VERSION)
    assert offer_verdict["guarantor"]["expected_id"] == GUARANTOR_ID
    assert offer_verdict["signature_fields"] == {"SME_Signature": True, "Guarantor_Signature": True}

    # What the automatic check established, for the SME's signing card.
    status = (
        await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    ).json()
    offer_checks = status["standing_offer"]["checks"]
    assert offer_checks["terms_match"] is True and offer_checks["verified_by"] == "NEAC"
    assert [s["role"] for s in offer_checks["signers"]] == ["SME", "GUARANTOR"]
    assert [s["role"] for s in status["service_agreement"]["checks"]["signers"]] == ["SME"]
    assert [s["role"] for s in status["loan_contract"]["checks"]["signers"]] == ["SME", "GUARANTOR"]

    # Staff see the same verdict in the admin project panel straight away, guarantor and numbers included.
    admin = await make_admin()
    project_id = (
        await db_session.execute(select(LoanApplication.project_id).where(LoanApplication.id == UUID(case.app_id)))
    ).scalar_one()
    panel = (await client.get(f"/admin/projects/{project_id}", headers=admin["headers"])).json()
    shown = {d["document_type"]: d["signature_check"] for a in panel["applications"] for d in a["documents"]}
    assert shown["sme_standing_offer"]["status"] == "VERIFIED"
    assert shown["sme_standing_offer"]["guarantor_required"] is True
    assert shown["sme_standing_offer"]["guarantor_matches"] is True
    assert shown["sme_standing_offer"]["terms_match"] is True
    assert shown["sme_standing_offer"]["expected_source"] == "KYB"
    assert shown["sme_service_agreement"]["guarantor_required"] is False
    assert shown["sme_loan_contract"]["guarantor_matches"] is True and shown["sme_loan_contract"]["terms_match"]

    # The SME can read back exactly what it uploaded.
    copy = await client.get(
        f"/loans/applications/{case.app_id}/listing-documents/standing-offer/signed", headers=auth_headers(case.sme)
    )
    assert copy.status_code == 200
    assert copy.content == stored[signed["sme_standing_offer"].file_key]


async def test_the_guarantor_signs_by_zalo_code_and_that_publishes_the_project(
    client, db_session, approved, stored, fake_neac, outbox
):
    """Money flow FINAL v1.0, Day 0: the SME signs with its digital signature, the guarantor by a Zalo code."""
    case = await approved()
    for kind in ("service-agreement", "standing-offer", "loan-contract"):
        # Only the company's signature in the PDF: accepted now; the guarantor signs separately.
        resp = await _upload(client, case, kind, await _signed(client, case, kind, fields=("SME_Signature",)))
        assert resp.status_code == 200, (kind, resp.text)
    body = resp.json()
    assert body["published"] is False and body["guarantor_signed_by"] is None
    assert body["guarantor_missing"] == [] and body["guarantor_phone"].startswith("09")
    assert await _project_status(db_session, case.app_id) == "DRAFT", "three documents without the guarantor"

    sent = await _guarantor_code(client, case)
    assert sent.status_code == 200, sent.text
    assert sent.json()["channel"] == "zalo" and sent.json()["expires_in"] == 300
    assert len(outbox) == 1 and outbox[0]["to"].startswith("849")
    wrong = await _guarantor_sign(client, case, "000000" if outbox[0]["code"] != "000000" else "111111")
    assert wrong.status_code == 400 and wrong.headers["X-Error-Code"] == "CODE_INCORRECT"
    db_session.expire_all()
    assert (await db_session.execute(select(GuarantorSigningCode.attempts))).scalar_one() == 1  # the guess counts

    signed = await _guarantor_sign(client, case, outbox[0]["code"])
    assert signed.status_code == 200, signed.text
    assert signed.json()["published"] is True and signed.json()["guarantor_signed_by"] == "ZALO_OTP"
    assert signed.json()["guarantor_signed_at"] is not None
    assert await _project_status(db_session, case.app_id) == "ACTIVE"

    signature = (await db_session.execute(select(GuarantorSignature))).scalar_one()
    assert signature.signer_id_number == GUARANTOR_ID and signature.purpose == "LISTING_DOCUMENTS"
    assert {d["document_type"] for d in signature.documents} == {"sme_standing_offer", "sme_loan_contract"}
    assert all(len(d["sha256"]) == 64 and d["check_code"] for d in signature.documents)
    audit = (
        await db_session.execute(select(AuditLog.after_state).where(AuditLog.action == "GUARANTOR_SIGNED_BY_ZALO_OTP"))
    ).scalar_one()
    assert audit["signed_by"] == "ZALO_OTP" and audit["phone_masked"].startswith("09")

    again = await _guarantor_code(client, case)
    assert again.status_code == 409 and again.headers["X-Error-Code"] == "GUARANTOR_ALREADY_SIGNED"


async def test_the_guarantor_signs_only_after_the_sme_signed_copies_are_on_file(client, approved, stored, fake_neac, outbox):
    case = await approved()
    early = await _guarantor_code(client, case)
    assert early.status_code == 409 and early.headers["X-Error-Code"] == "GUARANTOR_SIGNS_AFTER_UPLOAD"
    assert outbox == []
    await _sign_offer(client, case)
    still = await _guarantor_code(client, case)
    assert still.headers["X-Error-Code"] == "GUARANTOR_SIGNS_AFTER_UPLOAD"  # the Loan Contract is not on file yet


async def test_a_code_for_another_listing_does_not_sign_this_one(client, db_session, approved, stored, fake_neac, outbox):
    case = await approved()
    for kind in ("standing-offer", "loan-contract"):
        assert (await _upload(client, case, kind, await _signed(client, case, kind, fields=("SME_Signature",)))).status_code == 200
    assert (await _guarantor_code(client, case)).status_code == 200
    code = await db_session.execute(select(GuarantorSigningCode))
    row = code.scalar_one()
    row.subject_id = UUID(int=1)  # as if it had been sent for another application
    await db_session.commit()
    refused = await _guarantor_sign(client, case, outbox[0]["code"])
    assert refused.status_code == 400 and refused.headers["X-Error-Code"] == "CODE_EXPIRED"


async def test_a_copy_with_the_guarantors_own_digital_signature_still_counts(client, db_session, approved, stored, fake_neac):
    """Copies signed in the PDF by the guarantor before the Zalo code keep working: no code needed."""
    case = await approved()
    await _upload(client, case, "service-agreement", await _signed(client, case, "service-agreement"))
    await _sign_offer(client, case)
    done = await _upload(
        client, case, "loan-contract", await _signed(client, case, "loan-contract", signers=(TAX, GUARANTOR_ID))
    )
    assert done.status_code == 200, done.text
    assert done.json()["published"] is True and done.json()["guarantor_signed_by"] == "DIGITAL_SIGNATURE"
    assert await _project_status(db_session, case.app_id) == "ACTIVE"


async def test_signing_waits_for_a_ready_guarantor(client, approved, stored, fake_neac, outbox):
    """Ops rule "Guarantor ready before Day 0": an approved eKYC and a verified phone, before anything is accepted."""
    case = await approved(phone=False)
    status = await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    assert status.json()["guarantor_missing"] == ["PHONE"] and status.json()["guarantor_phone"] is None
    refused = await _upload(client, case, "service-agreement", await _signed(client, case, "service-agreement"))
    assert refused.status_code == 409 and refused.headers["X-Error-Code"] == "SIGNING_GUARANTOR_NOT_READY"
    assert "PHONE" in refused.json()["detail"]
    assert fake_neac.calls == []


async def test_the_standing_offer_needs_a_verified_guarantor(client, approved, stored):
    # Without the eKYC there is no ID number to print, or to check the Guarantor's signature against.
    case = await approved(kyc=False)
    status = await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    assert status.json()["guarantor_verified"] is False
    assert status.json()["standing_offer"]["missing_details"] == ["GUARANTOR_ID"]
    refused = await _upload(
        client, case, "standing-offer", await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    )
    assert refused.status_code == 422
    assert refused.headers["X-Error-Code"] == "SIGNING_DETAILS_MISSING"


@pytest.mark.parametrize(
    ("make", "code", "http"),
    [
        # The PDF as downloaded, never signed.
        (lambda pdf: pdf, "SIGNING_UNSIGNED", 422),
        (lambda pdf: b"PK\x03\x04 not a pdf", "SIGNING_NOT_PDF", 415),
        # Someone else's signature, valid as such.
        (lambda pdf: _with_fake_signature(pdf, "FAKE-NEAC-SIGNER:0101010101"), "SIGNING_SIGNER_MISMATCH", 422),
        (lambda pdf: _with_fake_signature(pdf, f"FAKE-NEAC-SIGNER:{TAX}", "FAKE-NEAC:INVALID"),
         "SIGNING_SIGNATURE_INVALID", 422),
        (lambda pdf: _with_fake_signature(pdf, f"FAKE-NEAC-SIGNER:{TAX}", "FAKE-NEAC:REVOKED"),
         "SIGNING_SIGNATURE_INVALID", 422),
        (lambda pdf: _with_fake_signature(pdf, f"FAKE-NEAC-SIGNER:{TAX}", "FAKE-NEAC:DOWN"), "NEAC_UNAVAILABLE", 503),
    ],
)
async def test_a_signed_agreement_that_does_not_check_out_is_refused(client, db_session, approved, stored, make,
                                                                      code, http):
    case = await approved()
    refused = await _upload(client, case, "service-agreement", make(await _download(client, case, "service-agreement")))
    assert refused.status_code == http, refused.text
    assert refused.headers["X-Error-Code"] == code
    assert not stored, "nothing is kept from a refused upload"
    status = await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    assert status.json()["service_agreement"]["signed"] is False


async def test_another_applications_document_is_the_wrong_document(client, approved, stored):
    mine = await approved()
    other = await approved(tax_id="0319911299")
    theirs = _with_fake_signature(await _download(client, other, "service-agreement"), f"FAKE-NEAC-SIGNER:{TAX}")
    refused = await _upload(client, mine, "service-agreement", theirs)
    assert refused.status_code == 422
    assert refused.headers["X-Error-Code"] == "SIGNING_WRONG_DOCUMENT"

    # Nor is the Service Agreement uploaded as the Standing Offer.
    agreement = await _signed(client, mine, "service-agreement", signers=(TAX, GUARANTOR_ID))
    swapped = await _upload(client, mine, "standing-offer", agreement)
    assert swapped.headers["X-Error-Code"] == "SIGNING_WRONG_DOCUMENT"


async def test_a_copy_signed_before_the_details_changed_is_outdated(client, db_session, approved, stored, fake_neac):
    # Downloaded and signed, then the bank account investors pay into changed: the signed copy names the old one.
    case = await approved()
    signed = await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    account = (
        await db_session.execute(select(PaymentAccount).where(PaymentAccount.user_id == UUID(case.sme["id"])))
    ).scalar_one()
    account.account_number = "0071000999999"
    await db_session.commit()

    refused = await _upload(client, case, "standing-offer", signed)
    assert refused.status_code == 422
    assert refused.headers["X-Error-Code"] == "SIGNING_DOCUMENT_OUTDATED"
    assert fake_neac.calls == []

    # The copy downloaded now carries the new account and the new check code.
    fresh = await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    assert "0071000999999" in _text(fresh)
    assert (await _upload(client, case, "standing-offer", fresh)).status_code == 200


async def test_a_signed_document_cannot_be_replaced(client, approved, stored):
    case = await approved()
    pdf = await _signed(client, case, "service-agreement")
    assert (await _upload(client, case, "service-agreement", pdf)).status_code == 200
    again = await _upload(client, case, "service-agreement", pdf)
    assert again.status_code == 409
    assert again.headers["X-Error-Code"] == "SIGNING_ALREADY_SIGNED"


async def test_only_the_owning_sme_signs(client, approved, make_user, stored):
    case = await approved()
    other = await make_user(role="SME")
    investor = await make_user(role="INVESTOR")
    pdf = await _signed(client, case, "service-agreement")
    for who in (other, investor):
        for resp in (
            await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(who)),
            await client.get(f"/loans/applications/{case.app_id}/standing-offer", headers=auth_headers(who)),
            await client.get(f"/loans/applications/{case.app_id}/loan-contract", headers=auth_headers(who)),
            await client.post(
                f"/loans/applications/{case.app_id}/listing-documents/service-agreement",
                files={"file": ("x.pdf", pdf, "application/pdf")},
                headers=auth_headers(who),
            ),
        ):
            assert resp.status_code == 403, resp.text
    unknown = await _upload(client, case, "credit-memo", pdf)
    assert unknown.status_code == 404


async def test_the_wizard_cannot_upload_a_signed_document_unchecked(client, make_user):
    # Signed listing documents arrive only through the NEAC-checked endpoint.
    sme = await make_user(role="SME")
    app_id = await _apply(client, sme)
    for document_type in ("sme_service_agreement", "sme_standing_offer", "sme_loan_contract"):
        resp = await client.post(
            "/uploads/init-upload",
            json={"loan_application_id": app_id, "document_type": document_type, "filename": "signed.pdf",
                  "content_type": "application/pdf", "size": 2048},
            headers=auth_headers(sme),
        )
        assert resp.status_code == 400
        assert resp.headers["X-Error-Code"] == "SIGNED_AFTER_APPROVAL"


async def test_two_uploads_at_once_still_publish(client, db_session, approved, stored, fake_neac):
    # The last two documents sent together, once the Offer is on file: each upload must see the other's, or
    # neither publishes. Sessions of their own, as two requests have (the test client shares one session).
    from app.core.database import SessionLocal
    from app.lending.models import LoanApplication as _Application
    from app.loans import signing
    from app.users.models import User

    case = await approved()
    await _sign_offer(client, case)
    contents = {
        "service-agreement": await _signed(client, case, "service-agreement"),
        "loan-contract": await _signed(client, case, "loan-contract", signers=(TAX, GUARANTOR_ID)),
    }

    async def sign(slug: str) -> None:
        async with SessionLocal() as session:
            await signing.record_signed(
                session,
                application=await session.get(_Application, UUID(case.app_id)),
                user=await session.get(User, UUID(case.sme["id"])),
                kind=signing.kind_for(slug),
                content=contents[slug],
                file_name=f"{slug}.pdf",
                now=dt.datetime.now(dt.timezone.utc),
            )
            await session.commit()

    await asyncio.gather(sign("service-agreement"), sign("loan-contract"))
    assert await _project_status(db_session, case.app_id) == "ACTIVE"


def test_every_character_of_the_three_contracts_has_a_glyph():
    # A character the embedded font lacks prints as nothing in the PDF a party signs (Contract 5's Δ did): every
    # character of the texts and of the layout, in every style.
    import inspect

    from fontTools.ttLib import TTFont

    from app.loans.agreement import pdf
    from app.loans.agreement import text as agreement_text_module

    for file in pdf.SERIF_FILES.values():
        cmap = set(TTFont(str(pdf.FONT_DIR / file)).getBestCmap())
        for module in (agreement_text_module, standing_offer_text, loan_contract_text, pdf):
            missing = {ch for ch in inspect.getsource(module) if ord(ch) > 127 and ord(ch) not in cmap}
            assert not missing, (style, module.__name__, missing)


async def test_the_loan_contracts_extension_formula_keeps_its_delta(client, approved, stored, fake_neac):
    case = await approved()
    await _sign_offer(client, case)
    assert "Δ = (U / số phải trả trong tháng × 21)" in _text(await _download(client, case, "loan-contract"))
