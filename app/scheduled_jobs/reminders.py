"""The daily repayment reminder: one email per SME each working day at 15:00 ICT.

The SME repays each investor directly, every working day, one VietQR per
investor (ADR-009). The reminder says what is due today and what earlier days
are still not marked paid, and sends them to the dashboard, where each
payment's VietQR and the "mark paid" button are. It reads exactly what the
dashboard shows (Edward's `repayment.todays_payments`), keeping only days still
DUE: a day the SME already marked paid is not their business any more.

Cloud Scheduler calls it daily at 15:00 ICT (app/scheduled_jobs/router.py). On a
day that is not a working day (weekend, VN public holiday) nothing is sent.
Lateness is out of MVP scope: the email never says late or overdue, only
"not yet marked paid".
"""

import dataclasses
import datetime as dt
import html
import logging
from uuid import UUID

from sqlalchemy import distinct, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import system_email
from app.admin.internal_email.schemas import EmailContent
from app.core.config import settings
from app.direct_transfer.calendar_utils import default_calendar, ict_date
from app.direct_transfer.models import WINDOW_LIVE_STATES, FundingWindow
from app.direct_transfer.repayment import todays_payments
from app.repayment_schedule.calendar import BusinessDayCalendar
from app.scheduled_jobs.models import RepaymentReminder
from app.users.models import USER_STATUS_SUSPENDED, User

logger = logging.getLogger(__name__)

STATUS_SENDING = "SENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
# Tries per SME per day; the scheduler's own retries bring the later ones.
MAX_ATTEMPTS = 3
# The rounds todays_payments looks at: with repay_from_approval an investment
# repays while its round is still open or closing.
_REPAYING_ROUNDS = WINDOW_LIVE_STATES


@dataclasses.dataclass(frozen=True)
class Summary:
    due_count: int
    due_vnd: int
    earlier_count: int
    earlier_vnd: int


def summarise(payments: dict, today: dt.date) -> Summary | None:
    """What to remind about from todays_payments' answer, or None when every
    day is marked paid already. Amounts are what the SME transfers (net of
    the tax it withholds), as on the dashboard."""
    due = [row for row in payments["rows"] if row["status"] == "DUE"]
    if not due:
        return None
    today_rows = [row for row in due if row["due_date"] == today]
    earlier = [row for row in due if row["due_date"] < today]
    return Summary(
        due_count=len(today_rows),
        due_vnd=sum(row["net_vnd"] for row in today_rows),
        earlier_count=len(earlier),
        earlier_vnd=sum(row["net_vnd"] for row in earlier),
    )


def _vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + " VND"


def compose(summary: Summary, *, sme: User, today: dt.date) -> EmailContent:
    """The reminder, in Vietnamese with the English below it."""
    name = html.escape(sme.full_name or sme.email)
    day = today.strftime("%d/%m/%Y")
    vi: list[str] = [f"<p>Xin chào {name},</p>"]
    en: list[str] = []
    if summary.due_count:
        vi.append(
            f"<p>Hôm nay, {day}, doanh nghiệp có <b>{summary.due_count} khoản trả</b> cho nhà đầu tư, "
            f"tổng số tiền cần chuyển là <b>{_vnd(summary.due_vnd)}</b>.</p>"
        )
        en.append(
            f"Today, {day}, you have {summary.due_count} repayments to your investors, "
            f"{_vnd(summary.due_vnd)} to transfer in total."
        )
    else:
        vi.append(f"<p>Hôm nay, {day}, không có khoản trả mới đến hạn.</p>")
        en.append(f"No new repayment is due today, {day}.")
    if summary.earlier_count:
        vi.append(
            f"<p>Còn <b>{summary.earlier_count} khoản</b> của các ngày trước chưa được đánh dấu đã trả, "
            f"tổng <b>{_vnd(summary.earlier_vnd)}</b>. Nếu bạn đã chuyển rồi, hãy đánh dấu đã trả trên FundLok.</p>"
        )
        en.append(
            f"{summary.earlier_count} payments from earlier days are not marked paid yet "
            f"({_vnd(summary.earlier_vnd)}). If you have already sent them, mark them paid on FundLok."
        )
    vi.append(
        "<p>Bạn chuyển trực tiếp cho từng nhà đầu tư. Mở trang tổng quan FundLok để xem mã VietQR "
        "của từng khoản, rồi đánh dấu đã trả.</p>"
    )
    en.append(
        "You pay each investor directly: open your FundLok dashboard for each payment's VietQR, "
        "then mark it paid."
    )
    body = "".join(vi) + "<p><i>" + " ".join(en) + "</i></p>"

    dashboard = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    https = dashboard.startswith("https://")
    return EmailContent(
        template="action_required",
        subject=f"FundLok: lịch trả nợ ngày {day} / repayments for {day}",
        heading="Nhắc lịch trả nợ · Repayment reminder",
        body=body,
        # The composer's templates take an https link only (local dev runs on http).
        button_label="Mở FundLok · Open FundLok" if https else None,
        button_url=dashboard if https else None,
    )


async def _claim(
    db: AsyncSession,
    sme_id: UUID,
    today: dt.date,
    *,
    sent_to: str,
    summary: Summary,
    retry: bool,
) -> bool:
    """Take today's reminder for this SME, committed before anything is
    sent. False when another run took it first."""
    values = {
        "status": STATUS_SENDING,
        "sent_to": sent_to,
        "due_count": summary.due_count,
        "due_vnd": summary.due_vnd,
        "earlier_count": summary.earlier_count,
    }
    if retry:
        stmt = (
            update(RepaymentReminder)
            .where(
                RepaymentReminder.sme_user_id == sme_id,
                RepaymentReminder.reminder_date == today,
                RepaymentReminder.status == STATUS_FAILED,
                RepaymentReminder.attempts < MAX_ATTEMPTS,
            )
            .values(**values, attempts=RepaymentReminder.attempts + 1, last_error=None)
            .returning(RepaymentReminder.sme_user_id)
        )
    else:
        stmt = (
            insert(RepaymentReminder)
            .values(sme_user_id=sme_id, reminder_date=today, attempts=1, **values)
            .on_conflict_do_nothing(index_elements=["sme_user_id", "reminder_date"])
            .returning(RepaymentReminder.sme_user_id)
        )
    claimed = (await db.execute(stmt)).scalar_one_or_none() is not None
    await db.commit()
    return claimed


async def _finish(db: AsyncSession, sme_id: UUID, today: dt.date, **values) -> None:
    await db.execute(
        update(RepaymentReminder)
        .where(RepaymentReminder.sme_user_id == sme_id, RepaymentReminder.reminder_date == today)
        .values(**values)
    )
    await db.commit()


async def send_repayment_reminders(
    db: AsyncSession,
    *,
    now: dt.datetime | None = None,
    calendar: BusinessDayCalendar | None = None,
) -> dict:
    """Remind every SME with a day still DUE (today or earlier). Returns
    {"sent", "failed", "skipped"}; skipped names why nothing ran."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = ict_date(now)
    if not (calendar or default_calendar()).is_business_day(today):
        return {"sent": 0, "failed": 0, "skipped": "NOT_A_WORKING_DAY"}

    sme_ids = (
        await db.execute(
            select(distinct(FundingWindow.sme_user_id)).where(
                FundingWindow.status.in_(_REPAYING_ROUNDS)
            )
        )
    ).scalars().all()
    sent = failed = 0
    for sme_id in sme_ids:
        sme = await db.get(User, sme_id, populate_existing=True)
        if sme is None or not sme.email or sme.status == USER_STATUS_SUSPENDED:
            continue
        existing = await db.get(RepaymentReminder, (sme_id, today), populate_existing=True)
        if existing is not None and not (
            existing.status == STATUS_FAILED and existing.attempts < MAX_ATTEMPTS
        ):
            continue
        summary = summarise(await todays_payments(db, sme_user_id=sme_id, on_date=today), today)
        if summary is None:
            continue
        to = sme.email
        if not await _claim(
            db, sme_id, today, sent_to=to, summary=summary, retry=existing is not None
        ):
            continue
        try:
            content = compose(summary, sme=sme, today=today)
            channel, from_address = await system_email.deliver(
                db,
                to=to,
                content=content,
                record={"repayment_reminder_date": today.isoformat(), "heading": content.heading},
                label=f"Repayment reminder {sme_id}",
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the row, retried next run
            await db.rollback()
            code = exc.code if isinstance(exc, system_email.NotSent) else type(exc).__name__
            logger.warning("Repayment reminder %s: not sent (%s)", sme_id, code)
            await _finish(db, sme_id, today, status=STATUS_FAILED, last_error=code[:100])
            failed += 1
            continue
        await _finish(
            db,
            sme_id,
            today,
            status=STATUS_SENT,
            channel=channel,
            from_address=from_address,
            sent_at=dt.datetime.now(dt.timezone.utc),
        )
        sent += 1
    return {"sent": sent, "failed": failed, "skipped": None}
