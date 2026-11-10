"""Signing the listing documents after approval (docs/specs/loans/listing-signing.md).

FundLok approves the request (the operator's approval and a LOCKED score run, which fixes the rate); the SME then
downloads the SME Service Agreement and the Standing Offer filled in with those terms, signs them (the Standing
Offer also by the Personal Guarantor) and uploads them back. Each upload is checked with NEAC (faked: conftest
fake_neac); the second one publishes the project.
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
from app.loans.agreement import standing_offer_text
from app.loans.agreement import text as agreement_text
from app.loans.signing import signature_fields
from conftest import auth_headers
from tests.loans.test_service_agreement import TAX, _apply, _verified_with_bank

GUARANTOR_ID = "079123456789"
GUARANTOR_NAME = "TRAN VAN OWNER"


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


@pytest.fixture
def approved(client, db_session, make_user, make_admin, attach_required_documents):
    """An SME whose request FundLok approved, with locked terms (14.5% a year), its KYB, an approved bank account
    and its owner's eKYC: everything signing needs, nothing signed yet."""

    async def _make(*, lock=True, kyc=True, approve=True, tax_id=TAX):
        sme = await make_user(role="SME")
        app_id = await _apply(client, sme, tax_id=tax_id)
        await _verified_with_bank(db_session, sme, tax_id=tax_id)
        if kyc:
            await _kyc(db_session, sme)
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
    path = "service-agreement" if kind == "service-agreement" else "standing-offer"
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
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


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

    offer = await client.get(f"/loans/applications/{case.app_id}/standing-offer", headers=auth_headers(case.sme))
    assert offer.status_code == 409
    assert offer.headers["X-Error-Code"] == "SIGNING_NOT_OPEN"

    pdf = _with_fake_signature(await _download(client, case, "service-agreement"), f"FAKE-NEAC-SIGNER:{TAX}")
    refused = await _upload(client, case, "service-agreement", pdf)
    assert refused.status_code == 409
    assert refused.headers["X-Error-Code"] == "SIGNING_NOT_OPEN"


async def test_an_operator_approval_without_locked_terms_is_not_ready(client, approved):
    # The rate comes from the LOCKED score run: without it there are no terms to sign.
    case = await approved(lock=False)
    status = await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    assert status.json()["ready"] is False


async def test_both_documents_carry_the_approved_terms(client, approved):
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


async def test_signing_both_publishes_the_project(client, db_session, approved, stored, fake_neac, make_admin):
    case = await approved()
    assert await _project_status(db_session, case.app_id) == "DRAFT", "approved is not yet published"

    first = await _upload(client, case, "service-agreement", await _signed(client, case, "service-agreement"))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["service_agreement"]["signed"] is True
    assert body["standing_offer"]["signed"] is False
    assert body["published"] is False
    assert await _project_status(db_session, case.app_id) == "DRAFT", "one of two is not enough"

    second = await _upload(
        client, case, "standing-offer",
        await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID)),
    )
    assert second.status_code == 200, second.text
    assert second.json()["published"] is True
    assert await _project_status(db_session, case.app_id) == "ACTIVE"

    # Kept: both as documents of the application, each with NEAC's verdict in the audit log.
    docs = (
        await db_session.execute(
            select(LoanApplicationDocument).where(LoanApplicationDocument.loan_application_id == UUID(case.app_id))
        )
    ).scalars().all()
    signed = {d.document_type: d for d in docs if d.document_type in ("sme_service_agreement", "sme_standing_offer")}
    assert set(signed) == {"sme_service_agreement", "sme_standing_offer"}
    assert all(d.status == "UPLOADED" and d.file_key in stored for d in signed.values())
    verdicts = (
        await db_session.execute(
            select(AuditLog.after_state).where(
                AuditLog.action == "NEAC_SIGNATURE_CHECKED",
                AuditLog.entity_id.in_([d.id for d in signed.values()]),
            )
        )
    ).scalars().all()
    assert len(verdicts) == 2 and all(v["status"] == "VERIFIED" and v["terms_match"] for v in verdicts)
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

    # The SME can read back exactly what it uploaded.
    copy = await client.get(
        f"/loans/applications/{case.app_id}/listing-documents/standing-offer/signed", headers=auth_headers(case.sme)
    )
    assert copy.status_code == 200
    assert copy.content == stored[signed["sme_standing_offer"].file_key]


async def test_the_standing_offer_needs_the_guarantors_own_signature(client, approved, stored, fake_neac):
    case = await approved()
    # Only the company's box signed: refused on the spot, before NEAC is asked.
    one_box = await _upload(
        client, case, "standing-offer",
        await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID), fields=("SME_Signature",)),
    )
    assert one_box.status_code == 422
    assert one_box.headers["X-Error-Code"] == "SIGNING_GUARANTOR_MISSING"
    assert fake_neac.calls == []

    # Both boxes signed, but both by the company: NEAC finds no signature of the guarantor's.
    only_company = await _upload(
        client, case, "standing-offer", await _signed(client, case, "standing-offer", signers=(TAX,))
    )
    assert only_company.status_code == 422
    assert only_company.headers["X-Error-Code"] == "SIGNING_GUARANTOR_MISSING"

    # The guarantor alone is not the company.
    only_guarantor = await _upload(
        client, case, "standing-offer", await _signed(client, case, "standing-offer", signers=(GUARANTOR_ID,))
    )
    assert only_guarantor.status_code == 422
    assert only_guarantor.headers["X-Error-Code"] == "SIGNING_SIGNER_MISMATCH"


async def test_the_standing_offer_needs_a_verified_guarantor(client, approved, stored):
    case = await approved(kyc=False)
    status = await client.get(f"/loans/applications/{case.app_id}/listing-documents", headers=auth_headers(case.sme))
    assert status.json()["guarantor_verified"] is False
    refused = await _upload(
        client, case, "standing-offer", await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID))
    )
    assert refused.status_code == 422
    assert refused.headers["X-Error-Code"] == "SIGNING_GUARANTOR_UNVERIFIED"


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
            await client.post(
                f"/loans/applications/{case.app_id}/listing-documents/service-agreement",
                files={"file": ("x.pdf", pdf, "application/pdf")},
                headers=auth_headers(who),
            ),
        ):
            assert resp.status_code == 403, resp.text
    unknown = await _upload(client, case, "loan-contract", pdf)
    assert unknown.status_code == 404


async def test_the_wizard_cannot_upload_a_signed_document_unchecked(client, make_user):
    # Signed listing documents arrive only through the NEAC-checked endpoint.
    sme = await make_user(role="SME")
    app_id = await _apply(client, sme)
    for document_type in ("sme_service_agreement", "sme_standing_offer"):
        resp = await client.post(
            "/uploads/init-upload",
            json={"loan_application_id": app_id, "document_type": document_type, "filename": "signed.pdf",
                  "content_type": "application/pdf", "size": 2048},
            headers=auth_headers(sme),
        )
        assert resp.status_code == 400
        assert resp.headers["X-Error-Code"] == "SIGNED_AFTER_APPROVAL"


async def test_two_uploads_at_once_still_publish(client, db_session, approved, stored):
    # The two documents sent together: each upload must see the other's, or neither publishes. Two sessions of
    # their own, as two requests have (the test client shares one session between requests).
    from app.core.database import SessionLocal
    from app.lending.models import LoanApplication as _Application
    from app.loans import signing
    from app.users.models import User

    case = await approved()
    contents = {
        "service-agreement": await _signed(client, case, "service-agreement"),
        "standing-offer": await _signed(client, case, "standing-offer", signers=(TAX, GUARANTOR_ID)),
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

    await asyncio.gather(sign("service-agreement"), sign("standing-offer"))
    assert await _project_status(db_session, case.app_id) == "ACTIVE"
