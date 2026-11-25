"""Platform-wide funding analytics for the admin overview (``GET /admin/funding/analytics``).

Every account at once: what investors have put into businesses, and what businesses have repaid them. FundLok holds
none of this money (ADR-009): each figure is a transfer between the parties' own bank accounts, as recorded on the
tickets and their repayment days. No names or accounts leave here, only totals.

The groups are the ones the investor and business screens use (``app.market.portfolio``): an investment counts once
the business has received it (CONFIRMED or ACTIVE), and a repayment day counts as paid once the business marked it
paid (PRESUMED_PAID) or FundLok matched its receipt (MATCHED). A day is late on the late-repayments list's rule
(``app.admin.repayments.is_overdue``): not marked paid, and its due date has passed. A "day" is one due date of one
ticket, as everywhere else: the first instalment and the first-period interest are two rows sharing one due date.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, case, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import ICT
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import FundingWindow, RepaymentDay, Tranche
from app.market.portfolio import PAID, RECEIVED

# Reserved or signed (the investor's transfer is still to come), or sent and waiting for FundLok's check.
IN_PROGRESS = ("RESERVED", "SIGNED", "ATTESTED", "UNDER_REVIEW")
TREND_MONTHS = 12


def trend_months(today: dt.date) -> list[str]:
    """The last TREND_MONTHS months as "YYYY-MM", oldest first, ending with today's."""
    year, month, out = today.year, today.month, []
    for _ in range(TREND_MONTHS):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out[::-1]


async def funding_analytics(db: AsyncSession, *, today: dt.date | None = None) -> dict:
    today = today or ict_date(dt.datetime.now(dt.timezone.utc))
    months = trend_months(today)
    first_year, first_month = (int(part) for part in months[0].split("-"))
    trend_start = dt.date(first_year, first_month, 1)

    # --- investments --------------------------------------------------------------------------------------------
    invested_vnd, investments, investors, businesses = (
        await db.execute(
            select(
                func.coalesce(func.sum(Tranche.amount_vnd), 0),
                func.count(Tranche.id),
                func.count(func.distinct(Tranche.investor_id)),
                func.count(func.distinct(FundingWindow.sme_user_id)),
            )
            .join(FundingWindow, FundingWindow.id == Tranche.window_id)
            .where(Tranche.status.in_(RECEIVED))
        )
    ).one()
    in_progress_vnd, in_progress = (
        await db.execute(
            select(func.coalesce(func.sum(Tranche.amount_vnd), 0), func.count(Tranche.id)).where(
                Tranche.status.in_(IN_PROGRESS)
            )
        )
    ).one()
    invested_by_month = dict.fromkeys(months, 0)
    received_rows = await db.execute(
        select(Tranche.confirmed_at, Tranche.amount_vnd).where(
            Tranche.status.in_(RECEIVED),
            Tranche.confirmed_at >= dt.datetime.combine(trend_start, dt.time(), tzinfo=ICT),
        )
    )
    for confirmed_at, amount in received_rows:
        month = ict_date(confirmed_at).strftime("%Y-%m")
        if month in invested_by_month:
            invested_by_month[month] += amount

    # --- repayments ---------------------------------------------------------------------------------------------
    paid = RepaymentDay.status.in_(PAID)
    past_due = RepaymentDay.due_date < today
    late = and_(RepaymentDay.status.in_(("DUE", "LATE")), RepaymentDay.settled_at.is_(None), past_due)
    on_time = and_(paid, past_due, RepaymentDay.late_marked_at.is_(None))
    a_day = tuple_(RepaymentDay.tranche_id, RepaymentDay.due_date)
    repaid_vnd, principal_repaid_vnd, days_due, days_on_time, late_days, late_tickets = (
        await db.execute(
            select(
                func.coalesce(func.sum(case((paid, RepaymentDay.gross_vnd), else_=0)), 0),
                func.coalesce(func.sum(case((paid, RepaymentDay.principal_vnd), else_=0)), 0),
                func.count(func.distinct(a_day)).filter(past_due),
                func.count(func.distinct(a_day)).filter(on_time),
                func.count(func.distinct(a_day)).filter(late),
                func.count(func.distinct(RepaymentDay.tranche_id)).filter(late),
            )
            .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
            .where(Tranche.status.in_(RECEIVED))
        )
    ).one()
    month_of = func.to_char(RepaymentDay.due_date, "YYYY-MM")
    repaid_by_month = dict.fromkeys(months, 0)
    for month, amount in await db.execute(
        select(month_of, func.sum(RepaymentDay.gross_vnd))
        .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
        .where(Tranche.status.in_(RECEIVED), paid, RepaymentDay.due_date >= trend_start, RepaymentDay.due_date <= today)
        .group_by(month_of)
    ):
        if month in repaid_by_month:
            repaid_by_month[month] = int(amount)

    return {
        "as_of": today,
        "investments": {
            "invested_vnd": int(invested_vnd),
            "investments": investments,
            "investors": investors,
            "businesses": businesses,
            "in_progress_vnd": int(in_progress_vnd),
            "in_progress": in_progress,
            "monthly": [{"month": m, "amount_vnd": v} for m, v in invested_by_month.items()],
        },
        "repayments": {
            "repaid_vnd": int(repaid_vnd),
            "principal_repaid_vnd": int(principal_repaid_vnd),
            # Principal the businesses have received and not repaid yet. Nothing is written down in the MVP.
            "outstanding_principal_vnd": int(invested_vnd) - int(principal_repaid_vnd),
            "days_due": days_due,
            "days_on_time": days_on_time,
            "late_days": late_days,
            "late_tickets": late_tickets,
            "monthly": [{"month": m, "amount_vnd": v} for m, v in repaid_by_month.items()],
        },
    }
