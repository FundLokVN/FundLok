"""Commit-then-accept jobs for Days 12 to 13 (Money flow FINAL v1.0; docs/specs/direct_transfer/day13-loan-contracts.md).

Two Cloud Scheduler jobs on prod-api (app/scheduled_jobs/router.py):

* `funding-advance`, 00:00, 15:00 and 15:40 ICT: runs `close.advance_window` for every commit-then-accept round with
  something due (00:00 Day 13: the commit phase ends and the pledge list is final; 15:00 Day 13: the silence rule;
  15:40 Day 14, after the 15:35 no-show step: the round closes and the pooled loan forms with everyone who paid, or
  waits for pending tickets, at most 2 days), then what the new state asks of the round's loan contracts
  (`day13_contracts.after_advance`: generated at the end of the commit phase, void when the round fails). One round
  per transaction; idempotent. Before the rounds, every ticket whose SME 24 hours have passed is confirmed, on any
  round (`tranches.confirm_lapsed_tranches`), so one left out of a loan that already formed is voided and refunded.
* `acceptance-reminders`, 09:00 ICT: 3 days and 1 day before the SME's 15:00 Day-13 deadline, the SME is reminded in
  the app, by email and on Zalo (once the ZBS template is approved and ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID is set), each
  logged in acceptance_reminders (one per round, kind and channel). SMS has no provider (D-056): logged SKIPPED. A round
  already decided is logged SKIPPED. A FAILED reminder is retried by a later run of the same day's kind.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import system_email
from app.admin.internal_email.schemas import EmailContent
from app.core.config import settings
from app.core.timezone import ICT
from app.direct_transfer import close, commit_flow, tranches
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import AcceptanceReminder, FundingWindow
from app.lending.models import Project
from app.market import day13_contracts, funding
from app.notifications import realtime
from app.notifications.models import EVENT_ACCEPTANCE_DUE_1D, EVENT_ACCEPTANCE_DUE_3D
from app.notifications.service import create_notification
from app.phone_verification import zalo
from app.scheduled_jobs.day14 import _error_code, _zalo_phone
from app.users.models import User
from app.utils.locks import advisory_xact_lock

logger = logging.getLogger(__name__)

KINDS = {3: "D_MINUS_3", 1: "D_MINUS_1"}
EVENTS = {"D_MINUS_3": EVENT_ACCEPTANCE_DUE_3D, "D_MINUS_1": EVENT_ACCEPTANCE_DUE_1D}
CHANNELS = ("IN_APP", "EMAIL", "ZALO", "SMS")
_COMPANY_MAX = 200


# --------------------------------------------------------------------------- #
# funding-advance
# --------------------------------------------------------------------------- #
async def run_funding_advance(db: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    """Advance every commit-then-accept round with something due, one per transaction. Returns {"advanced",
    "contracts", "failed"}: rounds looked at, loan contracts generated, rounds that failed (rolled back; the route
    answers 500 for them so Cloud Scheduler retries the whole run, which the rounds already advanced skip).

    First, in its own transaction, every ticket whose SME 24 hours have passed is confirmed, on any round, including
    one whose loan already formed (it is then void and the SME refunds it): the per-round loop below never looks at a
    round once it is REPAYING. A failure there is rolled back and counted like a failed round."""
    now = now or dt.datetime.now(dt.timezone.utc)
    totals = {"advanced": 0, "contracts": 0, "failed": 0}
    try:
        await tranches.confirm_lapsed_tranches(db, now=now)
        await db.commit()
    except Exception:  # noqa: BLE001 - the rounds below still run; the route answers 500 so the scheduler retries
        await db.rollback()
        logger.exception("funding-advance: confirming lapsed tickets failed")
        totals["failed"] += 1
    ids = (
        await db.execute(
            select(FundingWindow.id)
            .where(
                FundingWindow.flow == commit_flow.COMMIT_THEN_ACCEPT,
                FundingWindow.status.in_(("OPEN", "AWAITING_SME", "AWAITING_PAYMENT", "CLOSING")),
            )
            .order_by(FundingWindow.id)
        )
    ).scalars().all()
    for window_id in ids:
        try:
            window = await close.advance_window(db, window_id=window_id, now=now)
            totals["contracts"] += await day13_contracts.generate_for_window(
                db, window=window, storage=funding.evidence_storage(), now=now
            )
            await day13_contracts.void_for_window(db, window=window, now=now)
            await db.commit()
            totals["advanced"] += 1
        except Exception:  # noqa: BLE001 - one round's failure must not stop the others
            await db.rollback()
            logger.exception("funding-advance: round %s failed", window_id)
            totals["failed"] += 1
    return totals


# --------------------------------------------------------------------------- #
# acceptance-reminders
# --------------------------------------------------------------------------- #
def _deadline(window: FundingWindow) -> str:
    return window.sme_deadline_at.astimezone(ICT).strftime("%H:%M %d/%m/%Y")


def compose_email(window: FundingWindow, title: str, kind: str) -> EmailContent:
    days = 3 if kind == "D_MINUS_3" else 1
    deadline = _deadline(window)
    name = html.escape(title or "FundLok")
    body = (
        f"<p>Còn {days} ngày đến hạn quyết định của <b>{name}</b>: trước <b>{deadline}</b>, doanh nghiệp chấp nhận "
        "hoặc từ chối tổng số tiền nhà đầu tư đã cam kết. Chấp nhận nghĩa là ký số hợp đồng cho vay của từng nhà đầu "
        "tư. Nếu không quyết định, từ 50% mục tiêu trở lên được xem là chấp nhận và doanh nghiệp bị ràng buộc bởi các "
        "hợp đồng; dưới 50% được xem là từ chối. Dưới 50% mục tiêu, việc ký phải xong trước 12:00 Ngày 13.</p>"
        f"<p><i>{days} day(s) to your decision on {name}: by {deadline}, accept or decline the total investors "
        "pledged. Accepting means signing each investor's loan contract with your enterprise digital signature. If "
        "you stay silent, 50% of the target or more counts as accepted and binds your business to every contract; "
        "less counts as declined. Below 50% of the target, signing must be finished by 12:00 on Day 13.</i></p>"
    )
    url = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    https = url.startswith("https://")
    return EmailContent(
        template="action_required",
        subject=f"FundLok: quyết định trước {deadline} / decide by {deadline}",
        heading="Quyết định nhận vốn · Your funding decision",
        body=body,
        button_label="Mở FundLok · Open FundLok" if https else None,
        button_url=url if https else None,
    )


def zalo_template_data(window: FundingWindow, title: str, pledged_vnd: int) -> dict[str, str]:
    """Parameters of the SME acceptance ZBS template (to register): company_name, amount (pledged), deadline."""
    return {
        "company_name": (title or "FundLok")[:_COMPANY_MAX],
        "amount": str(pledged_vnd),
        "deadline": _deadline(window),
    }


async def _logged(db: AsyncSession, window_id: UUID, kind: str, channel: str) -> AcceptanceReminder | None:
    return await db.get(AcceptanceReminder, (window_id, kind, channel), populate_existing=True)


async def _log(
    db: AsyncSession, window_id: UUID, kind: str, channel: str, status: str, *, now: dt.datetime,
    sent_to: str | None = None, error: str | None = None,
) -> None:
    row = await _logged(db, window_id, kind, channel)
    if row is None:
        db.add(
            AcceptanceReminder(
                window_id=window_id, kind=kind, channel=channel, status=status, sent_to=sent_to, error=error,
                sent_at=now,
            )
        )
    else:
        row.status, row.sent_to, row.error, row.sent_at = status, sent_to, error, now
    await db.flush()


def _due(row: AcceptanceReminder | None) -> bool:
    """Not yet logged, or logged FAILED (retried)."""
    return row is None or row.status == "FAILED"


async def send_acceptance_reminders(db: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    """3 days and 1 day before each round's Day-13 deadline, remind its SME. Returns per channel
    {"in_app", "email", "zalo", "sms"}: {"sent", "failed", "skipped"}."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = ict_date(now)
    template_id = settings.ZALO_ZBS_ACCEPTANCE_TEMPLATE_ID
    counts = {c.lower(): {"sent": 0, "failed": 0, "skipped": None} for c in CHANNELS}
    if not template_id:
        counts["zalo"]["skipped"] = "NOT_CONFIGURED"
    counts["sms"]["skipped"] = "NO_PROVIDER"
    told: set[UUID] = set()

    windows = (
        await db.execute(
            select(FundingWindow, Project)
            .outerjoin(Project, Project.id == FundingWindow.project_id)
            .where(
                FundingWindow.flow == commit_flow.COMMIT_THEN_ACCEPT,
                FundingWindow.sme_deadline_at.is_not(None),
                FundingWindow.sme_deadline_at > now,
            )
            .order_by(FundingWindow.id)
        )
    ).all()
    for window, project in windows:
        kind = KINDS.get((ict_date(window.sme_deadline_at) - today).days)
        if kind is None:
            continue
        await advisory_xact_lock(db, f"acceptance-reminders:{window.id}:{kind}")
        title = (project.legal_name if project else None) or ""
        decided = window.sme_decision is not None or window.status not in ("OPEN", "AWAITING_SME")
        if decided:
            for channel in CHANNELS:
                if _due(await _logged(db, window.id, kind, channel)):
                    await _log(db, window.id, kind, channel, "SKIPPED", now=now, error="DECIDED")
            await db.commit()
            continue

        sme = await db.get(User, window.sme_user_id, populate_existing=True)
        pledged = await commit_flow.pledged_total(db, window.id)

        if _due(await _logged(db, window.id, kind, "IN_APP")):
            create_notification(
                db, user_id=window.sme_user_id, event=EVENTS[kind], entity_type="FUNDING_WINDOW", entity_id=window.id,
                data={
                    "window_id": str(window.id), "listing_title": title, "pledged_vnd": pledged,
                    "target_vnd": window.target_vnd, "sme_deadline_at": window.sme_deadline_at.isoformat(),
                    "project_id": str(window.project_id),
                },
            )
            await _log(db, window.id, kind, "IN_APP", "SENT", now=now)
            counts["in_app"]["sent"] += 1
            told.add(window.sme_user_id)
        await db.commit()

        if sme is not None and sme.email and _due(await _logged(db, window.id, kind, "EMAIL")):
            try:
                content = compose_email(window, title, kind)
                await system_email.deliver(
                    db, to=sme.email, content=content,
                    record={"acceptance_reminder": kind, "window_id": str(window.id), "heading": content.heading},
                    label=f"Acceptance reminder {window.id}",
                )
            except Exception as exc:  # noqa: BLE001 - logged FAILED, retried next run
                await db.rollback()
                await _log(db, window.id, kind, "EMAIL", "FAILED", now=now, sent_to=sme.email,
                           error=_error_code(exc)[:100])
                counts["email"]["failed"] += 1
            else:
                await _log(db, window.id, kind, "EMAIL", "SENT", now=now, sent_to=sme.email)
                counts["email"]["sent"] += 1
            await db.commit()

        to = _zalo_phone(sme)
        if _due(await _logged(db, window.id, kind, "ZALO")):
            if not template_id or not to:
                await _log(db, window.id, kind, "ZALO", "SKIPPED", now=now,
                           error="NOT_CONFIGURED" if not template_id else "NO_VERIFIED_PHONE")
            else:
                try:
                    await zalo.send_template(
                        to, template_id, zalo_template_data(window, title, pledged),
                        tracking_id=f"a{kind[-1]}{now.astimezone(ICT):%y%m%d}{window.id.hex[:24]}",
                    )
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    await _log(db, window.id, kind, "ZALO", "FAILED", now=now, sent_to=to, error=_error_code(exc)[:100])
                    counts["zalo"]["failed"] += 1
                else:
                    await _log(db, window.id, kind, "ZALO", "SENT", now=now, sent_to=to)
                    counts["zalo"]["sent"] += 1
            await db.commit()

        if _due(await _logged(db, window.id, kind, "SMS")):
            # D-056: no SMS provider is chosen; the mock sender records the skip.
            await _log(db, window.id, kind, "SMS", "SKIPPED", now=now, error="NO_PROVIDER")
            await db.commit()

    await realtime.publish(told)
    return counts
