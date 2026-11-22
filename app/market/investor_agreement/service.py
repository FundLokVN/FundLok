"""Contract 4, "Hợp đồng dịch vụ dành cho Nhà đầu tư / Investor Service Agreement".

Spec: docs/specs/market/investor-service-agreement.md

The investor confirms the master agreement once, with their first commitment. Agreeing to the commitment holds the
amount and gives it a code (no money moves); they then read the agreement filled in with their details and that
commitment's Annex, ask for a one-time code on their phone by Zalo, and confirm with it. The code is their
signature: investors are individuals and rarely hold a digital signature. The payment details are refused until
the current version is confirmed (funding_router.read_payment). Every later commitment confirms only its own
Investment Confirmation (the Annex), kept when the investor agrees (record_confirmation). This module fills Party
B, renders the PDFs, sends and checks the code, records the confirmed agreement, and keeps each commitment's Annex.

Where Party B's blanks come from:
  name, ID number         an individual's approved eKYC (full name, ID card number); an organisation's approved
                          KYB (business name, enterprise code)
  address                 the organisation's KYB address; for an individual, the ID card's address when the
                          eKYC provider returned one (GVerify's OCR does not today, so it prints as a blank)
  registered bank account the investor's approved account at a listed bank, the one the SME repays into
Anything unknown is None and prints as a dotted blank, as in the template.

What makes the code a signature on this document: it is sent only to the phone number on the investor's account,
it is bound to the commitment and the agreement version it was asked for (a code for one cannot confirm another),
it is single use, short lived and dies after a few wrong guesses, and what was confirmed is kept: the PDF as the
investor saw it plus a line saying when they confirmed and the number the code went to, with its hash, the code's
record, the IP address and the browser.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import logging
import math
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import fees
from app.core.timezone import ICT
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict
from app.direct_transfer.evidence import store_evidence_file
from app.direct_transfer.models import FundingWindow, PaymentAccount, Tranche
from app.direct_transfer.storage import EvidenceStorage
from app.lending.models import Project
from app.core.config import settings
from app.market.investor_agreement import text
from app.market.investor_agreement.models import (
    AgreementSigningCode,
    InvestorServiceAgreement,
    SignedInvestmentConfirmation,
)
from app.market.investor_agreement.pdf import AgreementData, AnnexData, annex_rows_for, render, render_annex
from app.phone_verification import service as phone_codes
from app.phone_verification import zalo
from app.phone_verification.phone import canonical_vn_mobile, zalo_number
from app.users.models import User
from app.utils.locks import advisory_xact_lock

logger = logging.getLogger(__name__)

# Contract set v0.3 (5 Nov 2026), DRAFT pending IBL review. Printed on every page of the PDF.
VERSION = text.VERSION

# Keys an eKYC OCR result might carry the ID card's address under (place of residence first). None of them is in
# GVerify's response today; read verbatim if one appears.
_ADDRESS_KEYS = ("residence_address", "place_of_residence", "recent_location", "address", "permanent_address")


@dataclasses.dataclass(frozen=True)
class PartyB:
    holder_type: str | None  # INDIVIDUAL | ENTITY, from the bank account
    name: str | None
    id_number: str | None  # personal ID number, or enterprise code
    address: str | None
    bank_account_number: str | None
    bank_account_name: str | None
    bank_name: str | None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _clean(value) -> str | None:
    value = (str(value) if value is not None else "").strip()
    return value or None


def investor_ref(investor_id: UUID) -> str:
    """The investor's id's first block, upper-case: printed on the PDF so a signed copy says whose it is."""
    return str(investor_id).split("-")[0].upper()


async def payout_account(db: AsyncSession, investor_id: UUID) -> PaymentAccount | None:
    """Where the SME repays this investor: their most recently approved account at a listed bank."""
    return (
        await db.execute(
            select(PaymentAccount)
            .where(
                PaymentAccount.user_id == investor_id,
                PaymentAccount.holder_role == "INVESTOR",
                PaymentAccount.status == "APPROVED",
                PaymentAccount.bank_bin.is_not(None),
            )
            .order_by(PaymentAccount.approved_at.desc().nulls_last(), PaymentAccount.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def party_b(db: AsyncSession, investor_id: UUID, account: PaymentAccount | None) -> PartyB:
    """Party B, filled from the investor's verified profile and `account`."""
    from app.gverify import kyb_service
    from app.gverify.models import STATUS_APPROVED, GVerifyVerification

    user = await db.get(User, investor_id)
    holder_type = account.holder_type if account is not None else None
    name = id_number = address = None
    if holder_type == "ENTITY":
        kyb = await kyb_service.latest_for_user(db, investor_id)
        if kyb is not None and kyb.is_approved:
            name, id_number, address = _clean(kyb.business_name), _clean(kyb.tax_code), _clean(kyb.company_address)
    else:
        kyc = (
            await db.execute(
                select(GVerifyVerification)
                .where(
                    GVerifyVerification.user_id == investor_id,
                    GVerifyVerification.verification_type == "KYC",
                    GVerifyVerification.status == STATUS_APPROVED,
                )
                .order_by(GVerifyVerification.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if kyc is not None:
            name, id_number = _clean(kyc.full_name), _clean(kyc.person_number)
            ocr = kyc.ocr_data if isinstance(kyc.ocr_data, dict) else {}
            address = next((_clean(ocr.get(key)) for key in _ADDRESS_KEYS if _clean(ocr.get(key))), None)
    return PartyB(
        holder_type=holder_type,
        name=name or (_clean(user.full_name) if user else None),
        id_number=id_number,
        address=address,
        bank_account_number=_clean(account.account_number) if account else None,
        bank_account_name=_clean(account.account_name) if account else None,
        bank_name=_clean(account.bank_name) if account else None,
    )


async def current_party_b(db: AsyncSession, investor_id: UUID) -> PartyB:
    return await party_b(db, investor_id, await payout_account(db, investor_id))


# --------------------------------------------------------------------------- #
# the PDF to sign
# --------------------------------------------------------------------------- #
async def build_pdf(
    db: AsyncSession,
    investor_id: UUID,
    *,
    now: dt.datetime,
    tranche: Tranche | None = None,
    party: PartyB | None = None,
    confirmed_phone: str | None = None,
) -> tuple[bytes, str]:
    """(pdf bytes, file name): the current version filled in with the investor's details and, given the commitment
    it is confirmed with, that commitment's code and Annex. Generated each time for reading. With
    `confirmed_phone` it is the copy kept as the record: the same document, plus the line saying Party B confirmed
    at `now` with the code sent to that (masked) number."""
    party = party or await current_party_b(db, investor_id)
    account = (
        f"{party.bank_account_number} ({party.bank_account_name})"
        if party.bank_account_number and party.bank_account_name
        else party.bank_account_number
    )
    ref = investor_ref(investor_id)
    annex = None
    if tranche is not None:
        window = await db.get(FundingWindow, tranche.window_id)
        terms = await investment_confirmation(
            db, window=window, amount_vnd=tranche.amount_vnd, pay_by=tranche.hold_expires_at
        )
        annex = annex_rows_for(
            listing_ref=terms.listing_ref,
            sme_name=terms.sme_name,
            amount_vnd=terms.amount_vnd,
            annual_rate_pct=terms.annual_rate_pct,
            term_months=terms.term_months,
            service_fee_vnd=terms.service_fee_vnd,
            expected_disbursement_date=terms.expected_disbursement_date,
        )
    data = AgreementData(
        investor_ref=ref,
        prepared_on=ict_date(now),
        name=party.name,
        id_number=party.id_number,
        address=party.address,
        bank_account=account,
        bank_name=party.bank_name,
        commitment_ref=tranche.reference_code if tranche is not None else None,
        annex=annex,
        confirmed_at=now.astimezone(ICT) if confirmed_phone else None,
        confirmed_phone=confirmed_phone,
    )
    suffix = f"-{tranche.reference_code}" if tranche is not None else ""
    return render(data), f"FundLok-hop-dong-dich-vu-nha-dau-tu-{ref}{suffix}-{VERSION}.pdf"


# --------------------------------------------------------------------------- #
# the confirmed agreement
# --------------------------------------------------------------------------- #
async def current_signature(db: AsyncSession, investor_id: UUID) -> InvestorServiceAgreement | None:
    """The investor's signed copy of the CURRENT version, or None: then they sign it with their next commitment,
    and its payment details wait for it."""
    return (
        await db.execute(
            select(InvestorServiceAgreement).where(
                InvestorServiceAgreement.investor_id == investor_id, InvestorServiceAgreement.version == VERSION
            )
        )
    ).scalar_one_or_none()


async def latest_signature(db: AsyncSession, investor_id: UUID) -> InvestorServiceAgreement | None:
    """The investor's most recent signed upload of any version."""
    return (
        await db.execute(
            select(InvestorServiceAgreement)
            .where(InvestorServiceAgreement.investor_id == investor_id)
            .order_by(InvestorServiceAgreement.signed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# confirming with a one-time code
# --------------------------------------------------------------------------- #
CODE_TTL = dt.timedelta(minutes=5)
RESEND_COOLDOWN = dt.timedelta(seconds=60)
MAX_CODES_PER_DAY = 5
MAX_ATTEMPTS_PER_CODE = 5
SIGNED_BY = "ZALO_OTP"


def masked_phone(canonical: str) -> str:
    """+84703564489 -> 0703***489: enough for the investor to recognise their number, not to learn it."""
    national = "0" + canonical[3:]
    return f"{national[:4]}***{national[-3:]}"


async def signing_phone(db: AsyncSession, investor_id: UUID) -> str | None:
    """The number the code goes to: the account's phone, when it is a Vietnamese mobile (canonical +84...)."""
    user = await db.get(User, investor_id)
    return canonical_vn_mobile(user.phone) if user is not None and user.phone else None


def _hash_code(row: AgreementSigningCode, code: str) -> str:
    # Bound to the row and to what it signs: a code for one commitment or version proves nothing about another.
    message = f"investor-agreement:{row.id}:{row.tranche_id}:{row.version}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _too_many(code: str, detail: str, retry_after: dt.timedelta) -> DirectTransferError:
    exc = DirectTransferError(429, detail, code)
    exc.retry_after = max(1, math.ceil(retry_after.total_seconds()))  # the router's Retry-After header
    return exc


async def _check_can_sign(db: AsyncSession, investor_id: UUID, tranche: Tranche, version: str, now: dt.datetime):
    if version != VERSION:
        raise conflict(
            "INVESTOR_AGREEMENT_OUTDATED",
            "The Investor Service Agreement has changed; read the new version and confirm it",
        )
    if await current_signature(db, investor_id) is not None:
        raise conflict("INVESTOR_AGREEMENT_ALREADY_SIGNED", "Your agreement is already confirmed")
    if tranche.status not in ("RESERVED", "SIGNED") or now > tranche.hold_expires_at:
        raise conflict("HOLD_EXPIRED", "The commitment's hold has lapsed; commit again and confirm with it")


async def send_signing_code(
    db: AsyncSession, *, investor_id: UUID, tranche: Tranche, version: str, now: dt.datetime
) -> AgreementSigningCode:
    """Send the investor a fresh one-time code, by Zalo to the phone number on their account, to confirm the
    current agreement with the commitment `tranche` (theirs; the caller checks). The new row retires any earlier
    code. The caller commits.

    409 INVESTOR_AGREEMENT_OUTDATED, INVESTOR_AGREEMENT_ALREADY_SIGNED or HOLD_EXPIRED as for signing; 409
    PHONE_REQUIRED when the account has no Vietnamese mobile number; 429 CODE_COOLDOWN (one code a minute) or
    CODE_DAILY_LIMIT (five a day); 400 PHONE_NO_ZALO when the number has no Zalo account that can receive it; 502
    CODE_NOT_SENT when Zalo refuses or cannot be reached; 503 CODE_UNAVAILABLE when Zalo is not configured.
    """
    await _check_can_sign(db, investor_id, tranche, version, now)
    phone = await signing_phone(db, investor_id)
    if phone is None:
        raise conflict(
            "PHONE_REQUIRED",
            "Add a Vietnamese mobile number to your profile: the confirmation code is sent to it by Zalo",
        )

    await advisory_xact_lock(db, f"investor-agreement-code:{investor_id}")
    sends, first, last = (
        await db.execute(
            select(
                func.count(AgreementSigningCode.id),
                func.min(AgreementSigningCode.created_at),
                func.max(AgreementSigningCode.created_at),
            ).where(
                AgreementSigningCode.investor_id == investor_id,
                AgreementSigningCode.created_at >= now - dt.timedelta(days=1),
            )
        )
    ).one()
    if last is not None and now - last < RESEND_COOLDOWN:
        raise _too_many("CODE_COOLDOWN", "Please wait before requesting another code", RESEND_COOLDOWN - (now - last))
    if sends >= MAX_CODES_PER_DAY:
        raise _too_many(
            "CODE_DAILY_LIMIT", "Too many codes requested today. Try again later.", first + dt.timedelta(days=1) - now
        )

    code = phone_codes._new_code()
    try:
        channel = phone_codes._channel()  # zalo, or log (DEBUG only), as for phone verification
        row = AgreementSigningCode(
            id=uuid4(), investor_id=investor_id, tranche_id=tranche.id, version=VERSION, phone=phone,
            channel=channel, expires_at=now + CODE_TTL, created_at=now,
        )
        row.code_hash = _hash_code(row, code)
        db.add(row)
        await db.flush()  # before the send: a database error should not cost a Zalo message
        if channel == "zalo":
            await zalo.send_otp(zalo_number(phone), code, tracking_id=row.id.hex)
        else:
            logger.warning(
                "Investor agreement code for %s (commitment %s): %s (PHONE_OTP_CHANNEL=log)",
                phone, tranche.reference_code, code,
            )
    except zalo.ZaloNotConfigured as exc:
        await db.rollback()
        logger.error("Investor agreement code unavailable: %s", exc)
        raise DirectTransferError(503, "The confirmation code is not available right now", "CODE_UNAVAILABLE") from exc
    except zalo.ZaloError as exc:
        # Rolled back, so an undelivered code neither counts against the limits nor starts a cooldown.
        await db.rollback()
        if exc.code in phone_codes.NO_ZALO_ERRORS:
            raise DirectTransferError(
                400, "Your phone number has no Zalo account that can receive the code", "PHONE_NO_ZALO"
            ) from exc
        raise DirectTransferError(502, "Could not send the confirmation code. Please try again.", "CODE_NOT_SENT") from exc
    return row


async def sign_with_code(
    db: AsyncSession,
    *,
    investor_id: UUID,
    tranche: Tranche,
    version: str,
    code: str,
    storage: EvidenceStorage,
    now: dt.datetime,
    ip_address: str | None = None,
    user_agent: str | None = None,
    params: DirectTransferParams | None = None,
) -> InvestorServiceAgreement:
    """Confirm the current agreement with the commitment `tranche` (theirs; the caller checks) by redeeming the
    investor's latest one-time code, and keep that commitment's Annex. The payment details then open.

    The agreement is rendered once more, as the investor read it plus the line saying when they confirmed and the
    number the code went to, and that PDF is the record: stored as evidence with its hash. 409
    INVESTOR_AGREEMENT_OUTDATED, INVESTOR_AGREEMENT_ALREADY_SIGNED or HOLD_EXPIRED; 400 CODE_EXPIRED when there is
    no live code for this commitment and version (none, used, expired, exhausted, or sent for another commitment);
    400 CODE_INCORRECT, or CODE_TOO_MANY_ATTEMPTS on the fifth wrong guess (both counted: the error is marked
    `persist`).
    """
    await _check_can_sign(db, investor_id, tranche, version, now)
    await advisory_xact_lock(db, f"investor-agreement-code:{investor_id}")
    row = (
        await db.execute(
            select(AgreementSigningCode)
            .where(AgreementSigningCode.investor_id == investor_id)
            .order_by(AgreementSigningCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.consumed_at is not None
        or row.expires_at <= now
        or row.attempts >= MAX_ATTEMPTS_PER_CODE
        or row.tranche_id != tranche.id
        or row.version != VERSION
    ):
        raise bad_request("This code has expired. Request a new one.", "CODE_EXPIRED")
    if not hmac.compare_digest(row.code_hash, _hash_code(row, code)):
        row.attempts += 1
        exhausted = row.attempts >= MAX_ATTEMPTS_PER_CODE
        exc = bad_request(
            "Too many incorrect attempts. Request a new code." if exhausted else "Incorrect code",
            "CODE_TOO_MANY_ATTEMPTS" if exhausted else "CODE_INCORRECT",
        )
        exc.persist = True  # the wrong guess is counted even though the request fails
        raise exc
    row.consumed_at = now
    return await record_agreement(
        db, investor_id=investor_id, tranche=tranche, code_row=row, storage=storage, now=now,
        ip_address=ip_address, user_agent=user_agent, params=params,
    )


async def record_agreement(
    db: AsyncSession,
    *,
    investor_id: UUID,
    tranche: Tranche,
    code_row: AgreementSigningCode,
    storage: EvidenceStorage,
    now: dt.datetime,
    ip_address: str | None = None,
    user_agent: str | None = None,
    params: DirectTransferParams | None = None,
) -> InvestorServiceAgreement:
    """Keep the current agreement as confirmed by the already-redeemed `code_row`, with the commitment's Annex.
    Shared by `sign_with_code` and the Day-14 loan contract (app/market/day14_signing.py), where one Zalo code signs
    the loan contract and, on the investor's first Day 14, this agreement too (Money flow FINAL v1.0)."""
    row = code_row
    party = await current_party_b(db, investor_id)
    masked = masked_phone(row.phone)
    content, file_name = await build_pdf(
        db, investor_id, now=now, tranche=tranche, party=party, confirmed_phone=masked
    )
    stored = await store_evidence_file(
        db, storage, owner_user_id=investor_id, purpose="SIGNED_DOCUMENT", content=content,
        content_type="application/pdf", filename=file_name, params=params,
    )
    agreement = InvestorServiceAgreement(
        investor_id=investor_id,
        version=VERSION,
        signed_at=now,
        tranche_id=tranche.id,
        party_b=party.as_dict(),
        signed_file_key=stored.storage_key,
        document_sha256=stored.sha256,
        file_name=file_name,
        file_size_bytes=len(content),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:500] or None,
        signature_check={
            "verified_by": SIGNED_BY,
            "checked_at": now.isoformat(),
            "phone": row.phone,
            "phone_masked": masked,
            "channel": row.channel,
            "code_id": str(row.id),
            "code_sent_at": row.created_at.isoformat(),
        },
        signature_checked_at=now,
    )
    db.add(agreement)
    await db.flush()
    # The commitment's Annex, as filled in the agreement just confirmed.
    await record_confirmation(
        db, tranche=tranche, window=await db.get(FundingWindow, tranche.window_id), agreement=agreement,
        storage=storage, now=now, params=params,
    )
    return agreement


# --------------------------------------------------------------------------- #
# Annex: Investment Confirmation, signed with every commitment
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class InvestmentConfirmation:
    listing_id: UUID
    listing_ref: str  # the listing id's first block, upper-case, as printed
    sme_name: str
    amount_vnd: int
    annual_rate_pct: Decimal
    term_months: int
    # P × 2.5% × M / 12, to the nearest dong, before VAT (fees.investor_fee_total, the figure the tranche is billed on).
    service_fee_vnd: int
    service_fee_bps_per_year: int
    # Investors pay as soon as they commit, within the hold (funding-window.md rule 8): the hold's last day.
    expected_disbursement_date: dt.date

    def as_document(self) -> dict:
        return {
            "listing_id": str(self.listing_id),
            "sme_name": self.sme_name,
            "committed_amount_vnd": self.amount_vnd,
            "annual_rate_pct": str(self.annual_rate_pct),
            "term_months": self.term_months,
            "expected_service_fee_vnd": self.service_fee_vnd,
            "service_fee_bps_per_year": self.service_fee_bps_per_year,
            "expected_disbursement_date": self.expected_disbursement_date.isoformat(),
        }


async def investment_confirmation(
    db: AsyncSession,
    *,
    window: FundingWindow,
    amount_vnd: int,
    pay_by: dt.datetime,
    params: DirectTransferParams | None = None,
) -> InvestmentConfirmation:
    """The Annex for `amount_vnd` on `window`, the investor paying by `pay_by` (the tranche's hold expiry)."""
    p = resolve(params)
    project = await db.get(Project, window.project_id)
    return InvestmentConfirmation(
        listing_id=window.listing_id,
        listing_ref=str(window.listing_id).split("-")[0].upper(),
        sme_name=project.legal_name if project else "",
        amount_vnd=amount_vnd,
        annual_rate_pct=window.annual_rate_pct,
        term_months=window.term_months,
        service_fee_vnd=fees.investor_fee_total(amount_vnd, window.term_months, p),
        service_fee_bps_per_year=p.investor_fee_bps_per_year,
        expected_disbursement_date=ict_date(pay_by),
    )


async def confirmation_for(db: AsyncSession, tranche_id: UUID) -> SignedInvestmentConfirmation | None:
    return (
        await db.execute(select(SignedInvestmentConfirmation).where(SignedInvestmentConfirmation.tranche_id == tranche_id))
    ).scalar_one_or_none()


async def record_confirmation(
    db: AsyncSession,
    *,
    tranche: Tranche,
    window: FundingWindow,
    agreement: InvestorServiceAgreement,
    storage: EvidenceStorage,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> SignedInvestmentConfirmation:
    """Fill in and keep the commitment's Investment Confirmation, before the investor is shown where to pay.

    The filled-in PDF goes to evidence storage and a row records its values and hash; the commitment's signed
    acceptance then names both, so the in-app signature covers the Annex. Idempotent per tranche (a replayed
    reservation finds the one already made)."""
    existing = await confirmation_for(db, tranche.id)
    if existing is not None:
        return existing
    terms = await investment_confirmation(
        db, window=window, amount_vnd=tranche.amount_vnd, pay_by=tranche.hold_expires_at, params=params
    )
    # Party B as in the master agreement it is part of; the live profile when that copy has no name.
    party = dict(agreement.party_b or {})
    if not party.get("name"):
        party = (await current_party_b(db, tranche.investor_id)).as_dict()
    pdf = render_annex(
        AnnexData(
            commitment_ref=tranche.reference_code,
            investor_name=party.get("name"),
            investor_id_number=party.get("id_number"),
            agreement_version=agreement.version,
            agreement_signed_on=ict_date(agreement.signed_at),
            listing_ref=terms.listing_ref,
            sme_name=terms.sme_name,
            amount_vnd=terms.amount_vnd,
            annual_rate_pct=terms.annual_rate_pct,
            term_months=terms.term_months,
            service_fee_vnd=terms.service_fee_vnd,
            expected_disbursement_date=terms.expected_disbursement_date,
            confirmed_at=now.astimezone(ICT),
        )
    )
    stored = await store_evidence_file(
        db, storage, owner_user_id=tranche.investor_id, purpose="SIGNED_DOCUMENT", content=pdf,
        content_type="application/pdf", filename=f"xac-nhan-dau-tu-{tranche.reference_code}.pdf", params=params,
    )
    row = SignedInvestmentConfirmation(
        tranche_id=tranche.id,
        investor_id=tranche.investor_id,
        agreement_id=agreement.id,
        listing_id=terms.listing_id,
        sme_name=terms.sme_name,
        amount_vnd=terms.amount_vnd,
        annual_rate_pct=terms.annual_rate_pct,
        term_months=terms.term_months,
        service_fee_vnd=terms.service_fee_vnd,
        service_fee_bps_per_year=terms.service_fee_bps_per_year,
        expected_disbursement_date=terms.expected_disbursement_date,
        file_key=stored.storage_key,
        document_sha256=stored.sha256,
        confirmed_at=now,
    )
    db.add(row)
    await db.flush()
    return row


def confirmation_document(row: SignedInvestmentConfirmation) -> dict:
    """The Annex as the commitment's signed acceptance carries it: its values, its record and its PDF's hash."""
    return {
        "id": str(row.id),
        "listing_id": str(row.listing_id),
        "sme_name": row.sme_name,
        "committed_amount_vnd": row.amount_vnd,
        "annual_rate_pct": str(row.annual_rate_pct),
        "term_months": row.term_months,
        "expected_service_fee_vnd": row.service_fee_vnd,
        "service_fee_bps_per_year": row.service_fee_bps_per_year,
        "expected_disbursement_date": row.expected_disbursement_date.isoformat(),
        "document_sha256": row.document_sha256,
    }
