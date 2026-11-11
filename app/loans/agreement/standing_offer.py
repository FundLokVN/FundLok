"""The Standing Offer to Conclude Loan Contracts (Contract 3) for one approved request, filled in and rendered as the
PDF the SME and its Personal Guarantor sign.

Where each blank comes from:
  Offeror (SME)            as the Service Agreement's Party B (agreement/service.agreement_data): the approved KYB,
                           else the project's own name and tax id; the approved SME bank account
  Personal Guarantor       the signing SME user's approved eKYC: full name and ID number; the ID card's address when
                           the provider returned one, else the address the SME entered on the signing step
                           (loan_applications.guarantor_address: GVerify's OCR returns none today)
  Listing details          the approved request's terms (agreement/terms.locked_terms): target, rate, term; the
                           minimum ticket from the funding parameters; the application reference as the Listing ID
                           (one Listing per request); the template's own rules for the dates
  revenue share (s)        not computed by the platform yet, so a dotted blank
  assessment and estimate  the LOCKED score run's business score and lock date; principal, interest, total, daily
                           instalment and backstop from the repayment schedule's own rules (terms.repayment_estimate)
Anything unknown prints as a dotted blank, as in the template, and the Offer cannot be signed while a party's details
are missing (`missing_details`).

Two empty signature fields sit over the signature boxes, SME_Signature and Guarantor_Signature, and every page
carries a check code over everything the Offer is filled in with (pdf.check_code): app/loans/signing.py works it
out again when the signed copy comes back.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.config import resolve
from app.gverify.models import STATUS_APPROVED, GVerifyVerification
from app.lending.models import LoanApplication
from app.loans.agreement import standing_offer_text as text
from app.loans.agreement.pdf import (
    GUARANTOR_ADDRESS,
    GUARANTOR_ID,
    GUARANTOR_NAME,
    GUARANTOR_SIGNATURE_FIELD,
    SME_SIGNATURE_FIELD,
    AgreementData,
    ContractPDF,
    check_code,
    rate,
    signer_name,
)
from app.loans.agreement.service import agreement_data
from app.loans.agreement.terms import ListingTerms, locked_terms
from app.loans.agreement.text import BLANK
from app.users.models import User

# Contract 3's own figure: N0 = 21 × M working days.
WORKING_DAYS_PER_MONTH = 21

# Where an ID card's address may sit in the eKYC provider's OCR output.
_ADDRESS_KEYS = ("residence_address", "place_of_residence", "recent_location", "address", "permanent_address")


@dataclass(frozen=True)
class Guarantor:
    name: str | None
    id_number: str | None
    address: str | None
    # Where the address came from: "EKYC" (the ID card), "SME" (entered on the signing step) or None (none yet).
    address_source: str | None = None

    def missing_details(self) -> list[str]:
        values = {GUARANTOR_NAME: self.name, GUARANTOR_ID: self.id_number, GUARANTOR_ADDRESS: self.address}
        return [code for code, value in values.items() if not value]


@dataclass(frozen=True)
class StandingOfferData:
    sme: AgreementData
    guarantor: Guarantor
    terms: ListingTerms | None
    min_ticket_vnd: int

    def missing_details(self) -> list[str]:
        """The parties' details the Offer (and the Loan Contract) still prints as a blank."""
        return self.sme.missing_details() + self.guarantor.missing_details()

    @property
    def check_code(self) -> str:
        """Over everything the Offer is filled in with: the SME, the Guarantor, the terms and the assessment."""
        terms = self.terms
        estimate = terms.estimate if terms is not None else None
        sme = self.sme.check_values()
        sme.pop("document")
        return check_code(
            {
                "document": text.VERSION,
                "sme": sme,
                "guarantor": [self.guarantor.name, self.guarantor.id_number, self.guarantor.address],
                "terms": [terms.amount_vnd, str(terms.rate_pct), terms.term_months] if terms else None,
                "score": _score(terms.business_score) if terms else None,
                "assessed_on": _date(terms.assessed_at) if terms else None,
                "estimate": [estimate.interest_vnd, estimate.total_vnd, estimate.daily_vnd,
                             estimate.final_instalment_vnd, estimate.backstop_day] if estimate else None,
                "min_ticket_vnd": self.min_ticket_vnd,
            }
        )


def _clean(value) -> str | None:
    value = (str(value) if value is not None else "").strip()
    return value or None


def _entered(application: LoanApplication | None) -> str | None:
    return _clean(application.guarantor_address) if application is not None else None


async def guarantor_for(db: AsyncSession, user: User, application: LoanApplication | None = None) -> Guarantor:
    """The Personal Guarantor: the signing SME user, as their approved eKYC names them. The address is the ID card's
    when the eKYC has one, else the one the SME entered for `application` (none without an application)."""
    kyc = (
        await db.execute(
            select(GVerifyVerification)
            .where(
                GVerifyVerification.user_id == user.id,
                GVerifyVerification.verification_type == "KYC",
                GVerifyVerification.status == STATUS_APPROVED,
            )
            .order_by(GVerifyVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    entered = _entered(application)
    if kyc is None:
        return Guarantor(
            name=_clean(user.full_name), id_number=None, address=entered, address_source="SME" if entered else None
        )
    ocr = kyc.ocr_data if isinstance(kyc.ocr_data, dict) else {}
    on_card = next((_clean(ocr.get(key)) for key in _ADDRESS_KEYS if _clean(ocr.get(key))), None)
    return Guarantor(
        name=_clean(kyc.full_name) or _clean(user.full_name),
        id_number=_clean(kyc.person_number),
        address=on_card or entered,
        address_source="EKYC" if on_card else ("SME" if entered else None),
    )


async def offer_data(db: AsyncSession, application: LoanApplication, user: User) -> StandingOfferData:
    return StandingOfferData(
        sme=await agreement_data(db, application, user),
        guarantor=await guarantor_for(db, user, application),
        terms=await locked_terms(db, application),
        min_ticket_vnd=resolve(None).min_ticket_vnd,
    )


def _fill(template: str, fields: dict, bold: bool) -> str:
    values = {k: (_clean(v) or BLANK) for k, v in fields.items()}
    if bold:
        values = {k: (f"**{v}**" if v != BLANK else v) for k, v in values.items()}
    return template.format(**values)


def parties(data: StandingOfferData) -> tuple[tuple[str, str], tuple[str, str]]:
    """((offeror vi, en), (guarantor vi, en)), the values in bold in Vietnamese."""
    sme = {
        "name": data.sme.sme_name,
        "code": data.sme.enterprise_code,
        "address": data.sme.address,
        "representative": data.sme.legal_representative,
        "account": data.sme.bank_account,
        "bank": data.sme.bank_name,
    }
    guarantor = {
        "name": data.guarantor.name,
        "id_number": data.guarantor.id_number,
        "address": data.guarantor.address,
    }
    return (
        (_fill(text.OFFEROR_VI, sme, bold=True), _fill(text.OFFEROR_EN, sme, bold=False)),
        (_fill(text.GUARANTOR_VI, guarantor, bold=True), _fill(text.GUARANTOR_EN, guarantor, bold=False)),
    )


def _vnd_plain(amount: int) -> str:
    return f"{int(amount):,}".replace(",", ".") + " VND"


def _vnd_digits(amount: int) -> str:
    return f"{int(amount):,}".replace(",", ".")


def _score(score: float | None) -> str | None:
    """72.4 -> "72,40 / 100", as the dashboard shows it (two decimals)."""
    return None if score is None else f"{score:.2f}".replace(".", ",") + " / 100"


def _date(moment) -> str | None:
    return None if moment is None else ict_date(moment).strftime("%d/%m/%Y")


def assessment_rows(data: StandingOfferData) -> list[tuple[str, str]]:
    labels = text.ASSESSMENT_LABELS
    terms = data.terms
    if terms is None:
        return [(label, BLANK) for label in labels.values()]
    e = terms.estimate
    return [
        (labels["score"], _score(terms.business_score) or BLANK),
        (labels["assessed_on"], _date(terms.assessed_at) or BLANK),
        (labels["principal"], _vnd_plain(e.principal_vnd)),
        (labels["interest"], _vnd_plain(e.interest_vnd)),
        (labels["total"], _vnd_plain(e.total_vnd)),
        (
            labels["daily"],
            text.ASSESSMENT_DAILY.format(
                daily=_vnd_digits(e.daily_vnd), days=e.instalment_days, last=_vnd_digits(e.final_instalment_vnd)
            ),
        ),
        (labels["backstop"], text.ASSESSMENT_BACKSTOP.format(day=e.backstop_day)),
    ]


def listing_rows(data: StandingOfferData) -> list[tuple[str, str]]:
    labels = text.LISTING_LABELS
    terms = data.terms
    if terms is not None:
        rate_vi, rate_en = rate(terms.rate_pct)
        target = f"{_vnd_plain(terms.amount_vnd)} {text.TARGET_CAP}"
        rate_value = f"{rate_vi}{text.RATE_VI} / {rate_en}% {text.RATE_EN}"
        term = f"{terms.term_months} tháng / {terms.term_months} months"
        days = WORKING_DAYS_PER_MONTH * terms.term_months
        instalment_days = (
            f"{WORKING_DAYS_PER_MONTH} × {terms.term_months} = {days} ngày làm việc / working days"
        )
    else:
        target = f"{BLANK} VND {text.TARGET_CAP}"
        rate_value = f"{BLANK}{text.RATE_VI} / {text.RATE_EN}"
        term = f"{BLANK} tháng / months"
        instalment_days = f"{WORKING_DAYS_PER_MONTH} × M ngày làm việc / working days"
    return [
        (labels["listing_id"], data.sme.application_ref),
        (labels["target"], target),
        (labels["rate"], rate_value),
        (labels["term"], term),
        (labels["instalment_days"], instalment_days),
        (labels["window_opens"], text.WINDOW_OPENS),
        (labels["window_closes"], text.WINDOW_CLOSES),
        (labels["acceptance_deadline"], text.ACCEPTANCE_DEADLINE),
        (labels["disbursement_date"], text.DISBURSEMENT_DATE),
        (labels["min_ticket"], _vnd_plain(data.min_ticket_vnd)),
        (labels["revenue_share"], f"{BLANK}{text.REVENUE_SHARE_SUFFIX}"),
    ]


def render(data: StandingOfferData, prepared_on: date) -> bytes:
    ref = data.sme.application_ref
    code = data.check_code
    pdf = ContractPDF(f"{text.VERSION} · Mã hồ sơ / Application {ref} · Mã đối chiếu / Check {code}")
    pdf.set_title(f"{text.TITLE[0]} / {text.TITLE[1]}")
    pdf.set_author("FundLok")
    pdf.set_subject(f"{text.VERSION} – {data.sme.sme_name or ''}".strip(" –"))
    pdf.add_page()

    pdf.title_block(
        text.TITLE,
        number=text.NUMBER.format(ref=ref),
        prepared_on=prepared_on,
        meta=f"Văn bản số 3 / Document 3   ·   Mã hồ sơ / Application: {ref}   ·   "
        f"Phiên bản / Version: {text.VERSION}   ·   Mã đối chiếu / Check: {code}",
    )
    pdf.vi(text.PREVAILS[0])
    pdf.en(text.PREVAILS[1])
    pdf.gap()
    pdf.vi(text.SIGNING[0])
    pdf.en(text.SIGNING[1])
    pdf.gap()
    (offeror_vi, offeror_en), (guarantor_vi, guarantor_en) = parties(data)
    pdf.vi(offeror_vi, markdown=True)
    pdf.en(offeror_en)
    pdf.gap()
    pdf.vi(guarantor_vi, markdown=True)
    pdf.en(guarantor_en)

    pdf.gap(4)
    pdf.heading(text.LISTING_TITLE)
    pdf.gap(1.5)
    pdf.rows(listing_rows(data), col_widths=(70, 100))

    pdf.gap(4)
    pdf.heading(text.ASSESSMENT_TITLE)
    pdf.gap(1.5)
    pdf.rows(assessment_rows(data), col_widths=(70, 100))
    pdf.gap(1.5)
    pdf.vi(text.ASSESSMENT_NOTE[0], size=8.5)
    pdf.en(text.ASSESSMENT_NOTE[1], size=8)

    pdf.articles(text.ARTICLES)

    pdf.gap(4)
    pdf.heading(text.ANNEX_TITLE)
    pdf.gap(1)
    pdf.vi(text.ANNEX[0])
    pdf.en(text.ANNEX[1])

    pdf.signature_blocks(
        text.SIGNATURES,
        fields=(SME_SIGNATURE_FIELD, GUARANTOR_SIGNATURE_FIELD),
        names=(signer_name(data.sme.legal_representative, data.sme.sme_name), data.guarantor.name),
    )
    return pdf.output_bytes()


def file_name(data: StandingOfferData) -> str:
    name_part = (data.sme.enterprise_code or data.sme.application_ref).replace("/", "-")
    return f"FundLok-de-nghi-giao-ket-{name_part}-{text.VERSION}.pdf"


async def build_pdf(db: AsyncSession, application: LoanApplication, user: User) -> tuple[bytes, str]:
    """(pdf bytes, file name) of the application's Standing Offer, for its owner (the caller checks)."""
    data = await offer_data(db, application, user)
    return render(data, ict_date(datetime.now(timezone.utc))), file_name(data)
