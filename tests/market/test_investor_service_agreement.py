"""Contract 4, the Investor Service Agreement, confirmed with the investor's first commitment: agreeing holds the
amount and gives it a code (no money moves); the investor reads the agreement filled in with their details and that
commitment's Annex, asks for a one-time code on their phone by Zalo and confirms with it; only then are the payment
details given. Every later commitment keeps its own filled-in Investment Confirmation (the Annex) when the investor
agrees. Before committing, an eligible investor can read the Loan Contract the SME signed for the listing.
Spec: docs/specs/market/investor-service-agreement.md
"""
import datetime as dt
import io
import json
import secrets
import uuid

import pytest
from pypdf import PdfReader
from sqlalchemy import select, update

from app.core.config import settings
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import EvidenceFile, PaymentAccount, Tranche
from app.direct_transfer.signing import MockSigner
from app.direct_transfer.storage import InMemoryStorage
from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification, GVerifyVerification
from app.lending.models import AuditLog, Contract, LoanApplicationDocument, Project
from app.market.funding import INVESTOR_TERMS_VERSION
from app.market.investor_agreement import service as investor_agreement
from app.market.investor_agreement import text as agreement_text
from app.market.investor_agreement.models import (
    AgreementSigningCode,
    InvestorServiceAgreement,
    SignedInvestmentConfirmation,
)
from app.phone_verification import zalo
from app.users.models import User
from tests.market.test_funding_flow import _open_project

VERSION = investor_agreement.VERSION


async def _reserve(client, investor, window_id, amount=10_000_000, *, key=None):
    body = {"amount_vnd": amount, "ack_risk_disclosure": True, "terms_version": INVESTOR_TERMS_VERSION}
    headers = investor["headers"] | {"Idempotency-Key": key or str(uuid.uuid4())}
    return await client.post(f"/funding/windows/{window_id}/reservations", json=body, headers=headers)


@pytest.fixture
def outbox(monkeypatch):
    """Every code handed to Zalo, in order."""
    sent: list[dict] = []

    async def _record(to, code, *, tracking_id):
        sent.append({"to": to, "code": code, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_otp", _record)
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "zalo")
    return sent


async def _send_code(client, investor, tranche_id, *, version=VERSION):
    return await client.post(
        "/funding/me/investor-agreement/code",
        json={"version": version, "tranche_id": str(tranche_id)},
        headers=investor["headers"],
    )


async def _sign(client, investor, tranche_id, code, *, version=VERSION):
    return await client.post(
        "/funding/me/investor-agreement",
        json={"version": version, "tranche_id": str(tranche_id), "code": code},
        headers=investor["headers"],
    )


async def _confirm(client, investor, tranche_id, outbox, *, version=VERSION):
    """Ask for the code and confirm with it: the whole signing step."""
    sent = await _send_code(client, investor, tranche_id, version=version)
    assert sent.status_code == 200, sent.text
    return await _sign(client, investor, tranche_id, outbox[-1]["code"], version=version)


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def _age_codes(db_session, by: dt.timedelta) -> None:
    """Move every code sent so far back in time, as if `by` had passed."""
    await db_session.execute(
        update(AgreementSigningCode).values(
            created_at=AgreementSigningCode.created_at - by, expires_at=AgreementSigningCode.expires_at - by
        )
    )
    await db_session.commit()


async def _codes(db_session, investor) -> list[AgreementSigningCode]:
    db_session.expire_all()
    return list(
        (
            await db_session.execute(
                select(AgreementSigningCode)
                .where(AgreementSigningCode.investor_id == uuid.UUID(investor["id"]))
                .order_by(AgreementSigningCode.created_at)
            )
        ).scalars()
    )


async def _payment(client, investor, tranche_id):
    return await client.get(f"/funding/tranches/{tranche_id}/payment", headers=investor["headers"])


async def _new_investor(db_session, make_user, make_eligible_investor, *, phone="auto"):
    """An eligible investor who has not confirmed the agreement yet, with a Vietnamese mobile on the account."""
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    if phone == "auto":
        phone = f"09{secrets.randbelow(10**8):08d}"
    user = await db_session.get(User, uuid.UUID(investor["id"]))
    user.phone = phone
    await db_session.commit()
    return investor | {"phone": phone}


async def _verified_investor(db_session, make_user, make_eligible_investor, *, name="NGUYEN VAN A",
                             id_number="079203001234", phone="auto"):
    """The same, whose approved eKYC carries a name and an ID number."""
    investor = await _new_investor(db_session, make_user, make_eligible_investor, phone=phone)
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
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
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
    assert "Party B confirmed this Agreement" not in _pdf_text(resp.content)  # not confirmed yet: no such line
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
        "P × 2,5% × M / 12 = 166.667 VND + VAT",  # 166,666.67 to the nearest dong
        ict_date(dt.datetime.fromisoformat(tranche["hold_expires_at"])).strftime("%d/%m/%Y"),
    ):
        assert expected in body, expected
    # Party A, the last article, the version on every page: the whole contract. It is confirmed by code.
    assert "1501167629" in body and "Điều 11." in body and body.count(VERSION) >= 2
    assert "one-time code (OTP) sent by Zalo" in " ".join(body.split())
    assert "digital signature" not in body

    download = await client.get(
        "/funding/me/investor-agreement/document", params={"tranche_id": tranche["id"], "download": "true"},
        headers=investor["headers"],
    )
    assert download.headers["content-disposition"].startswith("attachment;")
    # Without a commitment, the Annex is the blank template.
    blank = _pdf_text((await client.get("/funding/me/investor-agreement/document", headers=investor["headers"])).content)
    assert "Mã cam kết / Commitment" not in blank and "................ VND (≥ 10.000.000)" in blank


async def test_confirming_with_the_code_keeps_the_agreement_and_the_first_annex_and_opens_payment(
    client, db_session, make_admin, make_user, make_eligible_investor, storage, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _verified_investor(db_session, make_user, make_eligible_investor, phone="0703564489")
    tranche = (await _reserve(client, investor, window_id, 20_000_000)).json()
    before = dt.datetime.now(dt.timezone.utc)

    resp = await _confirm(client, investor, tranche["id"], outbox)

    assert resp.status_code == 201, resp.text
    status = resp.json()
    assert status["required"] is False and status["version"] == VERSION
    assert status["tranche_id"] == tranche["id"] and tranche["reference_code"] in status["file_name"]
    [signature] = await _signatures(db_session, investor)
    assert signature.version == VERSION and signature.signed_at >= before
    assert str(signature.tranche_id) == tranche["id"]  # confirmed with the first commitment
    assert signature.party_b["name"] == "NGUYEN VAN A" and signature.party_b["id_number"] == "079203001234"
    # The record: the agreement as read, plus the line saying when and how Party B confirmed, kept as evidence.
    evidence = (
        await db_session.execute(select(EvidenceFile).where(EvidenceFile.storage_key == signature.signed_file_key))
    ).scalar_one()
    assert evidence.purpose == "SIGNED_DOCUMENT" and evidence.sha256 == signature.document_sha256
    kept = storage.objects[signature.signed_file_key][0]
    assert signature.file_size_bytes == len(kept)
    body = " ".join(_pdf_text(kept).split())
    assert f"Mã cam kết / Commitment: {tranche['reference_code']}" in body and "NGUYEN VAN A" in body
    assert "Party B confirmed this Agreement at" in body and "phone number 0703***489" in body
    assert "0703564489" not in body  # the number is masked on the document
    # How it was confirmed is kept with it, and shown.
    check, signature_id = signature.signature_check, signature.id
    [code] = await _codes(db_session, investor)
    assert code.consumed_at is not None
    assert check["verified_by"] == "ZALO_OTP" and check["code_id"] == str(code.id)
    assert check["phone"] == "+84703564489" and check["channel"] == "zalo"
    assert status["signature_check"]["verified_by"] == "ZALO_OTP"
    assert status["signature_check"]["phone"] == "0703***489"
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "INVESTOR_AGREEMENT_SIGNED"))
    ).scalar_one()
    assert audit.after_state["reference_code"] == tranche["reference_code"]
    assert audit.after_state["signed_by"] == "ZALO_OTP" and audit.after_state["phone"] == "0703***489"
    # The first commitment's Annex is kept with it, and its payment details open.
    [confirmation] = await _confirmations(db_session)
    assert str(confirmation.tranche_id) == tranche["id"] and confirmation.agreement_id == signature_id
    assert confirmation.amount_vnd == 20_000_000
    assert (await _payment(client, investor, tranche["id"])).status_code == 200
    shown = await client.get(f"/funding/tranches/{tranche['id']}/investment-confirmation", headers=investor["headers"])
    assert shown.status_code == 200 and shown.json()["reference_code"] == tranche["reference_code"]
    # The investor can open exactly what was kept.
    signed = await client.get("/funding/me/investor-agreement/signed", headers=investor["headers"])
    assert signed.status_code == 200 and signed.content == kept


async def test_the_code_goes_by_zalo_to_the_accounts_phone(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor, phone="0912 345 678")
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert status["phone"] == "0912***678"  # where the code will go, before asking

    resp = await _send_code(client, investor, tranche_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"phone": "0912***678", "channel": "zalo", "expires_in": 300, "resend_in": 60}
    [sent] = outbox
    [row] = await _codes(db_session, investor)
    assert sent["to"] == "84912345678" and sent["tracking_id"] == row.id.hex
    assert len(sent["code"]) == 6 and sent["code"].isdigit()
    assert row.phone == "+84912345678" and str(row.tranche_id) == tranche_id and row.version == VERSION
    assert sent["code"] not in row.code_hash  # never the code itself
    assert await _signatures(db_session, investor) == []  # asking for a code confirms nothing


async def test_an_account_without_a_vietnamese_mobile_is_asked_for_one(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    for phone in (None, "+6591234567"):
        investor = await _new_investor(db_session, make_user, make_eligible_investor, phone=phone)
        tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

        status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
        resp = await _send_code(client, investor, tranche_id)

        assert status["phone"] is None
        assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "PHONE_REQUIRED", phone
    assert outbox == []


async def test_a_wrong_code_is_counted_and_the_fifth_kills_the_code(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    assert (await _send_code(client, investor, tranche_id)).status_code == 200
    code = outbox[-1]["code"]

    for attempt in range(1, 6):
        resp = await _sign(client, investor, tranche_id, _wrong(code))
        expected = "CODE_TOO_MANY_ATTEMPTS" if attempt == 5 else "CODE_INCORRECT"
        assert resp.status_code == 400 and resp.headers["X-Error-Code"] == expected, attempt
    [row] = await _codes(db_session, investor)
    assert row.attempts == 5 and row.consumed_at is None

    dead = await _sign(client, investor, tranche_id, code)  # the right code, too late
    assert dead.status_code == 400 and dead.headers["X-Error-Code"] == "CODE_EXPIRED"
    assert (await _sign(client, investor, tranche_id, "12345")).status_code == 422  # not six digits
    assert await _signatures(db_session, investor) == []
    assert (await _payment(client, investor, tranche_id)).status_code == 409


async def test_only_a_live_code_confirms(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

    none_yet = await _sign(client, investor, tranche_id, "123456")
    assert none_yet.status_code == 400 and none_yet.headers["X-Error-Code"] == "CODE_EXPIRED"

    assert (await _send_code(client, investor, tranche_id)).status_code == 200
    first = outbox[-1]["code"]
    await _age_codes(db_session, dt.timedelta(minutes=2))
    assert (await _send_code(client, investor, tranche_id)).status_code == 200
    second = outbox[-1]["code"]
    if first != second:  # only the latest code counts (and a miss on it is a wrong guess)
        superseded = await _sign(client, investor, tranche_id, first)
        assert superseded.status_code == 400 and superseded.headers["X-Error-Code"] == "CODE_INCORRECT"

    await _age_codes(db_session, dt.timedelta(minutes=6))  # past its five minutes
    expired = await _sign(client, investor, tranche_id, second)
    assert expired.status_code == 400 and expired.headers["X-Error-Code"] == "CODE_EXPIRED"
    assert await _signatures(db_session, investor) == []


async def test_a_code_confirms_only_the_commitment_it_was_sent_for(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, first)).json()["id"]
    other_tranche = (await _reserve(client, investor, second)).json()["id"]
    assert (await _send_code(client, investor, tranche_id)).status_code == 200
    code = outbox[-1]["code"]

    resp = await _sign(client, investor, other_tranche, code)

    assert resp.status_code == 400 and resp.headers["X-Error-Code"] == "CODE_EXPIRED"
    assert await _signatures(db_session, investor) == []
    assert (await _sign(client, investor, tranche_id, code)).status_code == 201  # still good for its own


async def test_codes_are_limited_to_one_a_minute_and_five_a_day(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    assert (await _send_code(client, investor, tranche_id)).status_code == 200

    too_soon = await _send_code(client, investor, tranche_id)
    assert too_soon.status_code == 429 and too_soon.headers["X-Error-Code"] == "CODE_COOLDOWN"
    assert 1 <= int(too_soon.headers["Retry-After"]) <= 60

    for _ in range(4):
        await _age_codes(db_session, dt.timedelta(minutes=2))
        assert (await _send_code(client, investor, tranche_id)).status_code == 200
    await _age_codes(db_session, dt.timedelta(minutes=2))
    sixth = await _send_code(client, investor, tranche_id)
    assert sixth.status_code == 429 and sixth.headers["X-Error-Code"] == "CODE_DAILY_LIMIT"
    assert len(outbox) == 5

    await _age_codes(db_session, dt.timedelta(days=1))  # a day on, the count starts over
    assert (await _send_code(client, investor, tranche_id)).status_code == 200


async def test_a_code_zalo_does_not_deliver_is_not_counted(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "zalo")

    for error, status_code, code in (
        (zalo.ZaloError("no zalo account", code=-118), 400, "PHONE_NO_ZALO"),
        (zalo.ZaloError("template not approved", code=-131), 502, "CODE_NOT_SENT"),
        (zalo.ZaloNotConfigured("ZALO_APP_ID not configured"), 503, "CODE_UNAVAILABLE"),
    ):
        async def _refused(to, otp, *, tracking_id, error=error):
            raise error

        monkeypatch.setattr(zalo, "send_otp", _refused)
        resp = await _send_code(client, investor, tranche_id)
        assert resp.status_code == status_code and resp.headers["X-Error-Code"] == code, code
    assert await _codes(db_session, investor) == []  # nothing counted, no cooldown started

    sent: list[str] = []

    async def _ok(to, otp, *, tracking_id):
        sent.append(otp)

    monkeypatch.setattr(zalo, "send_otp", _ok)
    assert (await _send_code(client, investor, tranche_id)).status_code == 200  # at once, no wait
    assert len(sent) == 1


async def test_the_log_channel_stands_in_for_zalo_only_in_debug(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "log")

    monkeypatch.setattr(settings, "DEBUG", False)
    refused = await _send_code(client, investor, tranche_id)
    assert refused.status_code == 503 and refused.headers["X-Error-Code"] == "CODE_UNAVAILABLE"

    monkeypatch.setattr(settings, "DEBUG", True)
    resp = await _send_code(client, investor, tranche_id)
    assert resp.status_code == 200 and resp.json()["channel"] == "log"


async def test_an_old_version_is_not_confirmed(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]

    for resp in (
        await _send_code(client, investor, tranche_id, version="investor-agreement-v0.1"),
        await _sign(client, investor, tranche_id, "123456", version="investor-agreement-v0.1"),
    ):
        assert resp.status_code == 409 and resp.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_OUTDATED"
    assert outbox == [] and await _signatures(db_session, investor) == []


async def test_the_agreement_is_confirmed_with_the_investors_own_live_commitment(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    other = await _new_investor(db_session, make_user, make_eligible_investor)
    others_tranche = (await _reserve(client, other, window_id)).json()["id"]

    for tranche_id in (others_tranche, uuid.uuid4()):
        assert (await _send_code(client, investor, tranche_id)).status_code == 404
        assert (await _sign(client, investor, tranche_id, "123456")).status_code == 404

    mine = (await _reserve(client, investor, window_id)).json()["id"]
    assert (await _send_code(client, investor, mine)).status_code == 200
    row = await db_session.get(Tranche, uuid.UUID(mine))
    row.hold_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    await db_session.commit()
    await _age_codes(db_session, dt.timedelta(minutes=2))
    for lapsed in (await _send_code(client, investor, mine), await _sign(client, investor, mine, outbox[-1]["code"])):
        assert lapsed.status_code == 409 and lapsed.headers["X-Error-Code"] == "HOLD_EXPIRED"
    assert await _signatures(db_session, investor) == []


async def test_the_agreement_is_confirmed_once_per_version(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    assert (await _confirm(client, investor, tranche_id, outbox)).status_code == 201
    await _age_codes(db_session, dt.timedelta(minutes=2))

    for again in (await _send_code(client, investor, tranche_id),
                  await _sign(client, investor, tranche_id, outbox[-1]["code"])):
        assert again.status_code == 409 and again.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_ALREADY_SIGNED"
    assert len(await _signatures(db_session, investor)) == 1 and len(outbox) == 1


async def test_an_investor_without_kyc_or_bank_account_cannot_confirm(client, make_user, outbox):
    investor = await make_user(role="INVESTOR")

    assert (await _send_code(client, investor, uuid.uuid4())).status_code == 403
    assert (await _sign(client, investor, uuid.uuid4(), "123456")).status_code == 403
    assert outbox == []


# --------------------------------------------------------------------------- #
# later commitments
# --------------------------------------------------------------------------- #
async def test_a_later_commitment_keeps_its_annex_at_once_and_pays_straight_away(
    client, db_session, make_admin, make_user, make_eligible_investor, signed_documents, storage, outbox
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = await _open_project(client, db_session, make_admin, make_user, requested=60_000_000, rate=14.0, term=3)
    second_window, second_listing = second["window"].id, second["window"].listing_id
    project = await db_session.get(Project, second["project"])
    sme_name = project.legal_name
    investor = await _verified_investor(db_session, make_user, make_eligible_investor)
    first_tranche = (await _reserve(client, investor, first)).json()["id"]
    assert (await _confirm(client, investor, first_tranche, outbox)).status_code == 201
    [signature] = await _signatures(db_session, investor)
    signature_id, signed_at, signature_sha = signature.id, signature.signed_at, signature.document_sha256
    signed_documents.clear()

    resp = await _reserve(client, investor, second_window, 15_000_000)

    assert resp.status_code == 201, resp.text
    tranche = resp.json()
    assert (await _payment(client, investor, tranche["id"])).status_code == 200  # no agreement to confirm again
    assert len(outbox) == 1  # and no second code
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
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch, outbox
):
    first = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    second = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
    first_tranche = (await _reserve(client, investor, first)).json()["id"]
    assert (await _confirm(client, investor, first_tranche, outbox)).status_code == 201
    await _age_codes(db_session, dt.timedelta(minutes=2))
    # A new text: its version changes in the printed PDF and in what the server asks for.
    monkeypatch.setattr(investor_agreement, "VERSION", "investor-agreement-v0.4")
    monkeypatch.setattr(agreement_text, "VERSION", "investor-agreement-v0.4")

    status = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert status["required"] is True and status["version"] == "investor-agreement-v0.4"
    assert status["signed_version"] == VERSION and status["signed_at"] is None
    tranche_id = (await _reserve(client, investor, second)).json()["id"]
    assert (await _payment(client, investor, tranche_id)).status_code == 409
    old_text = await _send_code(client, investor, tranche_id, version=VERSION)
    assert old_text.status_code == 409 and old_text.headers["X-Error-Code"] == "INVESTOR_AGREEMENT_OUTDATED"

    assert (await _confirm(client, investor, tranche_id, outbox, version="investor-agreement-v0.4")).status_code == 201
    assert (await _payment(client, investor, tranche_id)).status_code == 200
    versions = [s.version for s in await _signatures(db_session, investor)]
    assert versions == [VERSION, "investor-agreement-v0.4"]  # the old signature is kept as it was


# --------------------------------------------------------------------------- #
# what the page reads
# --------------------------------------------------------------------------- #
async def test_the_status_asks_for_the_agreement_until_signed_then_shows_what_was_signed(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    window_id = (await _open_project(client, db_session, make_admin, make_user))["window"].id
    investor = await _verified_investor(db_session, make_user, make_eligible_investor, name="TRAN THI B")

    before = (await client.get("/funding/me/investor-agreement", headers=investor["headers"])).json()
    assert before["version"] == VERSION and before["required"] is True
    assert before["signed_at"] is None and before["signed_version"] is None and before["tranche_id"] is None
    assert before["party_b"]["name"] == "TRAN THI B"  # as the PDF fills it in
    assert (await client.get("/funding/me/investor-agreement/signed", headers=investor["headers"])).status_code == 404

    tranche_id = (await _reserve(client, investor, window_id)).json()["id"]
    assert (await _confirm(client, investor, tranche_id, outbox)).status_code == 201
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
    investor = await _new_investor(db_session, make_user, make_eligible_investor)
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
    assert body["service_fee_vnd"] == 83_333  # 10,000,000 × 2.5% × 4 / 12, nearest dong (fees.md golden)
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
    assert (await _send_code(client, setup["sme"], tranche_id)).status_code == 403
    assert (await _sign(client, setup["sme"], tranche_id, "123456")).status_code == 403


# --------------------------------------------------------------------------- #
# the Loan Contract the SME signed, read before committing
# --------------------------------------------------------------------------- #
async def test_an_eligible_investor_reads_the_loan_contract_the_sme_signed(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window = setup["window"]
    contract = await db_session.get(Contract, window.contract_id)
    application_id = contract.application_id
    signed = (
        await db_session.execute(
            select(LoanApplicationDocument).where(
                LoanApplicationDocument.loan_application_id == application_id,
                LoanApplicationDocument.document_type == "sme_loan_contract",
            )
        )
    ).scalar_one()
    held = {signed.file_key: b"%PDF-1.7 the loan contract, signed by the SME and the Guarantor"}
    monkeypatch.setattr("app.loans.signing.get_bytes", lambda key, **kw: held[key])
    investor = await make_eligible_investor(await make_user(role="INVESTOR"), signed_agreement=False)
    path = f"/funding/windows/{window.id}/loan-contract"

    resp = await client.get(path, headers=investor["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.content == held[signed.file_key]  # exactly as the SME uploaded it
    assert resp.headers["content-type"] == "application/pdf" and resp.headers["cache-control"] == "no-store"
    assert resp.headers["content-disposition"].startswith("inline;")
    download = await client.get(path, params={"download": "true"}, headers=investor["headers"])
    assert download.headers["content-disposition"].startswith("attachment;")
    views = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "LOAN_CONTRACT_VIEWED_BY_INVESTOR"))
    ).scalars().all()
    assert len(views) == 2 and {str(v.actor_id) for v in views} == {investor["id"]}
    assert all(v.entity_id == application_id for v in views)

    # It names the Guarantor: only an eligible investor gets it, never an unverified account or an SME.
    unverified = await make_user(role="INVESTOR")
    assert (await client.get(path, headers=unverified["headers"])).status_code == 403
    assert (await client.get(path, headers=setup["sme"]["headers"])).status_code == 403
    assert (await client.get(f"/funding/windows/{uuid.uuid4()}/loan-contract", headers=investor["headers"])).status_code == 404


async def test_a_listing_without_a_signed_loan_contract_has_none_to_show(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window = setup["window"]
    contract = await db_session.get(Contract, window.contract_id)
    signed = (
        await db_session.execute(
            select(LoanApplicationDocument).where(
                LoanApplicationDocument.loan_application_id == contract.application_id,
                LoanApplicationDocument.document_type == "sme_loan_contract",
            )
        )
    ).scalar_one()
    await db_session.delete(signed)  # a listing published before the Loan Contract was signed
    await db_session.commit()
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))

    resp = await client.get(f"/funding/windows/{window.id}/loan-contract", headers=investor["headers"])

    assert resp.status_code == 404
