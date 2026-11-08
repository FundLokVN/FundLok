"""Contract 4, "Hợp đồng dịch vụ dành cho Nhà đầu tư / Investor Service Agreement".

Spec: docs/specs/market/investor-service-agreement.md

The investor signs the master agreement once, before their first commitment, the same way an SME signs its
service agreement: download the PDF filled in with their details, sign it with their digital signature, upload
the signed copy. Reserving refuses an investor without one for the current version (funding.reserve_and_accept);
every commitment, the first included, signs its own Investment Confirmation (the Annex), carried in the
commitment's signed acceptance. This module fills Party B, renders the PDF, checks and records the signed upload,
and builds the Annex.

Where Party B's blanks come from:
  name, ID number         an individual's approved eKYC (full name, ID card number); an organisation's approved
                          KYB (business name, enterprise code)
  address                 the organisation's KYB address; for an individual, the ID card's address when the
                          eKYC provider returned one (GVerify's OCR does not today, so it prints as a blank)
  registered bank account the investor's approved account at a listed bank, the one the SME repays into
Anything unknown is None and prints as a dotted blank, as in the template.

The signature check is a presence check (app/loans/agreement/signature.py): it catches the unsigned PDF sent
straight back, not who signed or which document it is. Those are for staff review, as for the SME agreement.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import fees
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, conflict
from app.direct_transfer.evidence import store_evidence_file
from app.direct_transfer.models import FundingWindow, PaymentAccount, Tranche
from app.direct_transfer.storage import EvidenceStorage
from app.lending.models import Project
from app.loans.agreement.signature import has_digital_signature, is_pdf
from app.market.investor_agreement import text
from app.market.investor_agreement.models import InvestorServiceAgreement
from app.market.investor_agreement.pdf import AgreementData, render
from app.users.models import User

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
async def build_pdf(db: AsyncSession, investor_id: UUID, *, now: dt.datetime) -> tuple[bytes, str]:
    """(pdf bytes, file name): the current version filled in with the investor's details. Generated each time,
    never stored: the signed upload is the record."""
    party = await current_party_b(db, investor_id)
    account = (
        f"{party.bank_account_number} ({party.bank_account_name})"
        if party.bank_account_number and party.bank_account_name
        else party.bank_account_number
    )
    ref = investor_ref(investor_id)
    data = AgreementData(
        investor_ref=ref,
        prepared_on=ict_date(now),
        name=party.name,
        id_number=party.id_number,
        address=party.address,
        bank_account=account,
        bank_name=party.bank_name,
    )
    return render(data), f"FundLok-hop-dong-dich-vu-nha-dau-tu-{ref}-{VERSION}.pdf"


# --------------------------------------------------------------------------- #
# the signed copy
# --------------------------------------------------------------------------- #
async def current_signature(db: AsyncSession, investor_id: UUID) -> InvestorServiceAgreement | None:
    """The investor's latest signed upload of the CURRENT version, or None: then they sign before committing."""
    return (
        await db.execute(
            select(InvestorServiceAgreement)
            .where(InvestorServiceAgreement.investor_id == investor_id, InvestorServiceAgreement.version == VERSION)
            .order_by(InvestorServiceAgreement.signed_at.desc())
            .limit(1)
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


async def used_by_a_commitment(db: AsyncSession, agreement: InvestorServiceAgreement) -> bool:
    """Whether the investor has committed since uploading it: its acceptance then rests on this copy."""
    return (
        await db.execute(
            select(Tranche.id)
            .where(Tranche.investor_id == agreement.investor_id, Tranche.reserved_at >= agreement.signed_at)
            .limit(1)
        )
    ).first() is not None


async def record_signed_upload(
    db: AsyncSession,
    *,
    investor_id: UUID,
    content: bytes,
    file_name: str | None,
    version: str,
    storage: EvidenceStorage,
    now: dt.datetime,
    ip_address: str | None = None,
    user_agent: str | None = None,
    params: DirectTransferParams | None = None,
) -> InvestorServiceAgreement:
    """Check and record the investor's signed copy of the current version.

    409 INVESTOR_AGREEMENT_OUTDATED when `version` (the one printed on the PDF they downloaded) is not the current
    one; 415 UNSUPPORTED_TYPE for anything but a PDF; 422 INVESTOR_AGREEMENT_UNSIGNED for a PDF with no digital
    signature in it; 409 INVESTOR_AGREEMENT_IN_USE when they already committed under their current copy.
    """
    if version != VERSION:
        raise conflict(
            "INVESTOR_AGREEMENT_OUTDATED",
            "The Investor Service Agreement has changed; download the new version and sign it",
        )
    if not is_pdf(content):
        raise DirectTransferError(415, "Upload the signed agreement as a PDF", "UNSUPPORTED_TYPE")
    if not has_digital_signature(content):
        raise DirectTransferError(
            422,
            "The agreement has no digital signature. Sign the PDF with your digital signature and upload the "
            "signed copy.",
            "INVESTOR_AGREEMENT_UNSIGNED",
        )
    existing = await current_signature(db, investor_id)
    if existing is not None and await used_by_a_commitment(db, existing):
        raise conflict(
            "INVESTOR_AGREEMENT_IN_USE",
            "Your signed agreement already covers an investment and cannot be replaced",
        )
    party = await current_party_b(db, investor_id)
    stored = await store_evidence_file(
        db, storage, owner_user_id=investor_id, purpose="SIGNED_DOCUMENT", content=content,
        content_type="application/pdf",
        filename=file_name or f"investor-service-agreement-{VERSION}-{investor_ref(investor_id)}.pdf",
        params=params,
    )
    row = InvestorServiceAgreement(
        investor_id=investor_id,
        version=VERSION,
        signed_at=now,
        party_b=party.as_dict(),
        signed_file_key=stored.storage_key,
        document_sha256=stored.sha256,
        file_name=(file_name or "")[:255] or None,
        file_size_bytes=len(content),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:500] or None,
    )
    db.add(row)
    await db.flush()
    return row


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
    # P × 2.5% × M / 12, floored, before VAT (fees.investor_fee_total, the figure the tranche is billed on).
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
