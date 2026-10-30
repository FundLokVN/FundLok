"""ARQ job functions for the direct-transfer timers (HANDOFF-04 B13).

Every job is a thin wrapper over an idempotent service function that takes an injected clock (never `sleep`,
never `BackgroundTasks` for financial operations). The ARQ context may carry:
    ctx["session_factory"]  -- async_sessionmaker (defaults to app.core.database.SessionLocal)
    ctx["clock"]            -- () -> aware datetime (defaults to now in UTC)
so tests drive time and the database without touching the worker. Each job commits once; a failure rolls the
whole unit back and ARQ retries it.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.core.timezone import ICT
from app.direct_transfer import cic, close, disputes, fees, repayment, tranches
from app.direct_transfer.calendar_utils import default_calendar, ict_date

logger = logging.getLogger("app.direct_transfer.jobs")


def _factory(ctx: dict[str, Any]):
    factory = ctx.get("session_factory")
    if factory is None:
        from app.core.database import SessionLocal

        factory = SessionLocal
    return factory


def _now(ctx: dict[str, Any]) -> dt.datetime:
    clock = ctx.get("clock")
    return clock() if clock else dt.datetime.now(dt.timezone.utc)


async def expire_holds_job(ctx: dict[str, Any]) -> int:
    """Ticket hold (2 h): RESERVED/SIGNED tranches with no receipt expire and free capacity."""
    async with _factory(ctx)() as db:
        n = await tranches.expire_holds(db, now=_now(ctx))
        await db.commit()
        return n


async def advance_windows_job(ctx: dict[str, Any]) -> int:
    """SME dispute window lapse -> CONFIRMED, window close triggers (full / day 14 / wait over), dispute deadlines."""
    async with _factory(ctx)() as db:
        now = _now(ctx)
        await tranches.confirm_lapsed_tranches(db, now=now)
        await disputes.advance_dispute_deadlines(db, now=now)
        n = await close.advance_all_windows(db, now=now)
        await db.commit()
        return n


async def repayment_day_job(ctx: dict[str, Any]) -> dict[str, int]:
    """Daily: presume unreported days paid, settle days past the missed-day report deadline."""
    async with _factory(ctx)() as db:
        now = _now(ctx)
        presumed = await repayment.presume_paid_days(db, now=now)
        settled = await repayment.settle_days(db, now=now)
        await db.commit()
        return {"presumed": presumed, "settled": settled}


async def invoice_job(ctx: dict[str, Any]) -> dict[str, int]:
    """Daily: mark invoices overdue (and assess the late fee), accrue investor fees for ended months, invoice them."""
    async with _factory(ctx)() as db:
        now = _now(ctx)
        overdue = await fees.mark_overdue_invoices(db, now=now)
        accrued = await fees.accrue_investor_fees(db, now=now)
        invoices = await fees.issue_investor_invoices(db, now=now)
        await db.commit()
        return {"overdue": overdue, "accrued": accrued, "invoiced": len(invoices)}


async def cic_daily_balance_job(ctx: dict[str, Any]) -> int:
    """Daily (shortly after midnight ICT): log the previous business day's outstanding balances. No sender."""
    async with _factory(ctx)() as db:
        now = _now(ctx)
        yesterday = ict_date(now) - dt.timedelta(days=1)
        if not default_calendar().is_business_day(yesterday):
            return 0
        n = await cic.emit_daily_balances(db, on_date=yesterday, now=now)
        await db.commit()
        return n


ALL_JOBS = [expire_holds_job, advance_windows_job, repayment_day_job, invoice_job, cic_daily_balance_job]
