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


class InvestorTrancheOut(TrancheOut):
    """An investor's own ticket. Adds when they uploaded their transfer receipt (null until they do, so the page
    knows whether to ask for one) and, in their list, the project it is in."""

    receipt_submitted_at: dt.datetime | None = None
    project_id: UUID | None = None
    project_name: str | None = None


class SmeTicketOut(BaseModel):
    """One investor's ticket as the business sees it: the investor masked ("N. A***"), as everywhere the business
    looks (list_window_tranches)."""

    id: UUID
    amount_vnd: int
    status: str
    reference_code: str
    investor_display: str
    attested_at: dt.datetime | None = None
    confirm_deadline: dt.datetime | None = None
    review_cause: str | None = None


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
    status: Literal["DUE", "PRESUMED_PAID", "REPORTED_MISSED", "MATCHED"]


class RepaymentReceiptOut(BaseModel):
    """A receipt the business uploaded for a repayment to the investor, and the days it pays. ``status``:
    AWAITING_CONFIRMATION (the investor has not said it arrived), CONFIRMED (they did, or staff approved it before
    investors confirmed: the days are paid and settled) or REJECTED (staff denied it before investors confirmed:
    the days were due again). ``payee`` is the investor's own account the money should reach, ``payer`` the
    business's. ``net_vnd`` is what should have arrived. ``open_report``: the investor reported one of these days
    as never arriving; confirming closes that report as paid."""

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
