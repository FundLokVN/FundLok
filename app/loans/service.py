from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.lending.models import LoanApplication
from app.loans.lite_grading import LiteGradingError, grade_lite
from app.loans.schemas import (
    LoanApplicationCreate,
    LoanApplicationFiguresIn,
    PublicRateEstimateIn,
    PublicRateEstimateOut,
)
from app.projects.service import user_owns_project
from app.users.models import Role, User
from uuid import UUID


async def create_application(db: AsyncSession, body: LoanApplicationCreate, current_user: User) -> LoanApplication:
    if current_user.role != Role.SME.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only SMEs can create applications")
    if not await user_owns_project(db, current_user.id, body.business_id):
        raise HTTPException(status_code=403, detail="Business does not belong to this user")
    app_row = LoanApplication(
        project_id=body.business_id,
        requested_amount=body.requested_amount,
        purpose=body.purpose,
        repayment_preference=body.repayment_preference,
        status="DRAFT",
    )
    db.add(app_row)
    await db.flush()
    await db.refresh(app_row)
    return app_row


async def _owned_application(
    db: AsyncSession, application_id: UUID, current_user: User
) -> LoanApplication:
    """Load an application the caller is actually allowed to touch.

    404 rather than 403 when the application exists but belongs to someone
    else: telling a stranger "this exists but is not yours" confirms which
    application ids are real, which is an enumeration oracle over other SMEs'
    borrowing.
    """
    result = await db.execute(select(LoanApplication).where(LoanApplication.id == application_id))
    row = result.scalar_one_or_none()
    if not row or not await user_owns_project(db, current_user.id, row.project_id):
        raise HTTPException(status_code=404, detail="Application not found")
    return row


async def save_figures(
    db: AsyncSession,
    application_id: UUID,
    body: LoanApplicationFiguresIn,
    current_user: User,
) -> LoanApplication:
    """Replace the self-reported figures on a draft application.

    A full replace, not a merge: the wizard always sends the complete set, and
    merging would let a second request that omitted a field silently keep a
    stale value the applicant believed they had cleared.
    """
    if current_user.role != Role.SME.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only SMEs can save figures")

    row = await _owned_application(db, application_id, current_user)

    # Figures are underwriting inputs. Once the application is submitted they
    # are evidence of what was claimed, so they freeze with it -- otherwise an
    # applicant could restate their revenue after seeing the offer.
    if row.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Application is not in DRAFT status")

    row.self_reported_figures = body.model_dump()
    row.figures_updated_at = datetime.now(timezone.utc)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def submit_application(db: AsyncSession, application_id: UUID, current_user: User) -> LoanApplication:
    if current_user.role != Role.SME.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only SMEs can submit")
    result = await db.execute(select(LoanApplication).where(LoanApplication.id == application_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")
    if not await user_owns_project(db, current_user.id, row.project_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if row.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Application is not in DRAFT status")
    # The SME Service Agreement and the Standing Offer are signed after
    # approval, with the approved terms in them (app/loans/signing.py), not
    # with the application.
    row.status = "SUBMITTED"
    row.submitted_at = datetime.now(timezone.utc)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def public_rate_estimate(db: AsyncSession, body: PublicRateEstimateIn) -> PublicRateEstimateOut:
    """An indicative band for an anonymous landing-page visitor.

    No database, no account, no stored application -- the visitor's answers go
    straight into the engine and the band comes straight back. Nothing is
    persisted: this is a calculator, and keeping it stateless is what lets it be
    unauthenticated without becoming a way to write rows.

    The engine is called SERVER-side deliberately. The bank reference rate and
    the rating formula are internal pricing inputs and never leave the server;
    the browser receives a rate band, a score band and the assumptions behind
    them, and nothing it could reconstruct the pricing from.
    """
    from app.rates.bank_rate import bank_rate_in_force

    # Read-only: the rate in force, the same one a score run is priced on.
    bank_rate_pct = await bank_rate_in_force(db)
    try:
        band = grade_lite(
            bank_rate_pct=bank_rate_pct,
            # No company exists yet, so there is no tax id to carry. The engine
            # never reads this field -- it is a label on GradingInput.
            company_code="public-estimate",
            industry=body.industry,
            employee_count=body.employee_count,
            incorporation_date_months=body.operating_months,
            loan_size_vnd=Decimal(body.loan_amount),
            duration_months=body.duration_months,
            figures={
                "revenue_last_12m": body.revenue_last_12m,
                "revenue_prior_12m": body.revenue_prior_12m,
                "revenue_best_month": body.revenue_best_month,
                "revenue_worst_month": body.revenue_worst_month,
                "cogs_y1": body.cogs_y1,
                "fixed_cost_y1": body.fixed_cost_y1,
                "variable_cost_excl_cogs_y1": body.variable_cost_excl_cogs_y1,
                "owner_withdrawal_pct": None,
                "conc_top1_pct": body.conc_top1_pct,
                "conc_top3_pct": body.conc_top3_pct,
            },
        )
    except LiteGradingError as exc:
        # A real answer about the visitor's numbers -- costs above revenue, an
        # unscoreable industry, a headcount outside every band.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        # grade()'s own input validation: loan size or term outside what the
        # engine accepts. Bounded in PublicRateEstimateIn, so reaching here
        # means a bound drifted -- still a 422, never a 500 on a public page.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return PublicRateEstimateOut(
        rate_low_pct=band.rate_low_pct,
        rate_high_pct=band.rate_high_pct,
        score_low=band.grade_low,
        score_high=band.grade_high,
        engine_version=band.engine_version,
        provisional=band.provisional,
        assumptions=list(band.assumptions),
        assumption_codes=list(band.assumption_codes),
    )
