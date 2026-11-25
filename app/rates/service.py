import ipaddress
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.loans.lite_grading import LiteBand, LiteGradingError, grade_lite
from app.rates.models import RateInquiry
from app.rates.schemas import (
    EstimatedMonthlyPayment,
    RateCalculationData,
    RateCalculationRequest,
    RateCalculationResponse,
    RateInquiriesAdminListResponse,
    RateInquiryAdminItem,
    RateRange,
    RiskProfile,
)


logger = logging.getLogger(__name__)


def parse_valid_ip(raw_ip: str | None) -> str | None:
    if not raw_ip:
        return None
    # If comma-separated (e.g. from X-Forwarded-For: client, proxy1, proxy2)
    candidate = raw_ip.split(",")[0].strip()
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        return None


# Where the engine's 0-100 grade is cut into the three labels the public page
# renders. Coarse on purpose: see RiskProfile's docstring for why the numeric
# grade itself is not returned. The boundaries are presentation, not pricing —
# moving them changes a badge and nothing about what anyone is charged.
#
# Calibrated against what the engine actually emits, not against the 0-100
# scale: sweeping supported industries x tenure x margin puts real applicants
# between roughly 56 and 83, so cuts at 80/20 would have labelled every visitor
# TIER_C and taught them nothing. Worth revisiting once `rate_inquiries` holds
# enough real submissions to read the live distribution off.
TIER_A_MIN_GRADE = 70.0
TIER_B_MIN_GRADE = 60.0


def tier_for_grade(grade_pct: float) -> str:
    if grade_pct >= TIER_A_MIN_GRADE:
        return "TIER_A"
    if grade_pct >= TIER_B_MIN_GRADE:
        return "TIER_B"
    return "TIER_C"


def derive_self_reported_metrics(
    revenue_l12m: int,
    revenue_prev_12m: int,
    cogs_l12m: int,
    fixed_costs_l12m: int,
    variable_costs_l12m: int,
    requested_amount: int,
) -> tuple[float, float, float, bool]:
    """The visitor's own figures divided by each other.

    These are DISPLAY only — the rate no longer comes from them. They are the
    caller's inputs rearranged, so returning them discloses nothing about the
    model, which is exactly why they survived the move to the real engine.
    """
    if revenue_prev_12m > 0:
        growth_rate_pct = round(((revenue_l12m - revenue_prev_12m) / revenue_prev_12m) * 100, 2)
    else:
        growth_rate_pct = 0.0

    total_costs = cogs_l12m + fixed_costs_l12m + variable_costs_l12m
    is_operating_loss = total_costs >= revenue_l12m
    ebitda_margin_pct = (
        round(((revenue_l12m - total_costs) / revenue_l12m) * 100, 2) if revenue_l12m > 0 else 0.0
    )
    debt_to_revenue_pct = (
        round((requested_amount / revenue_l12m) * 100, 2) if revenue_l12m > 0 else 100.0
    )
    return growth_rate_pct, ebitda_margin_pct, debt_to_revenue_pct, is_operating_loss


def _band_to_rate_range(band: LiteBand) -> RateRange:
    """The engine quotes an ANNUAL rate; this page has always shown monthly.

    `rate_low_pct`/`rate_high_pct` come out of the same pricing formula a real
    score run uses, capped at `pricing.rate_cap_pct`, so the monthly figures are
    a straight division and the APR is the engine's own number — not a monthly
    rate multiplied back up, which would reintroduce rounding the cap was
    applied before.
    """
    return RateRange(
        min_rate_monthly=round(band.rate_low_pct / 12, 2),
        max_rate_monthly=round(band.rate_high_pct / 12, 2),
        apr_min=round(band.rate_low_pct, 2),
        apr_max=round(band.rate_high_pct, 2),
    )


def _monthly_payments(
    requested_amount: int, tenor_months: int, rate_range: RateRange
) -> EstimatedMonthlyPayment:
    principal_per_month = requested_amount / tenor_months
    return EstimatedMonthlyPayment(
        min=round(principal_per_month + requested_amount * (rate_range.min_rate_monthly / 100)),
        max=round(principal_per_month + requested_amount * (rate_range.max_rate_monthly / 100)),
    )


async def process_rate_calculation(
    db: AsyncSession,
    body: RateCalculationRequest,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> RateCalculationResponse:
    # Resolve seasonality & concentration fields
    seasonality = body.seasonality
    peak_month_revenue = (
        seasonality.peak_month_revenue
        if seasonality and seasonality.peak_month_revenue is not None
        else body.peak_month_revenue
    )
    lowest_month_revenue = (
        seasonality.lowest_month_revenue
        if seasonality and seasonality.lowest_month_revenue is not None
        else body.lowest_month_revenue
    )
    top_1_customer_share = (
        seasonality.top_1_customer_share
        if seasonality and seasonality.top_1_customer_share is not None
        else body.top_1_customer_share
    )
    top_3_customer_share = (
        seasonality.top_3_customer_share
        if seasonality and seasonality.top_3_customer_share is not None
        else body.top_3_customer_share
    )

    # 1. Price it with the SAME engine a real score run uses.
    #
    # This endpoint used to carry its own TIER_A/B/C table with hardcoded
    # 1.2-2.4%/month spreads. Nothing kept those literals in step with
    # grading_params_v1.yaml, so the number a visitor was quoted on the landing
    # page and the number the engine produced after they signed up were two
    # unrelated calculations that happened to share a unit — including the
    # `rate_cap_pct` ceiling, which the old table did not apply at all.
    # `grade_lite` is the adapter the Lite flow already uses (see
    # app/loans/service.py::public_rate_estimate); going through it means the
    # calculator is off from an admin score run only by the inputs it cannot
    # ask for, not by the arithmetic.
    from app.rates.bank_rate import bank_rate_in_force

    bank_rate_pct = await bank_rate_in_force(db)
    try:
        band = grade_lite(
            bank_rate_pct=bank_rate_pct,
            # No company exists yet — the engine never reads this, it is a label.
            company_code="rate-calculator",
            industry=body.industry,
            employee_count=body.employee_count,
            incorporation_date_months=body.operating_months,
            loan_size_vnd=Decimal(body.requested_amount),
            duration_months=body.tenor_months,
            figures={
                "revenue_last_12m": body.revenue_l12m,
                "revenue_prior_12m": body.revenue_prev_12m,
                "revenue_best_month": peak_month_revenue,
                "revenue_worst_month": lowest_month_revenue,
                "cogs_y1": body.cogs_l12m,
                "fixed_cost_y1": body.fixed_costs_l12m,
                "variable_cost_excl_cogs_y1": body.variable_costs_l12m,
                # Not asked on a public calculator: a question about the owner's
                # personal draw only refines the score, it does not enable it.
                "owner_withdrawal_pct": None,
                "conc_top1_pct": top_1_customer_share,
                "conc_top3_pct": top_3_customer_share,
            },
        )
    except LiteGradingError as exc:
        # A real answer about the visitor's own numbers — costs at or above
        # revenue, an industry with no sector row. The message is written to be
        # shown, so it is the detail; the code rides in a header so `detail`
        # stays the plain string every existing client already parses, and a
        # Vietnamese visitor gets Vietnamese rather than the English prose.
        raise HTTPException(
            status_code=422,
            detail=str(exc),
            headers={"X-Error-Code": exc.code},
        ) from exc
    except ValueError as exc:
        # grade()'s own input validation. Every bound it checks is also checked
        # in RateCalculationRequest against the same YAML, so reaching this
        # means a bound drifted — still a 422, never a 500 on a public page.
        # The engine's message is NOT forwarded: unlike LiteGradingError it is
        # written for a developer and names internal parameters, and this is an
        # unauthenticated endpoint. It goes to the log, where drift gets noticed.
        logger.warning("Engine rejected a rate-calculator input: %s", exc)
        raise HTTPException(
            status_code=422,
            detail="These figures cannot be assessed.",
            headers={"X-Error-Code": "UNSCOREABLE"},
        ) from exc

    rate_range = _band_to_rate_range(band)
    estimated_monthly_payment = _monthly_payments(
        body.requested_amount, body.tenor_months, rate_range
    )
    growth_rate_pct, ebitda_margin_pct, debt_to_revenue_pct, is_operating_loss = (
        derive_self_reported_metrics(
            revenue_l12m=body.revenue_l12m,
            revenue_prev_12m=body.revenue_prev_12m,
            cogs_l12m=body.cogs_l12m,
            fixed_costs_l12m=body.fixed_costs_l12m,
            variable_costs_l12m=body.variable_costs_l12m,
            requested_amount=body.requested_amount,
        )
    )
    risk_profile = RiskProfile(
        # The worse end of the bracket, so the badge never flatters the
        # applicant more than the rate they were just quoted.
        tier=tier_for_grade(band.grade_low),
        growth_rate_pct=growth_rate_pct,
        ebitda_margin_pct=ebitda_margin_pct,
        debt_to_revenue_pct=debt_to_revenue_pct,
        is_operating_loss=is_operating_loss,
    )
    stored_monthly_payment = estimated_monthly_payment.min

    valid_ip = parse_valid_ip(client_ip)

    # 2. Persistence into rate_inquiries
    inquiry = RateInquiry(
        session_id=body.session_id,
        ip_address=valid_ip,
        user_agent=user_agent,
        industry=body.industry,
        operating_months=body.operating_months,
        employee_count=body.employee_count,
        revenue_l12m=body.revenue_l12m,
        revenue_prev_12m=body.revenue_prev_12m,
        cogs_l12m=body.cogs_l12m,
        fixed_costs_l12m=body.fixed_costs_l12m,
        variable_costs_l12m=body.variable_costs_l12m,
        requested_amount=body.requested_amount,
        tenor_months=body.tenor_months,
        peak_month_revenue=peak_month_revenue,
        lowest_month_revenue=lowest_month_revenue,
        top_1_customer_share=top_1_customer_share,
        top_3_customer_share=top_3_customer_share,
        estimated_rate_min=rate_range.min_rate_monthly,
        estimated_rate_max=rate_range.max_rate_monthly,
        risk_tier=risk_profile.tier,
        calculated_monthly_payment=stored_monthly_payment,
        full_name=body.full_name,
        company_name=body.company_name,
        email=body.email,
        phone=body.phone,
        consented_contact_at=datetime.now(timezone.utc),
    )
    db.add(inquiry)
    await db.commit()
    await db.refresh(inquiry)

    return RateCalculationResponse(
        inquiry_id=inquiry.id,
        status="success",
        data=RateCalculationData(
            rate_range=rate_range,
            estimated_monthly_payment=estimated_monthly_payment,
            risk_profile=risk_profile,
            provisional=band.provisional,
            assumption_codes=list(band.assumption_codes),
        ),
    )


def _format_industry(industry: str) -> str:
    cleaned = industry.replace("_", " ").strip()
    words = cleaned.split()
    formatted_words = []
    for w in words:
        if w.lower() in ("fmcg", "f&b", "sme", "b2b", "b2c", "it"):
            formatted_words.append(w.upper())
        else:
            formatted_words.append(w.capitalize())
    return " ".join(formatted_words)


def _format_loan_ask(amount: int, tenor: int) -> str:
    if amount >= 1_000_000_000 and amount % 1_000_000_000 == 0:
        amount_str = f"{amount // 1_000_000_000}B ₫"
    elif amount >= 1_000_000_000:
        amount_str = f"{amount / 1_000_000_000:.1f}B ₫"
    elif amount % 1_000_000 == 0:
        amount_str = f"{amount // 1_000_000}M ₫"
    else:
        amount_str = f"{amount / 1_000_000:.1f}M ₫"
    return f"{amount_str} / {tenor} mos"


_OPERATING_BAND_DISPLAY = {
    "LT_1Y": "< 1 yr (quiz)",
    "Y1_3": "1–3 yrs (quiz)",
    "GT_3Y": "> 3 yrs (quiz)",
}


def _vnd(amount: int) -> str:
    return f"{amount:,.0f} ₫".replace(",", ".")


def _format_quiz_row(row: RateInquiry) -> RateInquiryAdminItem:
    """A lead from the illustrative quiz. It has no calculator figures, so the
    display strings carry the quiz answers instead and the numbers stay null."""
    rate = float(row.estimated_rate_min) if row.estimated_rate_min is not None else None
    return RateInquiryAdminItem(
        id=row.id,
        source="QUIZ",
        created_at=row.created_at,
        created_at_formatted=row.created_at.strftime("%d/%m/%Y %H:%M") if row.created_at else "",
        industry=row.industry,
        industry_display=_format_industry(row.industry),
        operating_months=None,
        employee_count=None,
        tenure_staff_display=_OPERATING_BAND_DISPLAY.get(row.operating_band or "", "—"),
        revenue_l12m=None,
        revenue_l12m_formatted=f"{_vnd(row.monthly_revenue)} / mo" if row.monthly_revenue else "—",
        revenue_prev_12m=None,
        cogs_l12m=None,
        fixed_costs_l12m=None,
        variable_costs_l12m=None,
        yoy_growth_pct=None,
        yoy_growth_display="—",
        requested_amount=row.requested_amount,
        tenor_months=row.tenor_months,
        loan_ask_display=_format_loan_ask(row.requested_amount, row.tenor_months),
        estimated_rate_min=rate,
        estimated_rate_max=rate,
        calculated_rate_display=f"{rate:.2f}% / mo" if rate is not None else "Declined",
        risk_tier=None,
        tier_display="Quiz",
        calculated_monthly_payment=None,
        calculated_monthly_payment_formatted=None,
        session_id=row.session_id,
        ip_address=str(row.ip_address) if row.ip_address else None,
        user_agent=row.user_agent,
        full_name=row.full_name,
        company_name=row.company_name,
        email=row.email,
        phone=row.phone,
        consented_contact_at=row.consented_contact_at,
        monthly_revenue=row.monthly_revenue,
        operating_band=row.operating_band,
        quiz_outcome=row.quiz_outcome,
        quiz_params_version=row.quiz_params_version,
        utm_source=row.utm_source,
        utm_medium=row.utm_medium,
        utm_campaign=row.utm_campaign,
        utm_content=row.utm_content,
        utm_term=row.utm_term,
        fbclid=row.fbclid,
    )


def format_inquiry_row(row: RateInquiry) -> RateInquiryAdminItem:
    if row.source == "QUIZ":
        return _format_quiz_row(row)

    # Calculate YoY growth
    if row.revenue_prev_12m > 0:
        growth_pct = round(((row.revenue_l12m - row.revenue_prev_12m) / row.revenue_prev_12m) * 100, 2)
    else:
        growth_pct = 0.0

    created_at_fmt = row.created_at.strftime("%d/%m/%Y %H:%M") if row.created_at else ""
    rev_formatted = f"{row.revenue_l12m:,.0f} ₫".replace(",", ".")
    growth_display = f"{growth_pct:+.1f}%"
    loan_ask = _format_loan_ask(row.requested_amount, row.tenor_months)
    min_r = float(row.estimated_rate_min)
    max_r = float(row.estimated_rate_max)
    rate_display = f"{min_r:.1f}% - {max_r:.1f}% / mo"
    tier_display = row.risk_tier.replace("_", " ").title() if row.risk_tier else "N/A"
    monthly_payment_formatted = (
        f"{row.calculated_monthly_payment:,.0f} ₫".replace(",", ".")
        if row.calculated_monthly_payment is not None
        else None
    )

    return RateInquiryAdminItem(
        id=row.id,
        created_at=row.created_at,
        created_at_formatted=created_at_fmt,
        industry=row.industry,
        industry_display=_format_industry(row.industry),
        operating_months=row.operating_months,
        employee_count=row.employee_count,
        tenure_staff_display=f"{row.operating_months} mos / {row.employee_count} staff",
        revenue_l12m=row.revenue_l12m,
        revenue_l12m_formatted=rev_formatted,
        revenue_prev_12m=row.revenue_prev_12m,
        cogs_l12m=row.cogs_l12m,
        fixed_costs_l12m=row.fixed_costs_l12m,
        variable_costs_l12m=row.variable_costs_l12m,
        yoy_growth_pct=growth_pct,
        yoy_growth_display=growth_display,
        requested_amount=row.requested_amount,
        tenor_months=row.tenor_months,
        loan_ask_display=loan_ask,
        estimated_rate_min=min_r,
        estimated_rate_max=max_r,
        calculated_rate_display=rate_display,
        risk_tier=row.risk_tier,
        tier_display=tier_display,
        calculated_monthly_payment=row.calculated_monthly_payment,
        calculated_monthly_payment_formatted=monthly_payment_formatted,
        session_id=row.session_id,
        ip_address=str(row.ip_address) if row.ip_address else None,
        user_agent=row.user_agent,
        full_name=row.full_name,
        company_name=row.company_name,
        email=row.email,
        phone=row.phone,
        consented_contact_at=row.consented_contact_at,
        utm_source=row.utm_source,
        utm_medium=row.utm_medium,
        utm_campaign=row.utm_campaign,
        utm_content=row.utm_content,
        utm_term=row.utm_term,
        fbclid=row.fbclid,
    )


async def list_rate_inquiries_admin(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    industry: str | None = None,
    risk_tier: str | None = None,
    source: str | None = None,
) -> RateInquiriesAdminListResponse:
    query = select(RateInquiry).order_by(desc(RateInquiry.created_at))
    if source:
        query = query.where(RateInquiry.source == source)
    if industry:
        # Parameterised, so not injectable -- but `%` and `_` are LIKE
        # wildcards, and unescaped they let a filter of "%" match everything.
        escaped = industry.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(RateInquiry.industry.ilike(f"%{escaped}%", escape="\\"))
    if risk_tier:
        query = query.where(RateInquiry.risk_tier == risk_tier)

    # Total count
    count_query = select(func.count()).select_from(query.subquery())
    total_count = (await db.execute(count_query)).scalar_one()

    # Paginate
    paginated_query = query.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(paginated_query)).scalars().all()

    items = [format_inquiry_row(r) for r in rows]

    return RateInquiriesAdminListResponse(
        total=total_count,
        page=page,
        page_size=page_size,
        items=items,
    )


async def get_rate_inquiry_by_id_admin(
    db: AsyncSession,
    inquiry_id: UUID,
) -> RateInquiryAdminItem | None:
    query = select(RateInquiry).where(RateInquiry.id == inquiry_id)
    row = (await db.execute(query)).scalar_one_or_none()
    if not row:
        return None
    return format_inquiry_row(row)
