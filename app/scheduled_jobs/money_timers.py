"""The direct-transfer money timers Cloud Scheduler runs (no ARQ worker is deployed).

Each is the same unit of work as the ARQ job of the same purpose in `app/direct_transfer/jobs.py`
(`repayment_day_job`, `invoice_job`), on the request's session and committed once: a failure rolls the whole call
back and Cloud Scheduler retries. Idempotent. Spec: docs/specs/scheduled_jobs/cloud-scheduler-jobs.md (Money timers).
"""

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import fees, repayment


async def run_repayment_day(db: AsyncSession, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Days with no payment proof by 18:00 ICT on their due date become LATE; days with proof settle once the
    investor's 24 hours pass with no report."""
    now = now or dt.datetime.now(dt.timezone.utc)
    late = await repayment.mark_late_days(db, now=now)
    settled = await repayment.settle_days(db, now=now)
    await db.commit()
    return {"late": late, "settled": settled}


async def run_invoices(db: AsyncSession, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Mark invoices overdue (and assess the late fee), accrue investor fees for ended months, invoice them."""
    now = now or dt.datetime.now(dt.timezone.utc)
    overdue = await fees.mark_overdue_invoices(db, now=now)
    accrued = await fees.accrue_investor_fees(db, now=now)
    invoices = await fees.issue_investor_invoices(db, now=now)
    await db.commit()
    return {"overdue": overdue, "accrued": accrued, "invoiced": len(invoices)}
