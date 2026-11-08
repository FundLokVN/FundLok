"""Contract 4, the Investor Service Agreement: downloaded as a PDF filled in with the investor's details, signed
with their digital signature and uploaded back before their first commitment; then an Investment Confirmation
(its Annex) with every commitment. Spec: docs/specs/market/investor-service-agreement.md
"""
import datetime as dt
import io
import json
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
from app.market.investor_agreement.models import InvestorServiceAgreement
from conftest import SIGNED_AGREEMENT_PDF
from tests.market.test_funding_flow import _open_project

VERSION = investor_agreement.VERSION
UNSIGNED_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


async def _reserve(client, investor, window_id, amount=10_000_000, *, key=None):
    body = {"amount_vnd": amount, "ack_risk_disclosure": True, "terms_version": INVESTOR_TERMS_VERSION}
    headers = investor["headers"] | {"Idempotency-Key": key or str(uuid.uuid4())}
    return await client.post(f"/funding/windows/{window_id}/reservations", json=body, headers=headers)


async def _upload(client, investor, content=SIGNED_AGREEMENT_PDF, *, version=VERSION, filename="hop-dong-da-ky.pdf",
                  content_type="application/pdf"):
    return await client.post(
        "/funding/me/investor-agreement",
        data={"version": version},
        files={"file": (filename, content, content_type)},
        headers=investor["headers"],
    )


async def _verified_investor(db_session, make_user, make_eligible_investor, *, name="NGUYEN VAN A", id_number="079203001234"):
    """An eligible investor whose approved eKYC carries a name and an ID number."""
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    kyc = (
        await db_session.execute(
            select(GVerifyVerification).where(GVerifyVerification.user_id == uuid.UUID(investor["id"]))
        )
    ).scalar_one()
    kyc.full_name, kyc.person_number = name, id_number
    await db_session.commit()
    return investor


async def _signatures(db_session, investor):
    db_session.expire_all()
    return (
        await db_session.execute(
            select(InvestorServiceAgreement)
            .where(InvestorServiceAgreement.investor_id == uuid.UUID(investor["id"]))
            .order_by(InvestorServiceAgreement.signed_at)
        )
    ).scalars().all()


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
# the PDF to sign
# --------------------------------------------------------------------------- #
async def test_the_agreement_pdf_is_filled_from_the_investors_verified_details(
    client, db_session, make_user, make_eligible_investor
):
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    account = (
        await db_session.execute(select(PaymentAccount).where(PaymentAccount.user_id == uuid.UUID(investor["id"])))
    ).scalar_one()

    resp = await client.get("/funding/me/investor-agreement/document", headers=investor["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("inline;")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content.startswith(b"%PDF")
    assert b"/ByteRange" not in resp.content  # FundLok's own download is never signed
    body = _pdf_text(resp.content)
    # Party B, from the eKYC and the approved bank account; the address GVerify does not give is a blank.
    for expected in ("NGUYEN VAN A", "079203001234", account.account_number, "TEST INVESTOR", "Vietcombank"):
        assert expected in body, expected
    assert "địa chỉ ................" in body
    # Party A, the last article and the Annex: the whole contract. The version and the investor on every page.
    assert "1501167629" in body and "Điều 11." in body and "Phụ lục – Xác nhận đầu tư" in body
    assert body.count(VERSION) >= 2
    assert investor_agreement.investor_ref(uuid.UUID(investor["id"])) in resp.headers["content-disposition"]

    download = await client.get(
        "/funding/me/investor-agreement/document", params={"download": "true"}, headers=investor["headers"]
    )
    assert download.headers["content-disposition"].startswith("attachment;")


async def test_an_organisation_investor_is_filled_from_its_business_verification(
    client, db_session, make_user, make_eligible_investor
):
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
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


# --------------------------------------------------------------------------- #
# the signed copy
# --------------------------------------------------------------------------- #
async def test_a_first_commitment_without_a_signed_agreement_is_refused(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)

    resp = await _reserve(client, investor, setup["window"].id)

    assert resp.status_code == 409, resp.text
    assert resp.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_REQUIRED"
    assert (await db_session.execute(select(Tranche))).scalars().all() == []  # nothing reserved


async def test_the_signed_agreement_is_recorded_with_its_version_and_signing_time(
    client, db_session, make_user, make_eligible_investor, storage
):
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    before = dt.datetime.now(dt.timezone.utc)

    resp = await _upload(client, investor)

    assert resp.status_code == 201, resp.text
    status = resp.json()
    assert status["required"] is False and status["version"] == VERSION
    assert status["file_name"] == "hop-dong-da-ky.pdf" and status["replaceable"] is True
    [signature] = await _signatures(db_session, investor)
    assert signature.version == VERSION
    assert signature.signed_at >= before
    assert signature.file_size_bytes == len(SIGNED_AGREEMENT_PDF)
    assert signature.party_b["name"] == "NGUYEN VAN A" and signature.party_b["id_number"] == "079203001234"
    # The signed file is kept as evidence, byte for byte, and its hash recorded.
    evidence = (
        await db_session.execute(select(EvidenceFile).where(EvidenceFile.storage_key == signature.signed_file_key))
    ).scalar_one()
    assert evidence.purpose == "SIGNED_DOCUMENT" and evidence.sha256 == signature.document_sha256
    assert storage.objects[signature.signed_file_key][0] == SIGNED_AGREEMENT_PDF
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "INVESTOR_AGREEMENT_SIGNED"))
    ).scalar_one()
    assert str(audit.entity_id) == investor["id"] and audit.after_state["version"] == VERSION

    # The investor can open exactly what they uploaded.
    signed = await client.get("/funding/me/investor-agreement/signed", headers=investor["headers"])
    assert signed.status_code == 200 and signed.content == SIGNED_AGREEMENT_PDF
    assert signed.headers["content-type"] == "application/pdf"


async def test_the_unsigned_agreement_sent_straight_back_is_refused(
    client, db_session, make_user, make_eligible_investor
):
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    unsigned_download = (
        await client.get("/funding/me/investor-agreement/document", headers=investor["headers"])
    ).content

    for content in (UNSIGNED_PDF, unsigned_download):
        resp = await _upload(client, investor, content)
        assert resp.status_code == 422, resp.text
        assert resp.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_UNSIGNED"
    assert await _signatures(db_session, investor) == []


async def test_only_a_pdf_of_the_current_version_is_taken(client, db_session, make_user, make_eligible_investor):
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)

    image = await _upload(client, investor, b"\x89PNG\r\n\x1a\n /ByteRange [0 1 2 3]", filename="anh.png",
                          content_type="image/png")
    assert image.status_code == 415 and image.headers["X-Error-Code"] == "UNSUPPORTED_TYPE"
    old = await _upload(client, investor, version="investor-agreement-v0.1")
    assert old.status_code == 409 and old.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_OUTDATED"
    too_big = await _upload(client, investor, SIGNED_AGREEMENT_PDF + b"0" * (10 * 1024 * 1024))
    assert too_big.status_code == 413 and too_big.headers["X-Error-Code"] == "FILE_TOO_LARGE"
    assert await _signatures(db_session, investor) == []


async def test_an_investor_without_kyc_or_bank_account_cannot_upload(client, make_user):
    investor = await make_user(role="INVESTOR")

    resp = await _upload(client, investor)

    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# commitments
# --------------------------------------------------------------------------- #
async def test_the_first_commitment_goes_through_once_the_signed_agreement_is_in(
    client, db_session, make_admin, make_user, make_eligible_investor, signed_documents
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    assert (await _upload(client, investor)).status_code == 201
    [signature] = await _signatures(db_session, investor)
    signed_documents.clear()  # opening the window signed the SME's standing offer

    resp = await _reserve(client, investor, window_id, 20_000_000)

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "SIGNED"
    # The acceptance carries this commitment's Annex and names the signed agreement it falls under.
    [acceptance] = signed_documents
    assert acceptance["investor_service_agreement"] == {
        "id": str(signature.id),
        "version": VERSION,
        "signed_at": signature.signed_at.isoformat(),
        "document_sha256": signature.document_sha256,
    }
    assert acceptance["investment_confirmation"]["committed_amount_vnd"] == 20_000_000
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "INVESTMENT_RESERVED"))).scalar_one()
    assert audit.after_state["investor_agreement_id"] == str(signature.id)
    assert audit.after_state["investor_agreement_version"] == VERSION
    # Committed under it: it can no longer be swapped for another file.
    status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert status["replaceable"] is False


async def test_a_later_commitment_signs_only_its_investment_confirmation(
    client, db_session, make_admin, make_user, make_eligible_investor, signed_documents
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = await _open_project(client, db_session, make_admin, make_user, requested=60_000_000, rate=14.0, term=3)
    second_window, second_listing = second["window"].id, second["window"].listing_id
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    assert (await _upload(client, investor)).status_code == 201
    assert (await _reserve(client, investor, first)).status_code == 201
    [signature] = await _signatures(db_session, investor)
    signed_documents.clear()

    resp = await _reserve(client, investor, second_window, 15_000_000)

    assert resp.status_code == 201, resp.text
    assert [s.id for s in await _signatures(db_session, investor)] == [signature.id]  # nothing new to sign
    [acceptance] = signed_documents
    assert acceptance["investor_service_agreement"]["id"] == str(signature.id)
    project = await db_session.get(Project, second["project"])
    assert acceptance["investment_confirmation"] | {"expected_disbursement_date": None} == {
        "listing_id": str(second_listing),
        "sme_name": project.legal_name,
        "committed_amount_vnd": 15_000_000,
        "annual_rate_pct": "14.000",
        "term_months": 3,
        "expected_service_fee_vnd": 93_750,  # 15,000,000 × 2.5% × 3 / 12
        "service_fee_bps_per_year": 250,
        "expected_disbursement_date": None,
    }


async def test_a_wrong_file_can_be_replaced_until_a_commitment_is_made_under_it(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    assert (await _upload(client, investor, filename="nham.pdf")).status_code == 201

    again = await _upload(client, investor, SIGNED_AGREEMENT_PDF + b"% re-signed\n", filename="dung.pdf")

    assert again.status_code == 201, again.text
    assert again.json()["file_name"] == "dung.pdf"
    assert [s.file_name for s in await _signatures(db_session, investor)] == ["nham.pdf", "dung.pdf"]  # both kept
    assert (await _reserve(client, investor, window_id)).status_code == 201

    late = await _upload(client, investor, filename="khac.pdf")
    assert late.status_code == 409 and late.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_IN_USE"


async def test_a_new_version_asks_every_investor_to_sign_again(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    assert (await _upload(client, investor)).status_code == 201
    assert (await _reserve(client, investor, first)).status_code == 201
    monkeypatch.setattr(investor_agreement, "VERSION", "investor-agreement-v0.4")

    status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert status["required"] is True and status["version"] == "investor-agreement-v0.4"
    assert status["signed_version"] == VERSION and status["signed_at"] is None
    blocked = await _reserve(client, investor, second)
    assert blocked.status_code == 409 and blocked.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_REQUIRED"
    old_copy = await _upload(client, investor, version=VERSION)
    assert old_copy.status_code == 409 and old_copy.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_OUTDATED"

    assert (await _upload(client, investor, version="investor-agreement-v0.4")).status_code == 201
    assert (await _reserve(client, investor, second)).status_code == 201
    versions = [s.version for s in await _signatures(db_session, investor)]
    assert versions == [VERSION, "investor-agreement-v0.4"]  # the old signature is kept as it was


# --------------------------------------------------------------------------- #
# what the page reads
# --------------------------------------------------------------------------- #
async def test_the_status_asks_for_the_agreement_until_signed_then_shows_what_was_signed(
    client, db_session, make_user, make_eligible_investor
):
    investor = await _verified_investor(db_session, make_user, make_eligible_investor, name="TRAN THI B")

    before = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert before["version"] == VERSION and before["required"] is True
    assert before["signed_at"] is None and before["signed_version"] is None and before["replaceable"] is False
    assert before["party_b"]["name"] == "TRAN THI B"  # as the PDF fills it in
    missing = await client.get("/funding/me/investor-agreement/signed", headers=investor["headers"])
    assert missing.status_code == 404

    assert (await _upload(client, investor)).status_code == 201
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


async def test_the_investment_confirmation_is_filled_in_from_the_window(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
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


async def test_only_an_investor_has_an_investor_agreement(client, db_session, make_admin, make_user):
    setup = await _open_project(client, db_session, make_admin, make_user)
    sme = setup["sme"]

    for path in ("/funding/me/investor-agreement", "/funding/me/investor-agreement/document",
                 "/funding/me/investor-agreement/signed"):
        assert (await client.get(path, headers=sme["headers"])).status_code == 403, path
    assert (await _upload(client, sme)).status_code == 403
    confirmation = await client.get(
        f"/funding/windows/{setup['window'].id}/investment-confirmation",
        params={"amount_vnd": 10_000_000},
        headers=sme["headers"],
    )
    assert confirmation.status_code == 403
