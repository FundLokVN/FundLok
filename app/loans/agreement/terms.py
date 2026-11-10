"""The Listing Terms an approved request is signed and published on.

Both documents the SME signs after the assessment carry them (the Service Agreement's Annex and the Standing
Offer's Listing details), and the funding window later opens on the same three numbers
(app/direct_transfer/windows.open_funding_window): the requested amount, the LOCKED score run's rate (to 0.001,
as the window stores it) and the requested term. Until FundLok has approved the request and its score run is
LOCKED there are no terms to sign.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.config import resolve
from app.lending.models import LoanApplication, ScoreRun
from app.repayment_schedule.origination import compute_origination


@dataclass(frozen=True)
class RepaymentEstimate:
    """What the loan costs on its contractual term, by the repayment schedule's own rules
    (repayment_schedule.origination.compute_origination, spec §4.1): flat interest on the principal rounded to the
    đồng, N0 = working days per month × M, a daily instalment of floor(T0 / N0) with the remainder on the last
    day, and the backstop N_bs. The working days per month are the funding parameters' 21, Contract 3's own
    "N0 = 21 × M", so what the SME signs is what the schedule will ask for."""

    principal_vnd: int
    interest_vnd: int
    total_vnd: int  # T0 = P + I
    instalment_days: int  # N0
    daily_vnd: int  # A0
    final_instalment_vnd: int  # A0 + remainder
    backstop_day: int  # N_bs


def repayment_estimate(amount_vnd: int, rate_pct: Decimal, term_months: int) -> RepaymentEstimate:
    result = compute_origination(
        principal_vnd=int(amount_vnd),
        annual_rate_pct=Decimal(rate_pct),
        contractual_term_months=int(term_months),
        working_days_per_month=resolve(None).working_days_per_month,
    )
    return RepaymentEstimate(
        principal_vnd=result.principal_vnd,
        interest_vnd=result.interest_vnd,
        total_vnd=result.total_obligation_vnd,
        instalment_days=result.business_day_count,
        daily_vnd=result.daily_amount_vnd,
        final_instalment_vnd=result.final_instalment_vnd,
        backstop_day=result.backstop_day_index,
    )


@dataclass(frozen=True)
class ListingTerms:
    amount_vnd: int
    rate_pct: Decimal
    term_months: int
    score_run_id: UUID
    # FundLok's assessment the terms rest on: the business score (0-100) and when the run was locked.
    business_score: float | None = None
    assessed_at: dt.datetime | None = None

    @property
    def estimate(self) -> RepaymentEstimate:
        return repayment_estimate(self.amount_vnd, self.rate_pct, self.term_months)


async def locked_terms(db: AsyncSession, application: LoanApplication) -> ListingTerms | None:
    """The approved request's terms, or None while there are none to sign: not approved by FundLok (or rejected),
    no LOCKED score run with a rate yet, or no amount or term on the request."""
    if application.admin_approval != "APPROVED" or application.status == "REJECTED":
        return None
    if application.requested_amount is None or not application.duration_months:
        return None
    run = (
        await db.execute(
            select(ScoreRun)
            .where(
                ScoreRun.application_id == application.id,
                ScoreRun.status == "LOCKED",
                ScoreRun.interest_rate_pct.is_not(None),
            )
            .order_by(ScoreRun.locked_at.desc().nulls_last(), ScoreRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    return ListingTerms(
        amount_vnd=int(application.requested_amount),
        rate_pct=Decimal(str(run.interest_rate_pct)).quantize(Decimal("0.001")),
        term_months=int(application.duration_months),
        score_run_id=run.id,
        business_score=float(run.final_grade_precise) if run.final_grade_precise is not None else None,
        assessed_at=run.locked_at,
    )
