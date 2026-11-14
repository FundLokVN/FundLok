"""The daily Zalo repayment reminder: one ZBS template message to the SME for
each investor ticket due today that it has not marked paid.

Runs with the 15:00 ICT email reminder (app/scheduled_jobs/router.py,
`/internal/jobs/repayment-reminders`), not on its own schedule, and never for
service fees. The template (`fundlok_reminder`, its id in
ZALO_ZBS_REMINDER_TEMPLATE_ID) names one investment code, so one message covers
one investor ticket:

    company_name    the business (the project's legal name)
    reference_code  the ticket's memo
    due_date        today, dd/mm/yyyy
    amount_due      what the SME transfers that investor today (net of the tax
                    it withholds), digits only: valid whether the template
                    types it as text or as a number
    days_left       the ticket's repayment days after today

A ticket is reminded only while a day due today is still DUE: uploading the
receipt marks it PRESUMED_PAID, so an SME that paid before 15:00 gets nothing.
Earlier unpaid days are not messaged (the template says "due today"); staff see
those in red on Admin > Transactions > Repayments.

Each message costs money, so like the email it is claimed (a row written
SENDING and committed) before it is sent: a doubled or retried scheduler call
never sends it twice. A FAILED row is retried by a later run the same day.
"""

import dataclasses
import datetime as dt
import logging
from uuid import UUID

from sqlalchemy import distinct, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.direct_transfer.calendar_utils import default_calendar, ict_date
from app.direct_transfer.models import FundingWindow, RepaymentDay, Tranche
from app.lending.models import Project
from app.phone_verification import zalo
from app.phone_verification.phone import canonical_vn_mobile, zalo_number
from app.repayment_schedule.calendar import BusinessDayCalendar
from app.scheduled_jobs.models import RepaymentZaloReminder
from app.scheduled_jobs.reminders import (
    _REPAYING_ROUNDS,
    MAX_ATTEMPTS,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_SENT,
)
from app.users.models import USER_STATUS_SUSPENDED, User

logger = logging.getLogger(__name__)

# The template's "Tên sản phẩm / Thương hiệu" parameter takes at most 200 characters.
_COMPANY_MAX = 200


@dataclasses.dataclass(frozen=True)
class DueTicket:
    tranche_id: UUID
    sme_user_id: UUID
    company_name: str
    reference_code: str
    amount_vnd: int
    days_left: int


def template_data(ticket: DueTicket, today: dt.date) -> dict[str, str]:
    """The `fundlok_reminder` template's parameters, every value a string as Zalo takes them."""
    return {
        "company_name": ticket.company_name[:_COMPANY_MAX],
        "reference_code": ticket.reference_code,
        "due_date": today.strftime("%d/%m/%Y"),
        "amount_due": str(ticket.amount_vnd),
        "days_left": str(ticket.days_left),
    }


async def due_today(db: AsyncSession, today: dt.date) -> list[DueTicket]:
    """Every investor ticket with a day due today still DUE (no receipt), in a round the SME is repaying."""
    rows = (
        await db.execute(
            select(RepaymentDay, Tranche, FundingWindow)
            .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
            .join(FundingWindow, FundingWindow.id == Tranche.window_id)
            .where(
                RepaymentDay.due_date == today,
                RepaymentDay.status == "DUE",
                RepaymentDay.settled_at.is_(None),
                FundingWindow.status.in_(_REPAYING_ROUNDS),
            )
            .order_by(FundingWindow.sme_user_id, Tranche.confirmed_at, Tranche.id)
        )
    ).all()
    amounts: dict[UUID, int] = {}
    tickets: dict[UUID, tuple[Tranche, FundingWindow]] = {}
    for day, tranche, window in rows:
        amounts[tranche.id] = amounts.get(tranche.id, 0) + int(day.net_vnd)
        tickets.setdefault(tranche.id, (tranche, window))

    out: list[DueTicket] = []
    for tranche_id, (tranche, window) in tickets.items():
        if amounts[tranche_id] <= 0:
            continue
        days_left = (
            await db.execute(
                select(func.count(distinct(RepaymentDay.due_date))).where(
                    RepaymentDay.tranche_id == tranche_id, RepaymentDay.due_date > today
                )
            )
        ).scalar_one()
        project = await db.get(Project, window.project_id)
        sme = await db.get(User, window.sme_user_id)
        out.append(
            DueTicket(
                tranche_id=tranche_id,
                sme_user_id=window.sme_user_id,
                company_name=(project.legal_name if project else None) or sme.full_name or "",
                reference_code=tranche.reference_code,
                amount_vnd=amounts[tranche_id],
                days_left=int(days_left),
            )
        )
    return out


def _phone(sme: User | None) -> str | None:
    """The SME's Zalo number, only once it has proven the phone with a code: an unproven number may be someone
    else's, and the message names an investment and an amount."""
    if sme is None or sme.status == USER_STATUS_SUSPENDED or not sme.phone or sme.phone_verified_at is None:
        return None
    canonical = canonical_vn_mobile(sme.phone)
    return zalo_number(canonical) if canonical else None


async def _claim(db: AsyncSession, ticket: DueTicket, today: dt.date, *, to: str, retry: bool) -> bool:
    """Take today's message for this ticket, committed before it is sent. False when another run took it."""
    values = {"status": STATUS_SENDING, "sent_to": to, "amount_vnd": ticket.amount_vnd, "days_left": ticket.days_left}
    if retry:
        stmt = (
            update(RepaymentZaloReminder)
            .where(
                RepaymentZaloReminder.tranche_id == ticket.tranche_id,
                RepaymentZaloReminder.reminder_date == today,
                RepaymentZaloReminder.status == STATUS_FAILED,
                RepaymentZaloReminder.attempts < MAX_ATTEMPTS,
            )
            .values(**values, attempts=RepaymentZaloReminder.attempts + 1, last_error=None)
            .returning(RepaymentZaloReminder.tranche_id)
        )
    else:
        stmt = (
            insert(RepaymentZaloReminder)
            .values(
                tranche_id=ticket.tranche_id, reminder_date=today, sme_user_id=ticket.sme_user_id, attempts=1, **values
            )
            .on_conflict_do_nothing(index_elements=["tranche_id", "reminder_date"])
            .returning(RepaymentZaloReminder.tranche_id)
        )
    claimed = (await db.execute(stmt)).scalar_one_or_none() is not None
    await db.commit()
    return claimed


async def _finish(db: AsyncSession, tranche_id: UUID, today: dt.date, **values) -> None:
    await db.execute(
        update(RepaymentZaloReminder)
        .where(RepaymentZaloReminder.tranche_id == tranche_id, RepaymentZaloReminder.reminder_date == today)
        .values(**values)
    )
    await db.commit()


def _error_code(exc: Exception) -> str:
    if isinstance(exc, zalo.ZaloNotConfigured):
        return "ZALO_NOT_CONFIGURED"
    if isinstance(exc, zalo.ZaloError):
        return f"ZALO_{exc.code}" if exc.code is not None else "ZALO_UNREACHABLE"
    return type(exc).__name__


async def send_zalo_reminders(
    db: AsyncSession,
    *,
    now: dt.datetime | None = None,
    calendar: BusinessDayCalendar | None = None,
) -> dict:
    """Message the SME for each ticket due today that it has not marked paid. Returns {"sent", "failed",
    "skipped"}; skipped names why nothing ran."""
    template_id = settings.ZALO_ZBS_REMINDER_TEMPLATE_ID
    if not template_id:
        return {"sent": 0, "failed": 0, "skipped": "NOT_CONFIGURED"}
    now = now or dt.datetime.now(dt.timezone.utc)
    today = ict_date(now)
    if not (calendar or default_calendar()).is_business_day(today):
        return {"sent": 0, "failed": 0, "skipped": "NOT_A_WORKING_DAY"}

    sent = failed = 0
    for ticket in await due_today(db, today):
        to = _phone(await db.get(User, ticket.sme_user_id, populate_existing=True))
        if to is None:
            continue
        existing = await db.get(RepaymentZaloReminder, (ticket.tranche_id, today), populate_existing=True)
        if existing is not None and not (existing.status == STATUS_FAILED and existing.attempts < MAX_ATTEMPTS):
            continue
        if not await _claim(db, ticket, today, to=to, retry=existing is not None):
            continue
        try:
            await zalo.send_template(
                to,
                template_id,
                template_data(ticket, today),
                # 32 characters, like the OTP's: the date and the ticket, for matching Zalo's delivery reports.
                tracking_id=f"rr{today:%y%m%d}{ticket.tranche_id.hex[:24]}",
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the row, retried next run
            await db.rollback()
            code = _error_code(exc)
            logger.warning("Zalo repayment reminder %s: not sent (%s)", ticket.tranche_id, code)
            await _finish(db, ticket.tranche_id, today, status=STATUS_FAILED, last_error=code[:100])
            failed += 1
            if isinstance(exc, zalo.ZaloNotConfigured):
                break  # every other send would fail the same way
            continue
        await _finish(db, ticket.tranche_id, today, status=STATUS_SENT, sent_at=dt.datetime.now(dt.timezone.utc))
        sent += 1
    return {"sent": sent, "failed": failed, "skipped": None}
