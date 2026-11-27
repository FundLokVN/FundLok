"""The direct-transfer money timers Cloud Scheduler runs (no worker is deployed).

The repayment job commits settlements by round. All jobs are idempotent on retry.
Spec: docs/specs/scheduled_jobs/cloud-scheduler-jobs.md (Money timers).
"""

import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import cic, disputes, fees, repayment
from app.direct_transfer.calendar_utils import default_calendar, ict_date


logger = logging.getLogger(__name__)


async def run_repayment_day(db: AsyncSession, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Days with no payment proof by 18:00 ICT on their due date become LATE; days with proof settle once the
    investor's 24 hours pass with no report; a dispute whose required party missed its deadline passes to staff."""
    now = now or dt.datetime.now(dt.timezone.utc)
    late = await repayment.mark_late_days(db, now=now)
    await db.commit()
    settled = failed = 0
    for window_id in await repayment.due_settlement_windows(db, now=now):
        try:
            count = await repayment.settle_days(db, now=now, window_id=window_id)
            await db.commit()
            settled += count
        except Exception:
            await db.rollback()
            logger.exception("repayment-day: settlement failed for round %s", window_id)
            failed += 1
    to_staff = await disputes.advance_dispute_deadlines(db, now=now)
    await db.commit()
    return {"late": late, "settled": settled, "disputes": to_staff, "failed": failed}


async def run_invoices(db: AsyncSession, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Mark invoices overdue and accrue investor fees for ended months."""
    now = now or dt.datetime.now(dt.timezone.utc)
    overdue = await fees.mark_overdue_invoices(db, now=now)
    accrued = await fees.accrue_investor_fees(db, now=now)
    # Investor invoicing resumes when lenders can see and pay their invoices.
    await db.commit()
    return {"overdue": overdue, "accrued": accrued, "invoiced": 0}


async def run_cic_daily_balance(db: AsyncSession, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Log the previous business day's outstanding balances for the CIC event log. Nothing the morning after a
    non-business day."""
    now = now or dt.datetime.now(dt.timezone.utc)
    yesterday = ict_date(now) - dt.timedelta(days=1)
    if not default_calendar().is_business_day(yesterday):
        return {"events": 0}
    events = await cic.emit_daily_balances(db, on_date=yesterday, now=now)
    await db.commit()
    return {"events": events}
