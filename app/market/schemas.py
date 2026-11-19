import datetime as dt
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.direct_transfer.schemas import FundingWindowOut, PayeeOut, RepaymentRowOut, TrancheOut


class ListingCreate(BaseModel):
    contract_id: UUID
    # Same missing-bound class as OrderCreate.amount below: the service checks
    # `min_ticket <= 0` but never bounded target_amount, so a zero or negative
    # target produced a listing no order could ever fill (remaining <= 0).
    target_amount: int = Field(gt=0)
    min_ticket: int = Field(gt=0)


class ListingOut(BaseModel):
    id: UUID
    contract_id: UUID
    target_amount: int
    min_ticket: int | None
    status: str
    funded_amount: int

    model_config = {"from_attributes": True}


class OrderCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # gt=0 is load-bearing, not decoration. The service's lower bound is
    # `amount < min_ticket`, which is skipped when a listing has no min_ticket
    # (the column is nullable), and `amount > remaining` cannot catch a negative
    # — so without this a negative order would be accepted, decrementing the
    # listing's funded_amount and writing a FILLED order for negative money.
    amount: int = Field(gt=0)
    ack_risk_disclosure: bool = Field(default=False, alias="ackRiskDisclosure")


class OrderOut(BaseModel):
    id: UUID
    listing_id: UUID
    amount: int
    status: str

    model_config = {"from_attributes": True}

class ReservationCreate(BaseModel):
    """An investor's ticket on a funding window: the amount, and their agreement to the investing terms."""

    # Integer VND (CLAUDE.md). The bounds are the window's (minimum ticket, capacity left): reserve_ticket checks them.
    amount_vnd: int = Field(gt=0)
    # The risk disclosure and the investing terms are one agreement in the UI; both must be accepted.
    ack_risk_disclosure: bool = False
    # funding.INVESTOR_TERMS_VERSION of the text the investor read (409 TERMS_OUTDATED otherwise).
    terms_version: str = Field(min_length=1, max_length=40)
    # The investor's account the SME repays to; their latest approved one when omitted.
    payment_account_id: UUID | None = None


class PartyBOut(BaseModel):
    """Party B of the Investor Service Agreement, as filled in. A null prints as a dotted blank."""

    holder_type: str | None = None  # INDIVIDUAL | ENTITY
    name: str | None = None
    id_number: str | None = None  # personal ID number, or enterprise code
    address: str | None = None
    bank_account_number: str | None = None
    bank_account_name: str | None = None
    bank_name: str | None = None


class SignatureCheckOut(BaseModel):
    """How the agreement on file was confirmed: with a one-time code sent by Zalo ("ZALO_OTP": only `phone` is
    set), or, for agreements of v0.3, NEAC's verdict on an uploaded digitally signed copy ("NEAC")."""

    verified_by: str  # "ZALO_OTP" | "NEAC"
    checked_at: dt.datetime
    phone: str | None = None  # ZALO_OTP: the number the code went to, masked, e.g. "0703***489"
    signer_name: str | None = None  # the certificate's CN, e.g. "CÔNG TY CỔ PHẦN FUNDLOK"
    signer_id: str | None = None  # as on the certificate, e.g. "MST:1501167629" or "CCCD:079..."
    issuer_name: str | None = None  # the CA, e.g. "FPT Certification Authority SHA256"
    signed_time: str | None = None  # NEAC's wording, e.g. "Ngày 20 tháng 10 năm 2026, 17:16:11 [UTC+07:00]"
    name_matches: bool = False  # the certificate's name is the investor's verified name


class InvestorAgreementOut(BaseModel):
    """Whether the investor must still confirm the Investor Service Agreement with their next commitment."""

    # The version the platform asks for now, printed on the PDF to sign.
    version: str
    # True until the investor has confirmed `version` with a one-time code. While true, a commitment is still made
    # (code and all) but its payment details are refused: the agreement is confirmed with it first.
    required: bool
    # The number the confirmation code is sent to by Zalo, masked ("0703***489"): the account's phone. Null when
    # the account has no Vietnamese mobile number (the investor adds one to their profile first).
    phone: str | None = None
    # When the current version was confirmed; null while required.
    signed_at: dt.datetime | None = None
    # The investor's latest signed version, of any version (an older one while a new version is required).
    signed_version: str | None = None
    # The confirmed copy's file name.
    file_name: str | None = None
    # The commitment it was signed with (whose Annex it carries filled in).
    tranche_id: UUID | None = None
    # How it was confirmed (null for records made outside the flow).
    signature_check: SignatureCheckOut | None = None
    # As signed when signed; else as the PDF fills it in now, from the verified profile.
    party_b: PartyBOut


class InvestmentConfirmationOut(BaseModel):
    """The Annex of the Investor Service Agreement for one commitment, as it will be signed."""

    listing_id: UUID
    listing_ref: str
    sme_name: str
    amount_vnd: int
    annual_rate_pct: Decimal
    term_months: int
    # P × 2.5% × M / 12, before VAT.
    service_fee_vnd: int
    service_fee_bps_per_year: int
    expected_disbursement_date: dt.date


class SignedInvestmentConfirmationOut(BaseModel):
    """One commitment's Investment Confirmation as filled in and confirmed when the investor agreed to it."""

    id: UUID
    tranche_id: UUID
    # The commitment's reference code (its transfer memo too).
    reference_code: str
    listing_id: UUID
    listing_ref: str
    sme_name: str
    amount_vnd: int
    annual_rate_pct: Decimal
    term_months: int
    # P × 2.5% × M / 12, before VAT.
    service_fee_vnd: int
    service_fee_bps_per_year: int
    expected_disbursement_date: dt.date
    confirmed_at: dt.datetime
    # The Investor Service Agreement version it is part of.
    agreement_version: str


class InvestorTrancheOut(TrancheOut):
    """An investor's own ticket. Adds when they uploaded their transfer receipt (null until they do, so the page
    knows whether to ask for one) and, in their list, the project it is in and its loan term in months."""

    receipt_submitted_at: dt.datetime | None = None
    project_id: UUID | None = None
    project_name: str | None = None
    term_months: int | None = None
    # Day-14 transfer rule (day14-transfer-rule.md 4.4): from the latest receipt. PENDING_CHECK: a receipt is in but
    # its fields are not read yet. Null: no receipt, or the ticket never had a QR code.
    payment_timing: Literal["ON_TIME", "LATE", "PENDING_CHECK"] | None = None
    late_reason: Literal["EXPIRED_CODE", "NO_QR_CODE", "AFTER_CUTOFF"] | None = None
    # Confirmed automatically (the SME's 24 hours of silence): the SME may still dispute it until then (A5).
    dispute_until: dt.datetime | None = None


class TopUpPledgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    window_id: UUID
    listing_title: str | None = None
    amount_vnd: int
    recorded_at: dt.datetime
    status: Literal["AWAITING_UNLOCK", "AWAITING_SIGNING", "SIGNED", "SME_CONFIRMED", "VOID"]


class LenderStandingOut(BaseModel):
    """The lender's lock after a Day-14 no-show or late money (commit-then-accept-flow.md 4.7)."""

    model_config = ConfigDict(from_attributes=True)

    status: Literal["GOOD", "LOCKED", "CLOSED"]
    no_show_count: int
    locked_at: dt.datetime | None = None
    unlock_requires: list[Literal["EKYC", "ACKNOWLEDGEMENT"]]
    acknowledgement_sentence: str | None = None
    top_up_pledges: list[TopUpPledgeOut]


class UnlockStandingIn(BaseModel):
    acknowledgement: str = Field(min_length=1, max_length=500)


class SmeTicketOut(BaseModel):
    """One investor's ticket as the business sees it: the investor masked ("N. A***"), as everywhere the business
    looks (list_window_tranches)."""

    id: UUID
    amount_vnd: int
    status: str
    reference_code: str
    investor_display: str
    receipt_submitted_at: dt.datetime | None = None
    attested_at: dt.datetime | None = None
    confirm_deadline: dt.datetime | None = None
    review_cause: str | None = None
    # Confirmed automatically (silence for 24 hours): a dispute with a bank statement stays open until then (A5).
    dispute_until: dt.datetime | None = None


class SmeRoundOut(FundingWindowOut):
    """The business's own funding round: its progress and every ticket on it."""

    project_id: UUID
    project_name: str
    tickets: list[SmeTicketOut]


class MarkDaysPaidCreate(BaseModel):
    repayment_day_ids: list[UUID] = Field(min_length=1, max_length=500)
    receipt_file_id: UUID | None = None


class ReportMissingCreate(BaseModel):
    """The investor says a repayment day never reached their account."""

    statement: str | None = Field(default=None, max_length=2000)


class ProjectedDayOut(BaseModel):
    kind: str
    day_index: int
    due_date: dt.date
    principal_vnd: int
    interest_vnd: int
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int


class ProjectedTotalsOut(BaseModel):
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int


class ProjectionBasisOut(BaseModel):
    """How a ticket's days are worked out, step by step. The investors earn `investor_return_pct` over the whole
    loan, which as a yearly rate is `investor_rate_pct` (never above the business's `loan_rate_pct`): `interest_vnd`
    on `principal_vnd`. Repaid over `term_months` x `working_days_per_month` working days, `daily_vnd` a day
    (`total_vnd` / days, the last day `final_vnd` with the leftover), from the first working day after
    `approved_on`. The first-period interest is `stub_days` working days from `received_on` at the same rate over
    `stub_basis_days`, paid on day one. An individual investor's tax is `withholding_pct` of the interest only."""

    principal_vnd: int
    loan_rate_pct: Decimal
    investor_return_pct: Decimal | None
    investor_rate_pct: Decimal
    term_months: int
    working_days_per_month: int
    business_day_count: int
    interest_vnd: int
    total_vnd: int
    daily_vnd: int
    final_vnd: int
    received_on: dt.date
    approved_on: dt.date
    start_date: dt.date
    end_date: dt.date
    stub_days: int
    stub_basis_days: int
    stub_interest_vnd: int
    individual: bool
    withholding_pct: Decimal
    tax_vnd: int


class ProjectedScheduleOut(BaseModel):
    """What a confirmed ticket's repayment days would be if its round closed on `assumed_close_date` (its last
    day, the latest it can). Projected, not guaranteed; replaced by the real days once the loan forms."""

    assumed_close_date: dt.date
    start_date: dt.date
    business_day_count: int
    days: list[ProjectedDayOut]
    totals: ProjectedTotalsOut
    basis: ProjectionBasisOut | None = None


class SmeRepaymentDayOut(BaseModel):
    """One day the business pays: the sum over its investors. ``gross_vnd`` is what it owes in all (``net_vnd`` to
    the investors, ``tax_withheld_vnd`` held back for the tax office). ``paid`` and ``missing`` count the investors'
    tickets marked paid and reported missing (real days only)."""

    due_date: dt.date
    principal_vnd: int
    interest_vnd: int
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int
    tickets: int
    paid: int = 0
    missing: int = 0


class SmeRepaymentPlanOut(BaseModel):
    """The business's daily repayments to its investors. PROJECTED before the loan forms (if the round closed on
    ``assumed_close_date``, the latest it can; not a promise), ACTUAL once it has."""

    kind: str  # PROJECTED | ACTUAL
    assumed_close_date: dt.date | None = None
    start_date: dt.date | None = None
    days: list[SmeRepaymentDayOut]
    totals: ProjectedTotalsOut


class RepaymentRejectionOut(BaseModel):
    """FundLok staff denied the receipt the business uploaded for some of these days, and why."""

    reason: str
    decided_at: dt.datetime


class InvestmentDueOut(BaseModel):
    """What the business owes one investor on one investment now: every day due today or earlier that it has not
    marked paid, as one transfer to the investor's account (one VietQR for the whole amount, under the
    investment's memo). Each amount is tax included (gross) and as transferred once the tax is kept back (net)."""

    date: dt.date
    tranche_id: UUID
    reference_code: str
    investor_display: str
    days: list[RepaymentRowOut]
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int
    payee: PayeeOut | None
    vietqr_payload: str | None
    # Set when a day due here is due again because staff denied its receipt.
    rejection: RepaymentRejectionOut | None = None


class RepaymentReceiptAccountOut(BaseModel):
    """A bank account a repayment moved between, as the investor checks their statement against it."""

    bank_name: str
    account_number: str
    account_name: str


class RepaymentReceiptDayOut(BaseModel):
    """One repayment day a business's receipt pays the investor for."""

    repayment_day_id: UUID
    kind: Literal["INSTALMENT", "STUB"]
    due_date: dt.date
    principal_vnd: int
    interest_vnd: int
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int
    status: Literal["DUE", "LATE", "PRESUMED_PAID", "REPORTED_MISSED", "MATCHED"]


class RepaymentSystemCheckOut(BaseModel):
    """What the system read off the business's receipt, against this repayment (receipt-ocr.md section 10). Advice
    for the investor, never a confirmation: only their own account shows the money arrived. ``result``: MATCHES
    (the amount, the memo, the investor's account, bank and name, and the business as payer all match),
    DOES_NOT_MATCH (``failure_codes`` say what: AMOUNT_MISMATCH, REFERENCE_MISSING, PAYEE_MISMATCH,
    PAYEE_BANK_MISMATCH, PAYEE_NAME_MISMATCH, PAYER_NAME_MISMATCH, TIME_OUT_OF_RANGE, DUPLICATE_BANK_TXN) or UNSURE
    (it could not be read, or the receiver or payer confirmed, well enough)."""

    result: Literal["MATCHES", "DOES_NOT_MATCH", "UNSURE"]
    failure_codes: list[str] = []
    checked_at: dt.datetime


class RepaymentReceiptOut(BaseModel):
    """A receipt the business uploaded for a repayment to the investor, and the days it pays. ``status``:
    AWAITING_CONFIRMATION (the investor has not said it arrived), CONFIRMED (they did, or staff approved it before
    investors confirmed: the days are paid and settled) or REJECTED (staff denied it before investors confirmed:
    the days were due again). ``payee`` is the investor's own account the money should reach, ``payer`` the
    business's. ``net_vnd`` is what should have arrived. ``open_report``: the investor reported one of these days
    as never arriving; confirming closes that report as paid. ``system_check``: what the system read off the receipt,
    null when it was not read (OCR off)."""

    id: UUID
    tranche_id: UUID
    reference_code: str
    project_name: str
    submitted_at: dt.datetime
    status: Literal["AWAITING_CONFIRMATION", "CONFIRMED", "REJECTED"]
    confirmed_at: dt.datetime | None = None
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int
    days: list[RepaymentReceiptDayOut]
    payee: RepaymentReceiptAccountOut | None = None
    payer: RepaymentReceiptAccountOut | None = None
    original_filename: str | None = None
    content_type: str | None = None
    open_report: bool = False
    system_check: RepaymentSystemCheckOut | None = None


class TransactionOut(BaseModel):
    """One movement of the caller's money, signed from their side: out is negative. The investor: INVESTMENT (out)
    and RETURN (in: what reached them, after the tax the business keeps back). The business: DISBURSEMENT (an
    investment in), REPAYMENT (out: what it transferred an investor) and FEE (a FundLok invoice). ``status``:
    COMPLETED (received, matched by FundLok, or paid), PENDING (being checked, the business's word that it paid, or
    an invoice unpaid), FAILED (voided, or the investor reported the repayment never arrived). ``method_key`` names
    how it moved, for the page's copy."""

    id: str
    tranche_id: UUID | None = None
    reference: str
    type: Literal["INVESTMENT", "RETURN", "DISBURSEMENT", "REPAYMENT", "FEE"]
    status: Literal["COMPLETED", "PENDING", "FAILED"]
    created_at: dt.datetime
    amount: int
    currency: Literal["VND"] = "VND"
    counterparty: str
    method_key: str


class InvestorMonthlyPointOut(BaseModel):
    """One month of the portfolio: capital invested to date, and what came back (capital returned plus earnings
    after tax; ``principal_monthly + yield_monthly == returns_monthly``)."""

    month: str
    deployed_cumulative: int
    returns_cumulative: int
    returns_monthly: int
    principal_monthly: int
    yield_monthly: int


class IndustryAllocationOut(BaseModel):
    industry: str
    deployed: int


class CapitalStatusOut(BaseModel):
    """Where the invested capital stands: received but not repaying yet (active), out and repaying (repaying),
    returned (completed), or written down."""

    statusKey: Literal["active", "repaying", "completed", "writtenDown"]
    amount: int


class InvestorAnalyticsOut(BaseModel):
    monthly: list[InvestorMonthlyPointOut]
    allocation: list[IndustryAllocationOut]
    capital_status: list[CapitalStatusOut]
    active_positions: int


class SmeFacilityOut(BaseModel):
    """The business's loan: the sum of its investments, each repaying on its own. ``total_obligation`` is every
    repayment day tax included (the investors' tax is kept back by the business but owed all the same);
    ``interest_rate_pct`` is the investors' yearly rate (FundLok's fee is invoiced separately). ``target_daily`` is
    the next unpaid day; the period is this month's days. ``score`` is the business score the round was offered on."""

    principal: int
    total_obligation: int
    interest_rate_pct: float
    term_months: int
    disbursed_at: dt.date
    days_remaining: int
    target_daily: int
    current_period_total: int
    current_period_end_date: dt.date
    on_time_streak: int
    score: float | None = None


class SmeRepaymentMonthOut(BaseModel):
    """One month of repayment, tax included. ``revenue_share``: that against the average monthly revenue the loan
    was sized on (0 when there is none)."""

    month: str
    repaid: int
    repaid_cumulative: int
    revenue_share: float


class ScoreFactorOut(BaseModel):
    key: Literal["bcq", "rsg", "sector", "behavioral"]
    score: float
    weight: float


class BusinessAnalyticsOut(BaseModel):
    facility: SmeFacilityOut | None
    months: list[SmeRepaymentMonthOut]
    score_factors: list[ScoreFactorOut]
    # The average monthly revenue (last 12 months filed) the revenue share is measured against; null without one.
    revenue_basis_vnd: int | None


class AgreementCodeRequest(BaseModel):
    """Ask for the one-time code that confirms the agreement with a commitment."""

    version: str = Field(min_length=1, max_length=40)  # as GET /funding/me/investor-agreement gave it
    tranche_id: UUID


class AgreementCodeOut(BaseModel):
    phone: str  # masked, e.g. "0703***489"
    channel: str  # zalo | log
    expires_in: int  # seconds the code is good for
    resend_in: int  # seconds until another code may be requested


class AgreementSignRequest(AgreementCodeRequest):
    code: str = Field(pattern=r"^[0-9]{6}$")
