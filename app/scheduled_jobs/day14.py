"""Day-14 transfer rule jobs (docs/specs/direct_transfer/day14-transfer-rule.md rules 9-10).

Two Cloud Scheduler jobs on prod-api (app/scheduled_jobs/router.py):

* `day14-cutoff`, 15:00 and 15:35 ICT: runs Edward's `direct_transfer.day14.process_day14_window` per round. At
  15:00 unpaid and late lenders are locked; the 15:35 run, after the 15:30 receipt cut-off, decides: no-show, a top-up
  pledge for clean late money, or unlocking a lender whose receipt proved them in time. Tells each lender in the app.
  Idempotent per ticket.
* `day14-transfer-reminders`, 08:00 (DAY14_MORNING) and 14:00 (DAY14_AFTERNOON) ICT: every lender whose ticket on a
  round paying out today is still unpaid gets "sign and pay by 14:45" in the app, by email and (once the ZBS template
  is approved and ZALO_ZBS_DAY14_TEMPLATE_ID is set) on Zalo.

Both only touch rounds with Day-14 cut-offs (commit-then-accept windows, `pay_deadline_at` set); LEGACY rounds have
none. A reminder is claimed (a transfer_reminders row written SENDING and committed) before it is sent, so a doubled
scheduler call never sends it twice; a FAILED one is retried by a later run, up to MAX_ATTEMPTS.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import html
import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import system_email
from app.admin.internal_email.schemas import EmailContent
from app.core.config import settings
from app.core.timezone import ICT
from app.direct_transfer import day14
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import FundingWindow, Tranche, TransferReminder
from app.lending.models import Project
from app.notifications import realtime
from app.notifications.models import EVENT_LENDER_LOCKED, EVENT_TOP_UP_PLEDGE_RECORDED, EVENT_TRANSFER_DUE_TODAY
from app.notifications.service import create_notification
from app.phone_verification import zalo
from app.phone_verification.phone import canonical_vn_mobile, zalo_number
from app.users.models import USER_STATUS_SUSPENDED, User

logger = logging.getLogger(__name__)

KINDS = ("DAY14_MORNING", "DAY14_AFTERNOON")
STATUS_SENDING = "SENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
MAX_ATTEMPTS = 3
_COMPANY_MAX = 200  # the ZBS template's text parameter limit


# --------------------------------------------------------------------------- #
# 15:00 cut-off
# --------------------------------------------------------------------------- #
async def _titles(db: AsyncSession, window_ids: set[UUID]) -> dict[UUID, str | None]:
    if not window_ids:
        return {}
    rows = await db.execute(
        select(FundingWindow.id, Project.legal_name)
        .outerjoin(Project, Project.id == FundingWindow.project_id)
        .where(FundingWindow.id.in_(window_ids))
    )
    return {window_id: title for window_id, title in rows.all()}


async def run_day14_cutoff(db: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    """Lock no-shows and late money at 15:00; after the 15:30 receipt cut-off, decide (no-show, top-up pledge, or
    unlock). One window per transaction: a window that fails is rolled back, logged and left for the next run, and
    does not stop the others. Tells each lender in the app. Returns {"locked", "pledges", "released", "failed"} for
    this run (lenders newly locked, pledges recorded, lenders unlocked, windows that failed)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    totals = {"locked": 0, "pledges": 0, "released": 0, "failed": 0}
    for window_id in await day14.day14_windows_due(db, now=now):
        try:
            outcome = await day14.process_day14_window(db, window_id=window_id, now=now)
            titles = await _titles(db, {window_id})
            told = _notify(db, outcome, titles)
            await db.commit()
        except Exception:  # noqa: BLE001 - one window must not stop the others; the next run retries it
            await db.rollback()
            logger.exception("Day-14 cut-off: window %s failed", window_id)
            totals["failed"] += 1
            continue
        await realtime.publish(told)
        totals["locked"] += sum(1 for lock in outcome.locked if lock.newly_locked)
        totals["pledges"] += len(outcome.pledges)
        totals["released"] += len(outcome.released)
    return totals


def _notify(db: AsyncSession, outcome: day14.Day14Outcome, titles: dict[UUID, str | None]) -> set[UUID]:
    told: set[UUID] = set()
    for lock in outcome.locked:
        if not lock.newly_locked:
            continue
        create_notification(
            db, user_id=lock.investor_id, event=EVENT_LENDER_LOCKED, entity_type="TRANCHE", entity_id=lock.tranche_id,
            data={"window_id": str(lock.window_id), "listing_title": titles.get(lock.window_id), "reason": lock.reason},
        )
        told.add(lock.investor_id)
    for pledge in outcome.pledges:
        create_notification(
            db, user_id=pledge.investor_id, event=EVENT_TOP_UP_PLEDGE_RECORDED, entity_type="TRANCHE",
            entity_id=pledge.tranche_id,
            data={
                "pledge_id": str(pledge.pledge_id), "window_id": str(pledge.window_id),
                "listing_title": titles.get(pledge.window_id), "amount_vnd": pledge.amount_vnd,
            },
        )
        told.add(pledge.investor_id)
    return told


# --------------------------------------------------------------------------- #
# "sign and pay by 14:45"
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class DueTransfer:
    tranche_id: UUID
    investor_id: UUID
    listing_title: str
    reference_code: str
    amount_vnd: int
    pay_deadline_at: dt.datetime
    project_id: UUID | None


async def due_today(db: AsyncSession, now: dt.datetime) -> list[DueTransfer]:
    """Unpaid tickets (RESERVED or SIGNED, no receipt) on rounds whose Day 14 is today and still before 14:45."""
    today = ict_date(now)
    rows = (
        await db.execute(
            select(Tranche, FundingWindow, Project)
            .join(FundingWindow, FundingWindow.id == Tranche.window_id)
            .outerjoin(Project, Project.id == FundingWindow.project_id)
            .where(
                FundingWindow.pay_deadline_at.is_not(None),
                FundingWindow.pay_deadline_at > now,
                Tranche.status.in_(("RESERVED", "SIGNED")),
                Tranche.receipt_submitted_at.is_(None),
                Tranche.hold_expires_at > now,  # a lapsed hold can no longer get a QR
            )
            .order_by(Tranche.reserved_at, Tranche.id)
        )
    ).all()
    return [
        DueTransfer(
            tranche_id=tranche.id, investor_id=tranche.investor_id,
            listing_title=(project.legal_name if project else None) or "", reference_code=tranche.reference_code,
            amount_vnd=tranche.amount_vnd, pay_deadline_at=window.pay_deadline_at,
            project_id=project.id if project else None,
        )
        for tranche, window, project in rows
        if ict_date(window.pay_deadline_at) == today
    ]


def _vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + " ₫"


def _deadline(due: DueTransfer) -> str:
    return due.pay_deadline_at.astimezone(ICT).strftime("%H:%M")


def _invest_url(due: DueTransfer) -> str:
    base = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard/invest"
    return f"{base}?projectId={due.project_id}" if due.project_id else base


def compose_email(due: DueTransfer) -> EmailContent:
    deadline = _deadline(due)
    title = html.escape(due.listing_title or "FundLok")
    body = (
        f"<p>Hôm nay là ngày giải ngân của <b>{title}</b>. Hãy ký và chuyển khoản <b>{_vnd(due.amount_vnd)}</b> "
        f"trước <b>{deadline}</b>. Mỗi mã QR chỉ có hiệu lực 15 phút: mở FundLok để lấy mã QR, chuyển ngay, rồi tải "
        "lên biên lai. Tiền chuyển muộn sẽ không được hoàn lại mà trở thành khoản bổ sung, và tài khoản bị khóa lúc "
        "15:00.</p>"
        f"<p><i>Today is disbursement day for {title}. Sign and pay {_vnd(due.amount_vnd)} by {deadline}. Each QR is "
        "valid for 15 minutes: open FundLok for your QR, pay straight away, then upload your receipt. Money sent late "
        "is not returned: it becomes a top-up, and your account is locked at 15:00.</i></p>"
    )
    url = _invest_url(due)
    https = url.startswith("https://")
    return EmailContent(
        template="action_required",
        subject=f"FundLok: ký và chuyển khoản trước {deadline} / sign and pay by {deadline}",
        heading="Ngày giải ngân · Disbursement day",
        body=body,
        button_label="Mở FundLok · Open FundLok" if https else None,
        button_url=url if https else None,
    )


def zalo_template_data(due: DueTransfer) -> dict[str, str]:
    """Parameters of the investor Day-14 ZBS template (to register): every value a string, as Zalo takes them. No
    reference code on purpose: on Day 14 a transfer must carry a QR's own code (reference + 4 digits, valid 15
    minutes), so the message sends the investor to FundLok for the QR rather than handing them a memo."""
    return {
        "company_name": due.listing_title[:_COMPANY_MAX],
        "amount": str(due.amount_vnd),
        "deadline": due.pay_deadline_at.astimezone(ICT).strftime("%H:%M %d/%m/%Y"),
    }


def _zalo_phone(user: User | None) -> str | None:
    """Only a phone the investor proved with a code: the message names an investment and an amount."""
    if user is None or not user.phone or user.phone_verified_at is None:
        return None
    canonical = canonical_vn_mobile(user.phone)
    return zalo_number(canonical) if canonical else None


async def _claim(db: AsyncSession, due: DueTransfer, kind: str, channel: str, *, to: str | None, now: dt.datetime) -> bool:
    """Take this message, committed before it is sent. False when a run already sent it or holds it, or it failed
    MAX_ATTEMPTS times."""
    existing = await db.get(TransferReminder, (due.tranche_id, kind, channel), populate_existing=True)
    if existing is None:
        stmt = (
            insert(TransferReminder)
            .values(
                tranche_id=due.tranche_id, kind=kind, channel=channel, status=STATUS_SENDING, attempts=1, sent_to=to,
                claimed_at=now,
            )
            .on_conflict_do_nothing(index_elements=["tranche_id", "kind", "channel"])
            .returning(TransferReminder.tranche_id)
        )
    elif existing.status == STATUS_FAILED and existing.attempts < MAX_ATTEMPTS:
        stmt = (
            update(TransferReminder)
            .where(
                TransferReminder.tranche_id == due.tranche_id, TransferReminder.kind == kind,
                TransferReminder.channel == channel, TransferReminder.status == STATUS_FAILED,
                TransferReminder.attempts < MAX_ATTEMPTS,
            )
            .values(status=STATUS_SENDING, attempts=TransferReminder.attempts + 1, sent_to=to, last_error=None,
                    claimed_at=now)
            .returning(TransferReminder.tranche_id)
        )
    else:
        return False
    claimed = (await db.execute(stmt)).scalar_one_or_none() is not None
    await db.commit()
    return claimed


async def _finish(db: AsyncSession, due: DueTransfer, kind: str, channel: str, **values) -> None:
    await db.execute(
        update(TransferReminder)
        .where(
            TransferReminder.tranche_id == due.tranche_id, TransferReminder.kind == kind,
            TransferReminder.channel == channel,
        )
        .values(**values)
    )
    await db.commit()


def _error_code(exc: Exception) -> str:
    if isinstance(exc, system_email.NotSent):
        return exc.code
    if isinstance(exc, zalo.ZaloNotConfigured):
        return "ZALO_NOT_CONFIGURED"
    if isinstance(exc, zalo.ZaloError):
        return f"ZALO_{exc.code}" if exc.code is not None else "ZALO_UNREACHABLE"
    return type(exc).__name__


async def send_day14_reminders(db: AsyncSession, *, kind: str, now: dt.datetime | None = None) -> dict:
    """Remind every lender with an unpaid ticket on a round paying out today. Returns per channel
    {"in_app", "email", "zalo"}: {"sent", "failed", "skipped"}."""
    if kind not in KINDS:
        raise ValueError(f"unknown Day-14 reminder kind {kind!r}")
    now = now or dt.datetime.now(dt.timezone.utc)
    template_id = settings.ZALO_ZBS_DAY14_TEMPLATE_ID
    counts = {channel: {"sent": 0, "failed": 0, "skipped": None} for channel in ("in_app", "email", "zalo")}
    if not template_id:
        counts["zalo"]["skipped"] = "NOT_CONFIGURED"
    told: set[UUID] = set()
    zalo_down = False

    for due in await due_today(db, now):
        investor = await db.get(User, due.investor_id, populate_existing=True)
        if investor is None or investor.status == USER_STATUS_SUSPENDED:
            continue

        # In the app: the notification and its log row commit together.
        if await _claim(db, due, kind, "IN_APP", to=None, now=now):
            create_notification(
                db, user_id=due.investor_id, event=EVENT_TRANSFER_DUE_TODAY, entity_type="TRANCHE",
                entity_id=due.tranche_id,
                data={
                    "tranche_id": str(due.tranche_id), "listing_title": due.listing_title,
                    "amount_vnd": due.amount_vnd, "pay_deadline_at": due.pay_deadline_at.isoformat(),
                    "project_id": str(due.project_id) if due.project_id else None,
                },
            )
            await _finish(db, due, kind, "IN_APP", status=STATUS_SENT, sent_at=now)
            counts["in_app"]["sent"] += 1
            told.add(due.investor_id)

        if investor.email and await _claim(db, due, kind, "EMAIL", to=investor.email, now=now):
            try:
                content = compose_email(due)
                await system_email.deliver(
                    db, to=investor.email, content=content,
                    record={"day14_reminder": kind, "tranche_id": str(due.tranche_id), "heading": content.heading},
                    label=f"Day-14 reminder {due.tranche_id}",
                )
            except Exception as exc:  # noqa: BLE001 - recorded on the row, retried next run
                await db.rollback()
                code = _error_code(exc)
                logger.warning("Day-14 reminder email %s: not sent (%s)", due.tranche_id, code)
                await _finish(db, due, kind, "EMAIL", status=STATUS_FAILED, last_error=code[:100])
                counts["email"]["failed"] += 1
            else:
                await _finish(db, due, kind, "EMAIL", status=STATUS_SENT, sent_at=dt.datetime.now(dt.timezone.utc))
                counts["email"]["sent"] += 1

        to = _zalo_phone(investor)
        if template_id and not zalo_down and to and await _claim(db, due, kind, "ZALO", to=to, now=now):
            try:
                await zalo.send_template(
                    to, template_id, zalo_template_data(due),
                    # 32 characters: kind, day and ticket, for matching Zalo's delivery reports.
                    tracking_id=f"d{kind[6]}{now.astimezone(ICT):%y%m%d}{due.tranche_id.hex[:24]}",
                )
            except Exception as exc:  # noqa: BLE001 - recorded on the row, retried next run
                await db.rollback()
                code = _error_code(exc)
                logger.warning("Day-14 reminder Zalo %s: not sent (%s)", due.tranche_id, code)
                await _finish(db, due, kind, "ZALO", status=STATUS_FAILED, last_error=code[:100])
                counts["zalo"]["failed"] += 1
                zalo_down = isinstance(exc, zalo.ZaloNotConfigured)  # every other send would fail the same way
            else:
                await _finish(db, due, kind, "ZALO", status=STATUS_SENT, sent_at=dt.datetime.now(dt.timezone.utc))
                counts["zalo"]["sent"] += 1

    await realtime.publish(told)
    return counts
