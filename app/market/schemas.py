import datetime as dt
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


class ProjectedScheduleOut(BaseModel):
    """What a confirmed ticket's repayment days would be if its round closed on `assumed_close_date` (its last
    day, the latest it can). Projected, not guaranteed; replaced by the real days once the loan forms."""

    assumed_close_date: dt.date
    start_date: dt.date
    business_day_count: int
    days: list[ProjectedDayOut]
    totals: ProjectedTotalsOut


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
