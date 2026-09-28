import math
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.projects.engine_constraints import (
    params,
    validate_duration_months,
    validate_industry,
)

# Every bound below is READ from the grading engine's own params, never retyped.
# The calculator and a real score run have to agree on what is fundable, and a
# hand-copied literal is exactly how those two drift apart (see the same rule in
# tests/projects/test_create_project_validation.py).
_LOAN_CONSTRAINTS = params().loan_constraints
LOAN_MIN_VND = int(_LOAN_CONSTRAINTS["min_vnd"])
LOAN_MAX_VND = int(_LOAN_CONSTRAINTS["max_vnd"])
ALLOWED_TENORS = tuple(int(m) for m in _LOAN_CONSTRAINTS["allowed_durations_months"])

# A ceiling on the *reported* figures, which are not the loan and so are not
# bounded by loan_constraints. Without one, `revenue_l12m` accepted any integer
# a caller cared to send: Python ints are unbounded, so a 400-digit number was
# validated, multiplied through the metrics, and then handed to asyncpg, which
# raises on anything past int8 — a 500 from a public unauthenticated endpoint.
# 1e15 VND is ~40x Vietnam's GDP, so nothing legitimate is excluded.
FIGURE_MAX_VND = 1_000_000_000_000_000


def _sanitize_money_value(v: Any) -> int:
    if v is None:
        raise ValueError("Money value cannot be null")
    if isinstance(v, float) and not math.isfinite(v):
        # json.loads accepts the literals NaN and Infinity. int(inf) raises
        # OverflowError, which Pydantic does NOT convert to a validation error,
        # so without this an {"revenue_l12m": Infinity} body was a 500.
        raise ValueError("Money value must be a finite number")
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        cleaned = (
            v.replace(",", "")
            .replace(".", "")
            .replace(" ", "")
            .replace("₫", "")
            .replace("VND", "")
            .replace("vnd", "")
            .strip()
        )
        if not cleaned:
            raise ValueError("Money value cannot be empty")
        return int(cleaned)
    raise ValueError(f"Invalid monetary value: {v}")


def _sanitize_optional_money_value(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return _sanitize_money_value(v)


def _sanitize_pct_value(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        val = float(v)
    elif isinstance(v, str):
        val = float(v.replace("%", "").strip())
    else:
        raise ValueError(f"Invalid percentage value: {v}")
    # NaN compares False against everything, so it sailed through the range
    # check below. float("nan") also parses from the STRING "nan".
    if not math.isfinite(val):
        raise ValueError("Percentage must be a finite number")
    if val < 0 or val > 100:
        raise ValueError(f"Percentage must be between 0 and 100, got {val}")
    return round(val, 2)


class SeasonalityInput(BaseModel):
    peak_month_revenue: int | None = Field(default=None, ge=0, le=FIGURE_MAX_VND)
    lowest_month_revenue: int | None = Field(default=None, ge=0, le=FIGURE_MAX_VND)
    top_1_customer_share: float | None = Field(default=None, ge=0, le=100)
    top_3_customer_share: float | None = Field(default=None, ge=0, le=100)

    @field_validator("peak_month_revenue", "lowest_month_revenue", mode="before")
    @classmethod
    def validate_money(cls, v: Any) -> int | None:
        return _sanitize_optional_money_value(v)

    @field_validator("top_1_customer_share", "top_3_customer_share", mode="before")
    @classmethod
    def validate_pct(cls, v: Any) -> float | None:
        return _sanitize_pct_value(v)


_PHONE_ALLOWED = re.compile(r"^[0-9+()\s.\-]{8,20}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def normalise_contact_email(v: str) -> str:
    return v.strip().lower()


def validate_contact_phone(v: Any) -> str:
    """A phone number a person could be called on: 8-15 digits, with the
    separators people actually type. Shared by both rate-page forms."""
    if not isinstance(v, str) or not v.strip():
        raise ValueError("phone is required")
    if not _PHONE_ALLOWED.match(v.strip()):
        raise ValueError("phone must be digits, spaces, +, (), . or -")
    digits = sum(ch.isdigit() for ch in v)
    if not 8 <= digits <= 15:
        raise ValueError("phone must contain 8 to 15 digits")
    return v.strip()


class RateCalculationRequest(BaseModel):
    # `validate_assignment` matters here, it is not decoration. The router used
    # to backfill `session_id` from a header or cookie by plain attribute
    # assignment, which Pydantic does NOT validate by default — so `max_length`
    # was enforced on the body and bypassed entirely by the header path, and a
    # 200KB X-Session-Id header reached a String(128) column and 500'd. With
    # this on, the backfill goes through the same validator as the body.
    model_config = ConfigDict(validate_assignment=True)

    industry: str = Field(..., min_length=1, max_length=100)
    operating_months: int = Field(..., ge=0, le=1_200)
    employee_count: int = Field(..., ge=0, le=100_000)

    revenue_l12m: int = Field(..., gt=0, le=FIGURE_MAX_VND)
    revenue_prev_12m: int = Field(..., ge=0, le=FIGURE_MAX_VND)
    cogs_l12m: int = Field(..., ge=0, le=FIGURE_MAX_VND)
    fixed_costs_l12m: int = Field(..., ge=0, le=FIGURE_MAX_VND)
    variable_costs_l12m: int = Field(..., ge=0, le=FIGURE_MAX_VND)

    requested_amount: int = Field(..., ge=LOAN_MIN_VND, le=LOAN_MAX_VND)
    tenor_months: int = Field(..., ge=min(ALLOWED_TENORS), le=max(ALLOWED_TENORS))

    seasonality: SeasonalityInput | None = None
    session_id: str | None = Field(default=None, max_length=128)

    # Contact, so the team can follow up on the estimate. Consent must be
    # literally true: it is what makes storing an email and phone lawful
    # under Decree 13/2023, as on the investor form. A client that skipped it
    # gets a 422, not a quietly stored row.
    email: EmailStr = Field(..., max_length=254)
    phone: str = Field(..., max_length=20)
    consent_contact: Literal[True]

    # Cloudflare Turnstile. Optional on the type so the field can be absent in
    # tests and in local dev, where CLOUDFLARE_TURNSTILE_SECRET_KEY is unset and
    # `verify_turnstile_token` no-ops. In any environment that HAS the secret
    # configured, a missing token is rejected by that verifier, not here.
    turnstile_token: str | None = Field(default=None, max_length=4096)

    # Optional flat fields in case client submits without nesting in seasonality
    peak_month_revenue: int | None = Field(default=None, ge=0, le=FIGURE_MAX_VND)
    lowest_month_revenue: int | None = Field(default=None, ge=0, le=FIGURE_MAX_VND)
    top_1_customer_share: float | None = Field(default=None, ge=0, le=100)
    top_3_customer_share: float | None = Field(default=None, ge=0, le=100)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        return normalise_contact_email(v)

    @field_validator("phone", mode="before")
    @classmethod
    def _validate_phone(cls, v: Any) -> str:
        return validate_contact_phone(v)

    @field_validator("industry")
    @classmethod
    def validate_industry_against_engine(cls, v: str) -> str:
        # The engine raises ValueError on an industry it has no row for, and it
        # is called below on this exact value. Validating here turns that into a
        # 422 naming the field instead of a 500 from inside grade().
        # The shared validator quotes the rejected value back in its message.
        # Fine behind a login; on this unauthenticated endpoint it would make
        # the 422 a reflector for whatever string a caller chose to send.
        try:
            validated = validate_industry(v)
        except ValueError:
            raise ValueError(
                "industry is not one of the industries the grading engine supports"
            ) from None
        if validated is None:
            raise ValueError("industry is required")
        return validated

    @field_validator("tenor_months")
    @classmethod
    def validate_tenor_against_engine(cls, v: int) -> int:
        validate_duration_months(v)
        return v

    @field_validator(
        "revenue_l12m",
        "revenue_prev_12m",
        "cogs_l12m",
        "fixed_costs_l12m",
        "variable_costs_l12m",
        "requested_amount",
        mode="before",
    )
    @classmethod
    def validate_money_fields(cls, v: Any) -> int:
        return _sanitize_money_value(v)

    @field_validator("peak_month_revenue", "lowest_month_revenue", mode="before")
    @classmethod
    def validate_opt_money(cls, v: Any) -> int | None:
        return _sanitize_optional_money_value(v)

    @field_validator("top_1_customer_share", "top_3_customer_share", mode="before")
    @classmethod
    def validate_opt_pct(cls, v: Any) -> float | None:
        return _sanitize_pct_value(v)

    @model_validator(mode="after")
    def validate_figures_are_mutually_possible(self) -> "RateCalculationRequest":
        """Reject figure sets no real business can have.

        Each field was range-checked on its own, but the combinations were not,
        and the engine priced them anyway: a best month of 900 trillion against
        4B of annual revenue, a worst month above the best, a top-1 customer
        share above the top-3. Every one of those came back 200 with a rate and
        a stored row. Values may arrive nested under `seasonality` or flat, so
        both are resolved the same way the service resolves them.
        """
        s = self.seasonality

        def pick(name: str):
            nested = getattr(s, name) if s is not None else None
            return nested if nested is not None else getattr(self, name)

        best = pick("peak_month_revenue")
        worst = pick("lowest_month_revenue")
        top1 = pick("top_1_customer_share")
        top3 = pick("top_3_customer_share")

        if best is not None and best > self.revenue_l12m:
            raise ValueError(
                "peak_month_revenue cannot exceed revenue for the last 12 months"
            )
        if best is not None and worst is not None and worst > best:
            raise ValueError(
                "lowest_month_revenue cannot exceed peak_month_revenue"
            )
        if top1 is not None and top3 is not None and top1 > top3:
            raise ValueError(
                "top_1_customer_share cannot exceed top_3_customer_share"
            )
        return self


class RateRange(BaseModel):
    min_rate_monthly: float
    max_rate_monthly: float
    apr_min: float
    apr_max: float


class EstimatedMonthlyPayment(BaseModel):
    min: int
    max: int


class RiskProfile(BaseModel):
    """What the public page is allowed to see about the scoring.

    The three percentages are the visitor's OWN figures divided by each other —
    they carry no model information and are useful feedback, so they stay.

    What is deliberately absent is the engine's numeric `final_grade`. Pricing
    is `min(cap, BULR + (cap - BULR) * (100 - grade) / 100)`, so returning the
    grade alongside the rate lets any caller solve that single equation for the
    bank reference rate in one unauthenticated request. `tier` is a three-bucket
    projection of the grade: enough to label the result, far too coarse to
    invert. See grading-engine-core.md §10.
    """

    tier: str
    growth_rate_pct: float
    ebitda_margin_pct: float
    debt_to_revenue_pct: float
    is_operating_loss: bool = False


class RateCalculationData(BaseModel):
    rate_range: RateRange
    estimated_monthly_payment: EstimatedMonthlyPayment
    risk_profile: RiskProfile
    # The engine's own caveats. `grade_lite` brackets an unknown CIC score and
    # assumes KYC passes, so every band it returns is indicative and says so;
    # dropping these on the floor would let the page present a guess as a quote.
    provisional: bool = True
    assumption_codes: list[str] = []


class RateCalculationResponse(BaseModel):
    inquiry_id: UUID
    status: str = "success"
    data: RateCalculationData


class RateInquiryAdminItem(BaseModel):
    id: UUID
    created_at: datetime
    created_at_formatted: str
    industry: str
    industry_display: str
    operating_months: int
    employee_count: int
    tenure_staff_display: str
    revenue_l12m: int
    revenue_l12m_formatted: str
    revenue_prev_12m: int
    cogs_l12m: int
    fixed_costs_l12m: int
    variable_costs_l12m: int
    yoy_growth_pct: float
    yoy_growth_display: str
    requested_amount: int
    tenor_months: int
    loan_ask_display: str
    estimated_rate_min: float
    estimated_rate_max: float
    calculated_rate_display: str
    risk_tier: str | None
    tier_display: str
    calculated_monthly_payment: int | None
    calculated_monthly_payment_formatted: str | None
    session_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    # Contact for follow-up. Null on inquiries made before the form asked.
    email: str | None = None
    phone: str | None = None
    consented_contact_at: datetime | None = None

    model_config = {"from_attributes": True}


class RateInquiriesAdminListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[RateInquiryAdminItem]


# --------------------------------------------------------------------------- #
# Investor tab
# --------------------------------------------------------------------------- #

# Floor and ceiling on the amount an investor types. The prototype only had
# min="1"; a 1-dong commitment tells sales nothing and an unbounded one is the
# BIGINT overflow already fixed on the SME side. 1 trillion VND is far above
# any retail or HNW ticket this page is for.
INVESTOR_AMOUNT_MIN_VND = 1_000_000
INVESTOR_AMOUNT_MAX_VND = 1_000_000_000_000



class InvestorChoices(BaseModel):
    """The four inputs the yield depends on. No personal data."""

    amount_vnd: int = Field(..., ge=INVESTOR_AMOUNT_MIN_VND, le=INVESTOR_AMOUNT_MAX_VND)
    commitment_months: Literal[6, 12]
    risk_tier: Literal["conservative", "balanced", "growth"]
    reinvestment_cadence: Literal["none", "quarterly", "monthly", "weekly", "daily"]

    @field_validator("amount_vnd", mode="before")
    @classmethod
    def validate_amount(cls, v: Any) -> int:
        return _sanitize_money_value(v)


class InvestorLeadCreate(InvestorChoices):
    """Pressing "Calculate my target yield": the lead-capture moment."""

    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr = Field(..., max_length=254)
    # Required, like the borrower form: the team follows up by phone.
    phone: str = Field(..., max_length=20)
    locale: Literal["vi", "en"] | None = None

    # Must be literally true. The gate ("this is an illustration, not an
    # offer") is the page's compliance posture, and contact consent is what
    # makes storing a name and email lawful under Decree 13/2023. A client that
    # skipped either gets a 422, not a quietly stored row.
    acknowledged_illustrative: Literal[True]
    consent_contact: Literal[True]

    session_id: str | None = Field(default=None, max_length=128)
    turnstile_token: str | None = Field(default=None, max_length=4096)

    @field_validator("full_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        # Shown to staff in the admin table and possibly pasted into email; a
        # name carrying newlines or control characters is either broken input
        # or an attempt to inject into whatever consumes it next.
        cleaned = " ".join(v.split())
        if _CONTROL_CHARS.search(v) or len(cleaned) < 2:
            raise ValueError("full_name must be a plain name")
        return cleaned

    @field_validator("email")
    @classmethod
    def normalise_email(cls, v: str) -> str:
        return normalise_contact_email(v)

    @field_validator("phone", mode="before")
    @classmethod
    def validate_phone(cls, v: Any) -> str:
        return validate_contact_phone(v)


class InvestorEstimateOut(BaseModel):
    """The published walk-down. Deliberately absent: the internal rating each
    tier maps to and the bank reference rate — together with `loan_rate_pct`
    they solve the pricing formula."""

    loan_rate_pct: float
    expected_loss_pct: float
    fee_pct: float
    net_per_loan_pct: float
    net_apy_pct: float
    estimated_return_vnd: int
    avg_loan_months: int
    capital_turns: float


class InvestorTierOut(BaseModel):
    risk_tier: Literal["conservative", "balanced", "growth"]
    loan_rate_pct: float


class InvestorTiersResponse(BaseModel):
    """For the tier hint shown BEFORE calculating ("loans priced around
    14.0% a year"). A loan rate, never the yield — the yield stays behind the
    lead capture, as the prototype specifies."""

    tiers: list[InvestorTierOut]


class InvestorLeadResponse(BaseModel):
    lead_id: UUID
    reference: str
    estimate: InvestorEstimateOut


class InvestorSignupResponse(BaseModel):
    reference: str
    signed_up_at: datetime


class InvestorLeadAdminItem(BaseModel):
    id: UUID
    reference: str
    created_at: datetime
    full_name: str
    email: str
    phone: str | None
    locale: str | None
    amount_vnd: int
    commitment_months: int
    risk_tier: str
    reinvestment_cadence: str
    bank_rate_pct: float
    loan_rate_pct: float
    net_per_loan_pct: float
    net_apy_pct: float
    estimated_return_vnd: int
    acknowledged_illustrative_at: datetime
    consented_contact_at: datetime
    signed_up_at: datetime | None
    signup_amount_vnd: int | None
    signup_commitment_months: int | None
    signup_risk_tier: str | None
    signup_reinvestment_cadence: str | None
    signup_net_apy_pct: float | None
    session_id: str | None
    ip_address: str | None
    user_agent: str | None


class InvestorLeadsAdminListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[InvestorLeadAdminItem]
