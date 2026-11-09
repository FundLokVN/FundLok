"""Contract 4, the Investor Service Agreement, signed with the investor's first commitment: agreeing holds the
amount and gives it a code (no money moves); the investor downloads the agreement filled in with their details and
that commitment's Annex, signs it with their digital signature and uploads it; only then are the payment details
given. Every later commitment keeps its own filled-in Investment Confirmation (the Annex) when the investor agrees.
Spec: docs/specs/market/investor-service-agreement.md
"""
import datetime as dt
import io
import json
import re
import uuid

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import EvidenceFile, PaymentAccount, Tranche
from app.direct_transfer.signing import MockSigner
from app.direct_transfer.storage import InMemoryStorage
from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification, GVerifyVerification
from app.lending.models import AuditLog, Project
from app.market.funding import INVESTOR_TERMS_VERSION
from app.market.investor_agreement import service as investor_agreement
from app.market.investor_agreement import text as agreement_text
from app.market.investor_agreement.models import InvestorServiceAgreement, SignedInvestmentConfirmation
from conftest import SIGNED_AGREEMENT_PDF
from tests.market.test_funding_flow import _open_project

VERSION = investor_agreement.VERSION
UNSIGNED_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


async def _reserve(client, investor, window_id, amount=10_000_000, *, key=None):
    body = {"amount_vnd": amount, "ack_risk_disclosure": True, "terms_version": INVESTOR_TERMS_VERSION}
    headers = investor["headers"] | {"Idempotency-Key": key or str(uuid.uuid4())}
    return await client.post(f"/funding/windows/{window_id}/reservations", json=body, headers=headers)


def _with_fake_signature(pdf: bytes, *markers: str) -> bytes:
    """`pdf` as a signing tool leaves it, for the tests: a signature dictionary added after the original, then
    markers that steer the fake NEAC (conftest.fake_neac), and the original startxref so it still reads."""
    added = b"\n9 0 obj\n<< /Type /Sig /ByteRange [0 120 4216 380] /Contents <3082> >>\nendobj\n"
    added += b"".join(f"%{m}\n".encode() for m in markers)
    startxref = re.findall(rb"startxref\s+(\d+)", pdf)
    if not startxref:  # not a PDF (a refused download): the upload is refused before reading it
        return pdf + added
    return pdf + added + b"startxref\n" + startxref[-1] + b"\n%%EOF\n"


async def _signed_copy(client, investor, tranche_id, *markers, signer_id="auto", name=None):
    """The agreement downloaded for this commitment, "signed": by default by the investor (their ID as the fake
    NEAC's certificate carries it)."""
    pdf = (
        await client.get(
            "/funding/me/investor-agreement/document", params={"tranche_id": str(tranche_id)},
            headers=investor["headers"],
        )
    ).content
    if signer_id == "auto":
        status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
        signer_id = (status.get("party_b") or {}).get("id_number")
    marks = [*markers]
    if signer_id:
        marks.append(f"FAKE-NEAC-SIGNER:{signer_id}")
    if name:
        marks.append(f"FAKE-NEAC-NAME:{name}")
    return _with_fake_signature(pdf, *marks)


async def _upload(client, investor, tranche_id, content=None, *, version=VERSION,
                  filename="hop-dong-da-ky.pdf", content_type="application/pdf"):
    if content is None:
        content = await _signed_copy(client, investor, tranche_id)
    return await client.post(
        "/funding/me/investor-agreement",
        data={"version": version, "tranche_id": str(tranche_id)},
        files={"file": (filename, content, content_type)},
        headers=investor["headers"],
    )


async def _payment(client, investor, tranche_id):
    return await client.get(f"/funding/tranches/{tranche_id}/payment", headers=investor["headers"])


async def _verified_investor(db_session, make_user, make_eligible_investor, *, name="NGUYEN VAN A",
                             id_number="079203001234"):
    """An eligible investor, without the agreement yet, whose approved eKYC carries a name and an ID number."""
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    kyc = (
        await db_session.execute(
            select(GVerifyVerification).where(GVerifyVerification.user_id == uuid.UUID(investor["id"]))
        )
    ).scalar_one()
    kyc.full_name, kyc.person_number = name, id_number
    await db_session.commit()
    return investor


async def _new_investor(make_user, make_eligible_investor):
    """An eligible investor who has not signed the agreement yet."""
    return await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)


async def _signatures(db_session, investor):
    db_session.expire_all()
    return (
        await db_session.execute(
            select(InvestorServiceAgreement)
            .where(InvestorServiceAgreement.investor_id == uuid.UUID(investor["id"]))
            .order_by(InvestorServiceAgreement.signed_at)
        )
    ).scalars().all()


async def _confirmations(db_session):
    db_session.expire_all()
    return (await db_session.execute(select(SignedInvestmentConfirmation))).scalars().all()


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


@pytest.fixture
def storage(monkeypatch):
    """One evidence store for the whole test, so a stored file can be read back."""
    shared = InMemoryStorage()
    monkeypatch.setattr("app.market.funding.evidence_storage", lambda: shared)
    return shared


@pytest.fixture
def signed_documents(monkeypatch):
    """Every document signed through the platform signer, in order, as parsed JSON."""
    documents: list[dict] = []

    class RecordingSigner(MockSigner):
        async def sign(self, *, document, signer, now):
            documents.append(json.loads(document))
            return await super().sign(document=document, signer=signer, now=now)

    monkeypatch.setattr("app.market.funding._signer", RecordingSigner)
    return documents


# --------------------------------------------------------------------------- #
# the first commitment: code first, then the agreement, then the payment details
# --------------------------------------------------------------------------- #
async def test_a_first_commitment_gets_its_code_but_no_payment_details_until_signed(
    client, db_session, make_admin, make_user, make_eligible_investor, signed_documents
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    signed_documents.clear()  # opening the window signed the SME's standing offer

    resp = await _reserve(client, investor, window_id, 20_000_000)

    assert resp.status_code == 201, resp.text
    tranche = resp.json()
    assert tranche["status"] == "SIGNED" and tranche["reference_code"]  # the commitment and its code exist
    pay = await _payment(client, investor, tranche["id"])
    assert pay.status_code == 409 and pay.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_REQUIRED"  # no money yet
    assert await _signatures(db_session, investor) == []
    assert await _confirmations(db_session) == []  # the Annex is signed with the agreement, next
    [acceptance] = signed_documents
    assert acceptance["investor_service_agreement"] == {"version": VERSION, "signed_with_this_commitment_next": True}
    assert acceptance["investment_confirmation"]["committed_amount_vnd"] == 20_000_000
    status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert status["required"] is True


async def test_the_agreement_pdf_is_filled_in_with_the_investor_and_the_commitments_annex(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id, listing_id = setup["window"].id, setup["window"].listing_id
    project = await db_session.get(Project, setup["project"])
    sme_name = project.legal_name
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    account = (
        await db_session.execute(select(PaymentAccount).where(PaymentAccount.user_id == uuid.UUID(investor["id"])))
    ).scalar_one()
    tranche = (await _reserve(client, investor, window_id, 20_000_000)).json()

    resp = await client.get(
        "/funding/me/investor-agreement/document", params={"tranche_id": tranche["id"]}, headers=investor["headers"]
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline;")
    assert resp.headers["cache-control"] == "no-store"
    assert tranche["reference_code"] in resp.headers["content-disposition"]
    assert b"/ByteRange" not in resp.content  # FundLok's own download is never signed
    body = _pdf_text(resp.content)
    # Party B, from the eKYC and the approved bank account; the address GVerify does not give is a blank.
    for expected in ("NGUYEN VAN A", "079203001234", account.account_number, "TEST INVESTOR", "Vietcombank"):
        assert expected in body, expected
    assert "địa chỉ ................" in body
    # The Annex: this commitment's code and every row filled in.
    for expected in (
        f"Mã cam kết / Commitment: {tranche['reference_code']}",
        str(listing_id).split("-")[0].upper(),
        sme_name,
        "20.000.000 VND",
        "16%/năm, 4 tháng",
        "P × 2,5% × M / 12 = 166.666 VND + VAT",
        ict_date(dt.datetime.fromisoformat(tranche["hold_expires_at"])).strftime("%d/%m/%Y"),
    ):
        assert expected in body, expected
    # Party A, the last article, the version on every page: the whole contract.
    assert "1501167629" in body and "Điều 11." in body and body.count(VERSION) >= 2

    download = await client.get(
        "/funding/me/investor-agreement/document", params={"tranche_id": tranche["id"], "download": "true"},
        headers=investor["headers"],
    )
    assert download.headers["content-disposition"].startswith("attachment;")
    # Without a commitment, the Annex is the blank template.
    blank = _pdf_text((await client.get("/funding/me/investor-agreement/document", headers=investor["headers"])).content)
    assert "Mã cam kết / Commitment" not in blank and "................ VND (≥ 10.000.000)" in blank


async def test_uploading_the_signed_agreement_keeps_the_first_annex_and_opens_payment(
    client, db_session, make_admin, make_user, make_eligible_investor, storage
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    tranche = (await _reserve(client, investor, window_id, 20_000_000)).json()
    signed_pdf = await _signed_copy(client, investor, tranche["id"], name="NGUYEN VAN A")
    before = dt.datetime.now(dt.timezone.utc)

    resp = await _upload(client, investor, tranche["id"], signed_pdf)

    assert resp.status_code == 201, resp.text
    status = resp.json()
    assert status["required"] is False and status["version"] == VERSION
    assert status["file_name"] == "hop-dong-da-ky.pdf" and status["tranche_id"] == tranche["id"]
    [signature] = await _signatures(db_session, investor)
    assert signature.version == VERSION and signature.signed_at >= before
    assert str(signature.tranche_id) == tranche["id"]  # signed with the first commitment
    assert signature.file_size_bytes == len(signed_pdf)
    assert signature.party_b["name"] == "NGUYEN VAN A" and signature.party_b["id_number"] == "079203001234"
    evidence = (
        await db_session.execute(select(EvidenceFile).where(EvidenceFile.storage_key == signature.signed_file_key))
    ).scalar_one()
    assert evidence.purpose == "SIGNED_DOCUMENT" and evidence.sha256 == signature.document_sha256
    assert storage.objects[signature.signed_file_key][0] == signed_pdf
    # NEAC's verdict is kept with it, and shown.
    assert signature.signature_check["verified_by"] == "NEAC" and signature.signature_check["valid"] is True
    assert signature.signature_check["signer"]["cccd"] == "CCCD:079203001234"
    assert signature.signature_check["name_matches"] is True
    assert status["signature_check"]["signer_id"] == "CCCD:079203001234"
    assert status["signature_check"]["issuer_name"] == "FPT Certification Authority SHA256"
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "INVESTOR_AGREEMENT_SIGNED"))
    ).scalar_one()
    assert audit.after_state["reference_code"] == tranche["reference_code"]
    # The first commitment's Annex is kept with it, and its payment details open.
    signature_id = signature.id
    [confirmation] = await _confirmations(db_session)
    assert str(confirmation.tranche_id) == tranche["id"] and confirmation.agreement_id == signature_id
    assert confirmation.amount_vnd == 20_000_000
    assert (await _payment(client, investor, tranche["id"])).status_code == 200
    shown = await client.get(f"/funding/tranches/{tranche['id']}/investment-confirmation", headers=investor["headers"])
    assert shown.status_code == 200 and shown.json()["reference_code"] == tranche["reference_code"]
    # The investor can open exactly what they uploaded.
    signed = await client.get("/funding/me/investor-agreement/signed", headers=investor["headers"])
    assert signed.status_code == 200 and signed.content == signed_pdf


async def test_the_unsigned_agreement_sent_straight_back_is_refused(
    client, db_session, make_admin, make_user, make_eligible_investor, fake_neac
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    unsigned_download = (
        await client.get(
            "/funding/me/investor-agreement/document", params={"tranche_id": tranche_id}, headers=investor["headers"]
        )
    ).content

    for content in (UNSIGNED_PDF, unsigned_download):
        resp = await _upload(client, investor, tranche_id, content)
        assert resp.status_code == 422, resp.text
        assert resp.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_UNSIGNED"
    assert await _signatures(db_session, investor) == []
    assert (await _payment(client, investor, tranche_id)).status_code == 409
    assert fake_neac.calls == []  # refused before asking NEAC


async def test_neac_must_accept_the_signature(
    client, db_session, make_admin, make_user, make_eligible_investor, fake_neac
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

    for case, says in (
        ("INVALID", "Chữ ký: Không hợp lệ"),
        ("MODIFIED", "Tài liệu đã ký: Bị thay đổi"),
        ("REVOKED", "OCSP: Chứng thư số đã bị thu hồi."),
        ("UNSIGNED", "Văn bản không có chữ ký số"),
    ):
        resp = await _upload(client, investor, tranche_id, await _signed_copy(client, investor, tranche_id, f"FAKE-NEAC:{case}"))
        assert resp.status_code == 422, (case, resp.text)
        assert resp.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_SIGNATURE_INVALID", case
        assert says in resp.json()["detail"], (case, resp.json()["detail"])  # NEAC's own words
    assert len(fake_neac.calls) == 4
    assert await _signatures(db_session, investor) == []
    assert (await _payment(client, investor, tranche_id)).status_code == 409


async def test_the_valid_signature_must_be_the_investors(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

    someone_else = await _signed_copy(client, investor, tranche_id, signer_id="001099009999", name="TRAN VAN C")
    resp = await _upload(client, investor, tranche_id, someone_else)

    assert resp.status_code == 422 and resp.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_SIGNER_MISMATCH"
    assert "TRAN VAN C" in resp.json()["detail"] and "079203001234" in resp.json()["detail"]
    assert await _signatures(db_session, investor) == []


async def test_only_this_commitments_agreement_is_taken(
    client, db_session, make_admin, make_user, make_eligible_investor, fake_neac
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, first)).json()["id"]
    other_tranche = (await _reserve(client, investor, second)).json()["id"]

    # A signed PDF that is not our agreement, and our agreement for another commitment.
    for content in (SIGNED_AGREEMENT_PDF, await _signed_copy(client, investor, other_tranche)):
        resp = await _upload(client, investor, tranche_id, content)
        assert resp.status_code == 422 and resp.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_WRONG_DOCUMENT"
    assert fake_neac.calls == []  # refused before asking NEAC
    assert await _signatures(db_session, investor) == []


async def test_nothing_is_accepted_unchecked_when_neac_is_down(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

    resp = await _upload(client, investor, tranche_id, await _signed_copy(client, investor, tranche_id, "FAKE-NEAC:DOWN"))

    assert resp.status_code == 503 and resp.headers["X-Error-Code"] == "NEAC_UNAVAILABLE"
    assert await _signatures(db_session, investor) == []
    assert (await _payment(client, investor, tranche_id)).status_code == 409
    # Back up: the same upload goes through.
    assert (await _upload(client, investor, tranche_id)).status_code == 201


async def test_only_a_pdf_of_the_current_version_is_taken(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

    image = await _upload(client, investor, tranche_id, b"\x89PNG\r\n\x1a\n /ByteRange [0 1 2 3]", filename="anh.png",
                          content_type="image/png")
    assert image.status_code == 415 and image.headers["X-Error-Code"] == "UNSUPPORTED_TYPE"
    old = await _upload(client, investor, tranche_id, version="investor-agreement-v0.1")
    assert old.status_code == 409 and old.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_OUTDATED"
    too_big = await _upload(client, investor, tranche_id, SIGNED_AGREEMENT_PDF + b"0" * (10 * 1024 * 1024))
    assert too_big.status_code == 413 and too_big.headers["X-Error-Code"] == "FILE_TOO_LARGE"
    assert await _signatures(db_session, investor) == []


async def test_the_agreement_is_signed_with_the_investors_own_live_commitment(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    other = await _new_investor(make_user, make_eligible_investor)
    others_tranche = (await _reserve(client, other, window_id)).json()["id"]

    assert (await _upload(client, investor, others_tranche)).status_code == 404
    assert (await _upload(client, investor, uuid.uuid4())).status_code == 404

    mine = (await _reserve(client, investor, window_id)).json()["id"]
    row = await db_session.get(Tranche, uuid.UUID(mine))
    row.hold_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    await db_session.commit()
    lapsed = await _upload(client, investor, mine)
    assert lapsed.status_code == 409 and lapsed.headers["X-Error-Code"] == "HOLD_EXPIRED"
    assert await _signatures(db_session, investor) == []


async def test_the_agreement_is_signed_once_per_version(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    assert (await _upload(client, investor, tranche_id, filename="lan-1.pdf")).status_code == 201

    again = await _upload(client, investor, tranche_id, filename="lan-2.pdf")

    assert again.status_code == 409 and again.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_ALREADY_SIGNED"
    assert [s.file_name for s in await _signatures(db_session, investor)] == ["lan-1.pdf"]


async def test_an_investor_without_kyc_or_bank_account_cannot_upload(client, make_user):
    investor = await make_user(role="INVESTOR")

    resp = await _upload(client, investor, uuid.uuid4())

    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# later commitments
# --------------------------------------------------------------------------- #
async def test_a_later_commitment_keeps_its_annex_at_once_and_pays_straight_away(
    client, db_session, make_admin, make_user, make_eligible_investor, signed_documents, storage
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = await _open_project(client, db_session, make_admin, make_user, requested=60_000_000, rate=14.0, term=3)
    second_window, second_listing = second["window"].id, second["window"].listing_id
    project = await db_session.get(Project, second["project"])
    sme_name = project.legal_name
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    first_tranche = (await _reserve(client, investor, first)).json()["id"]
    assert (await _upload(client, investor, first_tranche)).status_code == 201
    [signature] = await _signatures(db_session, investor)
    signature_id, signed_at, signature_sha = signature.id, signature.signed_at, signature.document_sha256
    signed_documents.clear()

    resp = await _reserve(client, investor, second_window, 15_000_000)

    assert resp.status_code == 201, resp.text
    tranche = resp.json()
    assert (await _payment(client, investor, tranche["id"])).status_code == 200  # no agreement to sign again
    assert [s.id for s in await _signatures(db_session, investor)] == [signature_id]
    row = next(c for c in await _confirmations(db_session) if str(c.tranche_id) == tranche["id"])
    assert row.agreement_id == signature_id
    # The in-app signature covers it: the acceptance names the record and its PDF's hash.
    [acceptance] = signed_documents
    assert acceptance["investor_service_agreement"] == {
        "id": str(signature_id),
        "version": VERSION,
        "signed_at": signed_at.isoformat(),
        "document_sha256": signature_sha,
    }
    assert acceptance["investment_confirmation"]["id"] == str(row.id)
    assert acceptance["investment_confirmation"]["document_sha256"] == row.document_sha256
    unstable = {"expected_disbursement_date": None, "id": None, "document_sha256": None}
    assert acceptance["investment_confirmation"] | unstable == {
        "listing_id": str(second_listing),
        "sme_name": sme_name,
        "committed_amount_vnd": 15_000_000,
        "annual_rate_pct": "14.000",
        "term_months": 3,
        "expected_service_fee_vnd": 93_750,  # 15,000,000 × 2.5% × 3 / 12
        "service_fee_bps_per_year": 250,
        **unstable,
    }
    # Its filled-in PDF, as the transfer step shows it.
    pdf = await client.get(
        f"/funding/tranches/{tranche['id']}/investment-confirmation/document", headers=investor["headers"]
    )
    assert pdf.status_code == 200 and pdf.content == storage.objects[row.file_key][0]
    text_ = _pdf_text(pdf.content)
    for expected in (tranche["reference_code"], sme_name, "15.000.000 VND", "14%/năm, 3 tháng",
                     "P × 2,5% × M / 12 = 93.750 VND + VAT", "NGUYEN VAN A", VERSION):
        assert expected in text_, expected
    audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "INVESTMENT_RESERVED").order_by(AuditLog.created_at))
    ).scalars().all()
    assert [a.after_state["investor_agreement_id"] for a in audits] == [None, str(signature_id)]


async def test_replaying_a_commitment_keeps_one_investment_confirmation(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))  # agreement already signed
    key = str(uuid.uuid4())

    first = await _reserve(client, investor, window_id, key=key)
    again = await _reserve(client, investor, window_id, key=key)

    assert first.status_code == 201 and again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert len(await _confirmations(db_session)) == 1


async def test_a_new_version_is_signed_with_the_next_commitment(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    first_tranche = (await _reserve(client, investor, first)).json()["id"]
    assert (await _upload(client, investor, first_tranche)).status_code == 201
    # A new text: its version changes in the printed PDF and in what the server asks for.
    monkeypatch.setattr(investor_agreement, "VERSION", "investor-agreement-v0.4")
    monkeypatch.setattr(agreement_text, "VERSION", "investor-agreement-v0.4")

    status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert status["required"] is True and status["version"] == "investor-agreement-v0.4"
    assert status["signed_version"] == VERSION and status["signed_at"] is None
    tranche_id = (await _reserve(client, investor, second)).json()["id"]
    assert (await _payment(client, investor, tranche_id)).status_code == 409
    old_copy = await _upload(client, investor, tranche_id, version=VERSION)
    assert old_copy.status_code == 409 and old_copy.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_OUTDATED"

    assert (await _upload(client, investor, tranche_id, version="investor-agreement-v0.4")).status_code == 201
    assert (await _payment(client, investor, tranche_id)).status_code == 200
    versions = [s.version for s in await _signatures(db_session, investor)]
    assert versions == [VERSION, "investor-agreement-v0.4"]  # the old signature is kept as it was


# --------------------------------------------------------------------------- #
# what the page reads
# --------------------------------------------------------------------------- #
async def test_the_status_asks_for_the_agreement_until_signed_then_shows_what_was_signed(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _verified_investor(db_session, make_user, make_eligible_investor, name="TRAN THI B")

    before = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert before["version"] == VERSION and before["required"] is True
    assert before["signed_at"] is None and before["signed_version"] is None and before["tranche_id"] is None
    assert before["party_b"]["name"] == "TRAN THI B"  # as the PDF fills it in
    assert (await client.get("/funding/me/investor-agreement/signed", headers=investor["headers"])).status_code == 404

    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    assert (await _upload(client, investor, tranche_id)).status_code == 201
    # A later change to the profile does not rewrite what was signed.
    kyc = (
        await db_session.execute(
            select(GVerifyVerification).where(GVerifyVerification.user_id == uuid.UUID(investor["id"]))
        )
    ).scalar_one()
    kyc.full_name = "SOMEONE ELSE"
    await db_session.commit()

    after = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    [signature] = await _signatures(db_session, investor)
    assert after["required"] is False and after["signed_version"] == VERSION
    assert dt.datetime.fromisoformat(after["signed_at"]) == signature.signed_at
    assert after["party_b"]["name"] == "TRAN THI B"


async def test_an_organisation_investor_is_filled_from_its_business_verification(
    client, db_session, make_user, make_eligible_investor
):
    investor = await _new_investor(make_user, make_eligible_investor)
    user_id = uuid.UUID(investor["id"])
    account = (await db_session.execute(select(PaymentAccount).where(PaymentAccount.user_id == user_id))).scalar_one()
    account.holder_type, account.account_name = "ENTITY", "CONG TY DAU TU X"
    db_session.add(
        GVerifyKybVerification(
            user_id=user_id, status=STATUS_APPROVED, document_type="BUSINESS_REGISTRATION",
            business_name="CÔNG TY TNHH ĐẦU TƯ X", tax_code="0312345678", company_address="12 Lê Lợi, Quận 1, TP.HCM",
        )
    )
    await db_session.commit()

    party = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()["party_b"]
    body = _pdf_text(
        (await client.get("/funding/me/investor-agreement/document", headers=investor["headers"])).content
    )

    assert party["holder_type"] == "ENTITY"
    assert party["name"] == "CÔNG TY TNHH ĐẦU TƯ X"
    assert party["id_number"] == "0312345678"
    assert party["address"] == "12 Lê Lợi, Quận 1, TP.HCM"
    assert "0312345678" in body and "12 Lê Lợi" in body and "CONG TY DAU TU X" in body


async def test_an_organisation_signs_with_its_enterprise_certificate(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(make_user, make_eligible_investor)
    user_id = uuid.UUID(investor["id"])
    account = (await db_session.execute(select(PaymentAccount).where(PaymentAccount.user_id == user_id))).scalar_one()
    account.holder_type = "ENTITY"
    db_session.add(
        GVerifyKybVerification(
            user_id=user_id, status=STATUS_APPROVED, document_type="BUSINESS_REGISTRATION",
            business_name="CÔNG TY TNHH ĐẦU TƯ X", tax_code="0312345678",
        )
    )
    await db_session.commit()
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

    resp = await _upload(client, investor, tranche_id, await _signed_copy(client, investor, tranche_id, signer_id="0312345678"))

    assert resp.status_code == 201, resp.text  # the certificate's MST is the company's KYB enterprise code


async def test_the_investment_confirmation_preview_is_filled_in_from_the_window(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    window = setup["window"]

    resp = await client.get(
        f"/funding/windows/{window.id}/investment-confirmation",
        params={"amount_vnd": 10_000_000},
        headers=investor["headers"],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    project = await db_session.get(Project, setup["project"])
    assert body["listing_id"] == str(window.listing_id)
    assert body["listing_ref"] == str(window.listing_id).split("-")[0].upper()
    assert body["sme_name"] == project.legal_name
    assert body["amount_vnd"] == 10_000_000
    assert body["annual_rate_pct"] == "16.000" and body["term_months"] == 4
    assert body["service_fee_vnd"] == 83_333  # 10,000,000 × 2.5% × 4 / 12, floored (fees.md golden)
    assert body["service_fee_bps_per_year"] == 250
    # Investors pay as soon as they commit, within the 2-hour hold.
    today = ict_date(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2))
    assert dt.date.fromisoformat(body["expected_disbursement_date"]) == today


async def test_only_the_investor_who_committed_sees_its_documents(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    other = await make_eligible_investor(await make_user(role="INVESTOR"))
    tranche_id = (await _reserve(client, investor, setup["window"].id)).json()["id"]

    for path in (f"/funding/tranches/{tranche_id}/investment-confirmation",
                 f"/funding/tranches/{tranche_id}/investment-confirmation/document"):
        assert (await client.get(path, headers=other["headers"])).status_code == 404, path
        assert (await client.get(path, headers=setup["sme"]["headers"])).status_code == 403, path
    others_pdf = await client.get(
        "/funding/me/investor-agreement/document", params={"tranche_id": tranche_id}, headers=other["headers"]
    )
    assert others_pdf.status_code == 404
    for path in ("/funding/me/investor-agreement", "/funding/me/investor-agreement/document",
                 "/funding/me/investor-agreement/signed"):
        assert (await client.get(path, headers=setup["sme"]["headers"])).status_code == 403, path
    assert (await _upload(client, setup["sme"], tranche_id, SIGNED_AGREEMENT_PDF)).status_code == 403
