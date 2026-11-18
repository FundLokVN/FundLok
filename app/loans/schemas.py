from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.projects import engine_constraints


class LoanApplicationCreate(BaseModel):
    business_id: UUID
    requested_amount: int
    purpose: str | None = None
    repayment_preference: str | None = None

    @field_validator("requested_amount")
    @classmethod
    def _amount_within_engine_bounds(cls, value: int) -> int:
        return engine_constraints.validate_loan_size(value)


class LoanApplicationOut(BaseModel):
    id: UUID
    project_id: UUID
    requested_amount: int
    purpose: str | None
    repayment_preference: str | None
    status: str

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Lite grading figures
#
# The applicant types these instead of uploading the VAT and annual-report
# bundles. See the LoanApplication.self_reported_figures comment for why they
# are stored as JSONB; this model is the contract that guards that column.
#
# Every bound here is duplicated in the browser
# (Frontendfundlok/app/dashboard/_components/loan-application/lite-grading-fields.ts)
# so the applicant gets an instant error, but the browser copy is a convenience
# only -- a client can post anything, so these figures are validated again here
# before they reach underwriting.
# --------------------------------------------------------------------------- #

# A ceiling far above any plausible SME turnover. Its job is to catch a
# mistyped extra digit -- without it, a slipped keystroke grades a corner shop
# as a conglomerate, and the applicant never sees why their band was wrong.
VND_MAX = 1_000_000_000_000  # 1 trillion đồng

# VND amounts are whole đồng: the currency has no sub-unit, so a fractional
# figure is always an input error rather than precision.
VndAmount = Field(gt=0, le=VND_MAX)
OptionalVndAmount = Field(default=None, gt=0, le=VND_MAX)
OptionalPercent = Field(default=None, ge=0, le=100)


class LoanApplicationFiguresIn(BaseModel):
    """What the wizard saves from the SME's files on the revenue and costs step.

    `None` on an optional field means "left blank", which is deliberately NOT
    the same as 0: 0 is a real answer the engine scores (a company with no
    customer concentration), and collapsing the two would silently invent data.
    """

    # --- Required: the engine cannot produce a band without these -----------
    revenue_last_12m: int = VndAmount
    revenue_prior_12m: int = VndAmount
    cogs_y1: int = VndAmount
    fixed_cost_y1: int = VndAmount
    # Zero is a real answer, not a blank: a TT133 statement has no selling
    # line at all, so its variable cost is 0, and the statements say so. The
    # engine sums it into total cost, so 0 is safe there. The other two cost
    # lines stay strictly positive.
    variable_cost_excl_cogs_y1: int = Field(ge=0, le=VND_MAX)

    # --- Optional: they sharpen the band rather than enable it --------------
    revenue_best_month: int | None = OptionalVndAmount
    revenue_worst_month: int | None = OptionalVndAmount
    owner_withdrawal_pct: float | None = OptionalPercent
    conc_top1_pct: float | None = OptionalPercent
    conc_top3_pct: float | None = OptionalPercent

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_internally_consistent(self) -> "LoanApplicationFiguresIn":
        """Reject figure sets that cannot describe a real company.

        Each of these passes field-level validation while still being
        impossible, and every one of them is a plausible typo rather than a
        hypothetical. Catching them here means the applicant is told which
        number to fix, instead of underwriting scoring a contradiction.
        """
        best = self.revenue_best_month
        worst = self.revenue_worst_month

        if best is not None and worst is not None and worst > best:
            raise ValueError(
                "revenue_worst_month cannot exceed revenue_best_month"
            )

        # A single month cannot out-earn the year that contains it.
        for name, value in (
            ("revenue_best_month", best),
            ("revenue_worst_month", worst),
        ):
            if value is not None and value > self.revenue_last_12m:
                raise ValueError(f"{name} cannot exceed revenue_last_12m")

        top1 = self.conc_top1_pct
        top3 = self.conc_top3_pct
        if top1 is not None and top3 is not None and top1 > top3:
            # The top 3 include the top 1 by definition.
            raise ValueError("conc_top1_pct cannot exceed conc_top3_pct")

        # The engine refuses to grade a year that made no profit (R8/R10,
        # deviations register D21): revenue minus cost of goods sold, fixed
        # and variable cost must stay above zero. Refused here, at the figures,
        # so the SME fixes the number instead of the score run failing later.
        total_cost = self.cogs_y1 + self.fixed_cost_y1 + self.variable_cost_excl_cogs_y1
        if total_cost >= self.revenue_last_12m:
            raise ValueError(
                "cogs_y1 + fixed_cost_y1 + variable_cost_excl_cogs_y1 must be below revenue_last_12m"
            )

        return self


class LoanApplicationFiguresOut(LoanApplicationFiguresIn):
    """The stored figures, plus when they were last written.

    Returned so the wizard can repopulate itself when the applicant comes back
    to a draft rather than making them retype everything.
    """

    figures_updated_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Public rate estimate (landing page)
#
# Answered by a visitor with no account, so this model is the ONLY validation
# between the open internet and the grading engine. Every bound the engine
# enforces is re-stated here, because reaching `grade()`'s own ValueError means
# a 500 rather than a message the visitor can act on.
# --------------------------------------------------------------------------- #


class PublicRateEstimateIn(BaseModel):
    """What the landing-page form asks for.

    Deliberately NOT the prototype's field list. Three differences, each forced
    by what `grade_lite()` actually needs to return a real number:

      * `industry` is one of the engine's 15 supported industries rather than
        the prototype's 8 invented buckets, so nothing has to be mapped (and
        silently mis-mapped) on the way in.
      * `employee_count` is required. The engine derives `company_size` from it
        and cannot score without one.
      * `revenue_prior_12m` is required. Two annual totals are what the monthly
        series is modelled from; with one year there is no trend to read.
    """

    industry: str
    # Below 24 months is a soft gate in the engine, not a refusal, so the bound
    # here is only "a real trading history" -- the engine decides what it means.
    operating_months: int = Field(ge=1, le=1200)
    employee_count: int = Field(ge=1, le=200)

    revenue_last_12m: int = VndAmount
    revenue_prior_12m: int = VndAmount
    cogs_y1: int = VndAmount
    fixed_cost_y1: int = VndAmount
    variable_cost_excl_cogs_y1: int = Field(default=0, ge=0, le=VND_MAX)

    loan_amount: int = VndAmount
    duration_months: int

    # Optional refinements -- they sharpen the band rather than enable it.
    revenue_best_month: int | None = OptionalVndAmount
    revenue_worst_month: int | None = OptionalVndAmount
    conc_top1_pct: float | None = OptionalPercent
    conc_top3_pct: float | None = OptionalPercent

    model_config = {"extra": "forbid"}

    @field_validator("industry")
    @classmethod
    def _industry_is_known_to_the_engine(cls, value: str) -> str:
        # Rejected at the edge rather than inside grade(): an unrecognised
        # industry there is a ValueError, and a public form should say which
        # field is wrong instead of returning a generic failure.
        validated = engine_constraints.validate_industry(value)
        if validated is None:
            raise ValueError("industry is required")
        return validated

    @field_validator("duration_months")
    @classmethod
    def _duration_is_allowed(cls, value: int) -> int:
        validated = engine_constraints.validate_duration_months(value)
        if validated is None:
            raise ValueError("duration_months is required")
        return validated

    @field_validator("loan_amount")
    @classmethod
    def _loan_within_engine_bounds(cls, value: int) -> int:
        engine_constraints.validate_loan_size(Decimal(value))
        return value

    @model_validator(mode="after")
    def _check_internally_consistent(self) -> "PublicRateEstimateIn":
        best = self.revenue_best_month
        worst = self.revenue_worst_month
        if best is not None and worst is not None and worst > best:
            raise ValueError("revenue_worst_month cannot exceed revenue_best_month")
        for name, value in (
            ("revenue_best_month", best),
            ("revenue_worst_month", worst),
        ):
            if value is not None and value > self.revenue_last_12m:
                raise ValueError(f"{name} cannot exceed revenue_last_12m")
        if (
            self.conc_top1_pct is not None
            and self.conc_top3_pct is not None
            and self.conc_top1_pct > self.conc_top3_pct
        ):
            raise ValueError("conc_top1_pct cannot exceed conc_top3_pct")
        return self


class PublicRateEstimateOut(BaseModel):
    """An indicative band for an anonymous visitor.

    A strict subset of what the authenticated endpoint returns. The reference
    rate, the grade weights and the params file are all absent on purpose:
    this response is rendered on a public page, and the bank reference rate and
    the internal-rating formula are never shown externally.

    `engine_version` is kept because a visitor can screenshot a rate and ask
    about it later, and we need to know which engine produced it.
    """

    rate_low_pct: float
    rate_high_pct: float
    score_low: float
    score_high: float
    engine_version: str
    provisional: bool
    # Parallel lists, same order. The UI renders `assumption_codes` through its
    # own strings and falls back to `assumptions` for a code it has no string
    # for -- a backend deployed ahead of the frontend.
    assumptions: list[str]
    assumption_codes: list[str]


# --------------------------------------------------------------------------- #
# Admin: grading inputs assembled from an application's evidence
# (app/loans/grading_inputs.py)
# --------------------------------------------------------------------------- #


class GradingInputField(BaseModel):
    value: Any = None
    source: Literal["vat", "statements", "e-invoice", "cic", "kyb", "project", "typed", "sector", "config"] | None = None
    status: Literal["filled", "missing", "partial"]
    note: str | None = None


class EInvoiceEvidenceOut(BaseModel):
    parser_version: str
    seller_tax_code: str
    period: str
    months_covered: int
    signed: bool
    related_party_pct: float | None = None
    total_revenue_vnd: int | None = None
    warnings: list[dict[str, str]] = []


class TaxFilingsEvidenceOut(BaseModel):
    parser_version: str
    tax_code: str
    fiscal_year: int
    vat_months: int
    signed: bool
    interest_expense_vnd: int | None = None
    warnings: list[dict[str, str]] = []


class CicEvidenceOut(BaseModel):
    parser_version: str
    subject_type: str
    score: int | None
    rank: int | None
    scored_on: str | None
    # Million đồng, CIC's unit.
    debt_total_vnd_million: float | None
    debt_attention_vnd_million: float | None
    debt_bad_vnd_million: float | None
    negative_history: bool | None
    warnings: list[dict[str, str]]


class GradingInputsOut(BaseModel):
    application_id: UUID
    gradeable: bool
    # Inputs whose absence stops a grade outright (engine R8 / NOT NULL), as
    # opposed to optional ones that only score lower.
    blockers: list[str]
    fields: dict[str, GradingInputField]
    tax_filings: TaxFilingsEvidenceOut | None = None
    e_invoice: EInvoiceEvidenceOut | None = None
    cic: CicEvidenceOut | None = None


class SignerCheckOut(BaseModel):
    role: Literal["SME", "GUARANTOR"]
    name: str | None = None


class ListingDocumentChecksOut(BaseModel):
    """What the automatic check on upload established (app/loans/signing.py): NEAC accepted the signatures and
    they are the right signers', and the signed copy carries the approved numbers (its check code)."""

    checked_at: datetime | None = None
    verified_by: str | None = None
    terms_match: bool = False
    signers: list[SignerCheckOut] = []


class SignedListingDocumentOut(BaseModel):
    """One of the three documents the SME signs after approval (app/loans/signing.py).

    missing_details: the details it still prints as a blank, which must be completed before it can be signed
      (COMPANY_NAME, ENTERPRISE_CODE, COMPANY_ADDRESS, LEGAL_REPRESENTATIVE, BANK_ACCOUNT, and on the two the
      Guarantor signs GUARANTOR_NAME, GUARANTOR_ID, GUARANTOR_ADDRESS). Empty once signed.
    waiting_for: the document that must be signed first ("standing-offer" for the Loan Contract until the Offer is
      on file), else null.
    """

    signed: bool
    signed_at: datetime | None = None
    file_name: str | None = None
    checks: ListingDocumentChecksOut | None = None
    missing_details: list[str] = []
    waiting_for: str | None = None


class GuarantorAddressIn(BaseModel):
    """The Personal Guarantor's address (their permanent residence, as on the ID card), for the Standing Offer and
    the Loan Contract template when the eKYC carries none."""

    address: str = Field(min_length=8, max_length=300)

    @field_validator("address")
    @classmethod
    def _one_line(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 8:
            raise ValueError("Enter the full address")
        return value


class ListingDocumentsOut(BaseModel):
    """Where signing stands on an application.

    ready: FundLok approved the request and its terms are locked, so the two
      documents carry them and can be signed (or already are).
    published: the project is live on the marketplace. Signing all three
      publishes it; a project that went live earlier is published without them.
    guarantor_verified: the signing user has an approved eKYC with an ID
      number, which the Personal Guarantor's signature (on the Standing Offer
      and the Loan Contract template) is checked against. Without it neither
      can be accepted.
    """

    ready: bool
    published: bool
    guarantor_verified: bool
    # The Personal Guarantor's address as the Standing Offer and the Loan Contract print it, and where it came
    # from: "EKYC" (the ID card), "SME" (entered on this step, PUT .../guarantor-address) or null (none yet).
    guarantor_address: str | None = None
    guarantor_address_source: Literal["EKYC", "SME"] | None = None
    service_agreement: SignedListingDocumentOut
    standing_offer: SignedListingDocumentOut
    loan_contract: SignedListingDocumentOut
    # The Personal Guarantor (Money flow FINAL v1.0): what they still lack before signing opens ("EKYC", "PHONE"),
    # how they signed the Standing Offer and Loan Contract ("ZALO_OTP", or "DIGITAL_SIGNATURE" for copies signed
    # in the PDF before the Zalo code), when, and the masked number the code goes to.
    guarantor_missing: list[Literal["EKYC", "PHONE"]] = []
    guarantor_signed_by: Literal["ZALO_OTP", "DIGITAL_SIGNATURE"] | None = None
    guarantor_signed_at: datetime | None = None
    guarantor_phone: str | None = None


class GuarantorCodeOut(BaseModel):
    """A Zalo code was sent to the guarantor: to `phone` (masked), good for `expires_in` seconds, the next one in
    `resend_in` seconds. `channel` is "zalo" (or "log" in local development)."""

    phone: str
    channel: str
    expires_in: int
    resend_in: int


class GuarantorSignRequest(BaseModel):
    code: str
