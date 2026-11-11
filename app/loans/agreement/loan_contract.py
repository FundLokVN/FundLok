"""The Loan Contract template (Contract 5) for one approved request, filled in with what is known before any
Investor commits and rendered as the PDF the SME and its Personal Guarantor sign, with the Service Agreement and
the Standing Offer (app/loans/signing.py). Each Investor's own contract is generated from it at commitment (later).

Signed after the Standing Offer, which it cites by number and date (`offer_signed_on`): until the Offer is on file
there is no date to print, and the template is not offered (409 SIGNING_OFFER_FIRST, app/loans/signing.py).

Where each blank comes from:
  Borrower (SME), Guarantor    as the Standing Offer (standing_offer.offer_data)
  Standing Offer no. and date  the Offer's number (standing_offer_text.NUMBER) and the day its signed copy was
                               accepted (ICT)
  rate, term, N0, backstop     the approved request's terms and the repayment schedule's rules
                               (terms.repayment_estimate: N0 = 21 × M, N_bs)
  per Investor                 the contract no., the Lender, P and everything worked out from it (I, T, A), the
                               Disbursement Date and the Investor's signing time are each Investor's own, filled in
                               their copy at commitment: this copy states the rule each follows
                               (loan_contract_text.PER_INVESTOR), with I, T and A worked out at the minimum ticket
  revenue share (s)            not computed by the platform yet, so a dotted blank

Two empty signature fields sit over the signature boxes, SME_Signature and Guarantor_Signature, and every page
carries a check code over everything it is filled in with (pdf.check_code).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.calendar_utils import ict_date
from app.lending.models import LoanApplication, LoanApplicationDocument
from app.loans.agreement import loan_contract_text as text
from app.loans.agreement import standing_offer_text
from app.loans.agreement.neac_check import STANDING_OFFER_TYPE
from app.loans.agreement.pdf import (
    GUARANTOR_SIGNATURE_FIELD,
    SME_SIGNATURE_FIELD,
    ContractPDF,
    check_code,
    rate,
    signer_name,
)
from app.loans.agreement.standing_offer import StandingOfferData, _clean, _vnd_digits, offer_data
from app.loans.agreement.terms import repayment_estimate
from app.loans.agreement.text import BLANK
from app.users.models import User


async def offer_signed_on(db: AsyncSession, application: LoanApplication) -> date | None:
    """The day (ICT) the signed Standing Offer was accepted, the date the Loan Contract cites; None before."""
    uploaded_at = (
        await db.execute(
            select(LoanApplicationDocument.uploaded_at).where(
                LoanApplicationDocument.loan_application_id == application.id,
                LoanApplicationDocument.document_type == STANDING_OFFER_TYPE,
                LoanApplicationDocument.status == "UPLOADED",
            )
        )
    ).scalar_one_or_none()
    return ict_date(uploaded_at) if uploaded_at is not None else None


def _offer(data: StandingOfferData, signed_on: date | None) -> dict[str, str]:
    return {
        "offer_no": standing_offer_text.NUMBER.format(ref=data.sme.application_ref),
        "offer_date": signed_on.strftime("%d/%m/%Y") if signed_on else BLANK,
    }


def _fill(template: str, fields: dict, bold: bool) -> str:
    values = {k: (_clean(v) or BLANK) for k, v in fields.items()}
    if bold:
        values = {k: (f"**{v}**" if v != BLANK else v) for k, v in values.items()}
    return template.format(**values)


def contract_check_code(data: StandingOfferData, signed_on: date | None) -> str:
    """Over everything the template is filled in with: the SME, the Guarantor, the approved terms (the worked
    example and the cap follow from them and the minimum ticket) and the Offer's date."""
    terms = data.terms
    estimate = terms.estimate if terms is not None else None
    sme = data.sme.check_values()
    sme.pop("document")
    return check_code(
        {
            "document": text.VERSION,
            "sme": sme,
            "guarantor": [data.guarantor.name, data.guarantor.id_number, data.guarantor.address],
            "terms": [str(terms.rate_pct), terms.term_months] if terms else None,
            "schedule": [estimate.instalment_days, estimate.backstop_day] if estimate else None,
            "target": terms.amount_vnd if terms else None,
            "min_ticket_vnd": data.min_ticket_vnd,
            "offer_signed_on": signed_on.isoformat() if signed_on else None,
        }
    )


def parties(data: StandingOfferData) -> list[tuple[str, str]]:
    """(vi, en) of Article 1: the Lender (each Investor, named in their own copy), the Borrower, the Guarantor."""
    offer_no = standing_offer_text.NUMBER.format(ref=data.sme.application_ref)
    borrower = {
        "name": data.sme.sme_name,
        "code": data.sme.enterprise_code,
        "representative": data.sme.legal_representative,
        "account": data.sme.bank_account,
        "bank": data.sme.bank_name,
    }
    guarantor = {
        "name": data.guarantor.name,
        "id_number": data.guarantor.id_number,
        "address": data.guarantor.address,
    }
    return [
        (
            text.PER_INVESTOR["lender_vi"].format(offer_no=offer_no),
            text.PER_INVESTOR["lender_en"].format(offer_no=offer_no),
        ),
        (_fill(text.BORROWER_VI, borrower, bold=True), _fill(text.BORROWER_EN, borrower, bold=False)),
        (_fill(text.GUARANTOR_VI, guarantor, bold=True), _fill(text.GUARANTOR_EN, guarantor, bold=False)),
    ]


def terms_rows(data: StandingOfferData) -> list[tuple[str, str]]:
    """Article 4. P and what follows from it are each Investor's: the rule, and the figures at the minimum ticket."""
    labels = text.TERMS_LABELS
    terms = data.terms
    estimate = terms.estimate if terms is not None else None
    rules = text.PER_INVESTOR
    if terms is not None:
        rate_vi, rate_en = rate(terms.rate_pct)
        rate_value = f"{rate_vi}{text.RATE_VI} / {rate_en}% {text.RATE_EN}"
        term = text.TERM.format(months=terms.term_months)
        days = text.INSTALMENT_DAYS.format(days=estimate.instalment_days)
        backstop = str(estimate.backstop_day)
        example = repayment_estimate(data.min_ticket_vnd, terms.rate_pct, terms.term_months)
        at_min = {
            "min": _vnd_digits(data.min_ticket_vnd),
            "target": _vnd_digits(terms.amount_vnd),
            "rate": rate_vi,
            "months": terms.term_months,
            "days": example.instalment_days,
            "interest": _vnd_digits(example.interest_vnd),
            "total": _vnd_digits(example.total_vnd),
            "daily": _vnd_digits(example.daily_vnd),
            "last": _vnd_digits(example.final_instalment_vnd),
        }
        principal, interest, total, daily = (
            rules[key].format(**at_min) for key in ("principal", "interest", "total", "daily")
        )
    else:
        rate_value = f"{BLANK}{text.RATE_VI} / {text.RATE_EN}"
        term = text.TERM.format(months=BLANK)
        days = text.INSTALMENT_DAYS.format(days=BLANK)
        backstop = BLANK
        principal = interest = total = f"{BLANK}{text.VND_SUFFIX}"
        daily = text.DAILY.format(daily=BLANK, last=BLANK)
    return [
        (labels["principal"], principal),
        (labels["rate"], rate_value),
        (labels["term"], term),
        (labels["interest"], interest),
        (labels["total"], total),
        (labels["instalment_days"], days),
        (labels["daily"], daily),
        (labels["disbursement_date"], standing_offer_text.DISBURSEMENT_DATE),
        (labels["cut_off"], text.CUT_OFF),
        (labels["first_instalment"], text.FIRST_INSTALMENT),
        (labels["backstop"], backstop),
        (labels["revenue_share"], text.REVENUE_SHARE.format(share=BLANK)),
        (labels["pit"], text.PIT),
    ]


def render(data: StandingOfferData, prepared_on: date, offer_signed: date | None) -> bytes:
    ref = data.sme.application_ref
    code = contract_check_code(data, offer_signed)
    offer = _offer(data, offer_signed)
    pdf = ContractPDF(f"{text.VERSION} · Mã hồ sơ / Application {ref} · Mã đối chiếu / Check {code}")
    pdf.set_title(f"{text.TITLE[0]} / {text.TITLE[1]}")
    pdf.set_author("FundLok")
    pdf.set_subject(f"{text.VERSION} – {data.sme.sme_name or ''}".strip(" –"))
    pdf.add_page()

    pdf.title_block(
        text.TITLE,
        number=text.NUMBER.format(ref=ref),
        prepared_on=prepared_on,
        meta=f"Văn bản số 5 / Document 5   ·   Mã hồ sơ / Application: {ref}   ·   "
        f"Phiên bản / Version: {text.VERSION}   ·   Mã đối chiếu / Check: {code}",
    )
    pdf.vi(text.PREVAILS[0])
    pdf.en(text.PREVAILS[1])
    pdf.gap()
    pdf.vi(text.SIGNING[0])
    pdf.en(text.SIGNING[1])

    pdf.gap(4)
    pdf.heading(text.ARTICLE_1)
    pdf.gap(1)
    for p_vi, p_en in parties(data):
        pdf.vi(p_vi, markdown=True)
        pdf.en(p_en)
        pdf.gap(1.5)

    heading, a2_vi, a2_en = text.ARTICLE_2
    pdf.articles([(heading, [(a2_vi.format(**offer), a2_en.format(**offer))]), text.ARTICLE_3])

    pdf.gap(4)
    pdf.heading(text.ARTICLE_4)
    pdf.gap(1.5)
    pdf.rows(terms_rows(data), col_widths=(75, 95))

    pdf.articles(text.ARTICLES)

    pdf.gap(4)
    pdf.heading(text.ANNEX[0])
    pdf.gap(1)
    pdf.vi(text.ANNEX[1])
    pdf.en(text.ANNEX[2])

    pdf.gap(4)
    pdf.heading(text.SIGNATURES_TITLE)
    pdf.gap(1)
    lines = {
        "investor_time_vi": text.PER_INVESTOR["investor_time_vi"],
        "investor_time_en": text.PER_INVESTOR["investor_time_en"],
        **offer,
    }
    for line in text.SIGNATURE_LINES:
        pdf.vi(line.format(**lines), size=9.5)
        pdf.gap(0.8)

    pdf.signature_blocks(
        text.SIGNATURE_BOXES,
        fields=(SME_SIGNATURE_FIELD, GUARANTOR_SIGNATURE_FIELD),
        names=(signer_name(data.sme.legal_representative, data.sme.sme_name), data.guarantor.name),
    )
    return pdf.output_bytes()


def file_name(data: StandingOfferData) -> str:
    name_part = (data.sme.enterprise_code or data.sme.application_ref).replace("/", "-")
    return f"FundLok-hop-dong-cho-vay-mau-{name_part}-{text.VERSION}.pdf"


async def build_pdf(db: AsyncSession, application: LoanApplication, user: User) -> tuple[bytes, str]:
    """(pdf bytes, file name) of the application's Loan Contract template, for its owner (the caller checks, and
    that the Offer is signed)."""
    data = await offer_data(db, application, user)
    signed_on = await offer_signed_on(db, application)
    return render(data, ict_date(datetime.now(timezone.utc)), signed_on), file_name(data)
