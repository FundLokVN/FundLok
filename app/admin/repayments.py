"""The admin Transactions tab's repayments: the receipts businesses upload when they repay an investor, for staff to
read (ADR-009: no bank feed). Read-only: the investor the receipt pays confirms it arrived
(app/market/repayment_receipts.py); staff no longer approve or deny receipts.

One receipt pays one investor for one or more days, a REPAYMENT transfer per day (app/market/repayment_router.py
`pay_with_receipt`). A day the investor says never arrived is reported from their schedule and decided by staff
through the dispute queue, as before.

Late repayments (`overdue_summary`, `list_overdue`): a day the business has not marked paid (still DUE: uploading a
receipt makes it PRESUMED_PAID) after its due date. Display only, for staff: no arrears state, no late interest
(lateness handling is out of MVP scope, CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from collections.abc import Iterable
from typing import Protocol
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.funding import RECEIPT_URL_TTL_SECONDS, _receiver_out
from app.admin.schemas import (
    ActorOut,
    AdminOverdueRepaymentOut,
    AdminOverdueRepaymentsOut,
    AdminPayerCheckOut,
    AdminRepaymentAccountOut,
    AdminReceiptUrlResponse,
    AdminRepaymentDayOut,
    AdminRepaymentReceiptOut,
    AdminSystemCheckOut,
    Page,
)
from app.direct_transfer.calendar_utils import count_business_days, ict_date
from app.direct_transfer.models import (
    WINDOW_LIVE_STATES,
    Dispute,
    EvidenceFile,
    FundingWindow,
    PaymentAccount,
    RepaymentDay,
    Tranche,
    Transfer,
)
from app.lending.models import Project
from app.market import receipt_auto
from app.market.repayment_receipt_check import ENTITY_RECEIPT
from app.repayment_schedule.calendar import BusinessDayCalendar
from app.users.models import User
from app.utils import r2
from app.utils.audit import append_audit

# NEEDS_ACTION: a receipt with a day still waiting on the investor's confirmation. DECIDED: confirmed (or, before
# investors confirmed, approved or denied by staff).
RECEIPT_FILTERS = ("NEEDS_ACTION", "DECIDED", "ALL")

# The rounds whose days the business is asked to pay: the ones its dashboard and the daily reminder read
# (app/direct_transfer/repayment.py `todays_payments`); with repay_from_approval a round repays while still open.
_REPAYING_ROUNDS = WINDOW_LIVE_STATES


class _Day(Protocol):
    status: str
    due_date: dt.date
    net_vnd: int
    settled_at: dt.datetime | None


@dataclasses.dataclass(frozen=True)
class OverdueSummary:
    """A ticket's late days. ``days`` counts due dates (a first-period-interest row due with day 1 is the same
    day); ``working_days_late`` runs from the oldest one."""

    days: int = 0
    net_vnd: int = 0
    oldest_due_date: dt.date | None = None
    working_days_late: int = 0


def is_overdue(day: _Day, today: dt.date) -> bool:
    """Not marked paid and past its due date. The business may pay a day at any hour of its due date
    (`mark_days_paid` has no cut-off; the 15:00 ICT reminder only says what is due today), so it is late from the
    next ICT calendar day. A day the investor reported missing, or staff matched, is not "not submitted"."""
    return day.status == "DUE" and day.settled_at is None and day.due_date < today


def overdue_summary(
    days: Iterable[_Day], today: dt.date, calendar: BusinessDayCalendar | None = None
) -> OverdueSummary:
    """How late a ticket is. Working days late = working days from the oldest late due date (included) to today
    (excluded): due Monday and unpaid on Tuesday is 1; due Friday and unpaid the next Monday is still 1."""
    late = [day for day in days if is_overdue(day, today)]
    if not late:
        return OverdueSummary()
    oldest = min(day.due_date for day in late)
    return OverdueSummary(
        days=len({day.due_date for day in late}),
        net_vnd=sum(int(day.net_vnd) for day in late),
        oldest_due_date=oldest,
        working_days_late=count_business_days(oldest, today, calendar),
    )


def _receipts():
    """One row per receipt: when it came in, and whether any day it pays still waits on the investor."""
    return (
        select(
            Transfer.evidence_file_id.label("receipt_id"),
            func.min(Transfer.created_at).label("submitted_at"),
            func.bool_or(Transfer.check_status == "AWAITING_FIELDS").label("waiting"),
        )
        .where(Transfer.kind == "REPAYMENT", Transfer.evidence_file_id.is_not(None))
        .group_by(Transfer.evidence_file_id)
        .subquery()
    )


async def _transfers(db: AsyncSession, receipt_id: UUID) -> list[Transfer]:
    stmt = (
        select(Transfer)
        .where(Transfer.kind == "REPAYMENT", Transfer.evidence_file_id == receipt_id)
        .order_by(Transfer.created_at, Transfer.id)
    )
    transfers = (await db.execute(stmt)).scalars().all()
    if not transfers:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return list(transfers)


def _account(account: PaymentAccount | None) -> AdminRepaymentAccountOut | None:
    if account is None:
        return None
    return AdminRepaymentAccountOut(
        bank_name=account.bank_name, account_number=account.account_number, account_name=account.account_name
    )


async def _system_check_out(db: AsyncSession, receipt_id: UUID) -> AdminSystemCheckOut | None:
    """What the system read off the receipt and how it compares (receipt-ocr.md section 10). Nothing is applied to a
    repayment receipt it kept, and staff do not review it: the investor confirms."""
    state = await receipt_auto.system_check_state(db, receipt_id, entity_type=ENTITY_RECEIPT)
    if state is None:
        return None
    reading = state.reading or {}
    payer = state.payer or None
    return AdminSystemCheckOut(
        verdict=state.verdict,
        applied=False,
        suggested=state.suggested,
        summary=state.summary,
        failure_codes=state.failure_codes,
        review_reasons=state.review_reasons,
        receiver=_receiver_out(state.receiver),
        payer=AdminPayerCheckOut(**payer) if payer else None,
        decided_at=state.decided_at,
        engine=reading.get("engine"),
        confidence=reading.get("confidence"),
        fields=reading.get("fields"),
        field_confidence=reading.get("field_confidence") or {},
        notes=list(reading.get("notes") or []),
    )


async def _row(db: AsyncSession, receipt_id: UUID, *, today: dt.date | None = None) -> AdminRepaymentReceiptOut:
    today = today or ict_date(dt.datetime.now(dt.timezone.utc))
    transfers = await _transfers(db, receipt_id)
    tranche = await db.get(Tranche, transfers[0].tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    project = await db.get(Project, window.project_id)
    business = await db.get(User, window.sme_user_id)
    investor = await db.get(User, tranche.investor_id)
    file = await db.get(EvidenceFile, receipt_id)
    day_ids = [t.repayment_day_id for t in transfers]
    tranche_days = (
        await db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tranche.id))
    ).scalars().all()
    days = {day.id: day for day in tranche_days}
    reported = (
        await db.execute(
            select(Dispute.id).where(Dispute.repayment_day_id.in_(day_ids), Dispute.status != "DECIDED").limit(1)
        )
    ).first() is not None
    late = overdue_summary(tranche_days, today)
    statuses = {t.check_status for t in transfers}
    status = "AWAITING_FIELDS" if "AWAITING_FIELDS" in statuses else "REJECTED" if "REJECTED" in statuses else "MATCHED"
    rows = [
        AdminRepaymentDayOut(
            repayment_day_id=day.id,
            transfer_id=transfer.id,
            kind=day.kind,
            day_index=day.day_index,
            due_date=day.due_date,
            principal_vnd=day.principal_vnd,
            interest_vnd=day.interest_vnd,
            gross_vnd=day.gross_vnd,
            tax_withheld_vnd=day.tax_withheld_vnd,
            net_vnd=day.net_vnd,
            status=day.status,
            check_status=transfer.check_status,
        )
        for transfer in transfers
        if (day := days.get(transfer.repayment_day_id)) is not None
    ]
    rows.sort(key=lambda row: (row.due_date, row.kind != "STUB"))
    return AdminRepaymentReceiptOut(
        id=receipt_id,
        tranche_id=tranche.id,
        window_id=window.id,
        project_name=project.legal_name if project else "",
        reference_code=tranche.reference_code,
        tranche_amount_vnd=int(tranche.amount_vnd),
        tranche_paid_days=len({d.due_date for d in tranche_days if d.status in ("PRESUMED_PAID", "MATCHED")}),
        tranche_total_days=len({d.due_date for d in tranche_days}),
        business=ActorOut.model_validate(business),
        investor=ActorOut.model_validate(investor),
        submitted_at=min(t.created_at for t in transfers),
        check_status=status,
        needs_action=status == "AWAITING_FIELDS",
        gross_vnd=sum(row.gross_vnd for row in rows),
        tax_withheld_vnd=sum(row.tax_withheld_vnd for row in rows),
        net_vnd=sum(row.net_vnd for row in rows),
        days=rows,
        payee=_account(await db.get(PaymentAccount, tranche.investor_account_id) if tranche.investor_account_id else None),
        payer=_account(
            await db.get(PaymentAccount, window.sme_payment_account_id) if window.sme_payment_account_id else None
        ),
        original_filename=file.original_filename if file else None,
        content_type=file.content_type if file else None,
        open_report=reported,
        system_check=await _system_check_out(db, receipt_id),
        tranche_overdue_days=late.days,
        tranche_overdue_net_vnd=late.net_vnd,
        tranche_oldest_overdue_date=late.oldest_due_date,
        tranche_working_days_late=late.working_days_late,
    )


async def list_receipts(
    db: AsyncSession, *, status: str, page: int, page_size: int
) -> Page[AdminRepaymentReceiptOut]:
    """Businesses' repayment receipts, the ones waiting on the investor first, then newest."""
    receipts = _receipts()
    stmt = select(receipts.c.receipt_id)
    if status == "NEEDS_ACTION":
        stmt = stmt.where(receipts.c.waiting)
    elif status == "DECIDED":
        stmt = stmt.where(~receipts.c.waiting)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    ids = (
        await db.execute(
            stmt.order_by(case((receipts.c.waiting, 0), else_=1), receipts.c.submitted_at.desc(), receipts.c.receipt_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    today = ict_date(dt.datetime.now(dt.timezone.utc))
    return Page[AdminRepaymentReceiptOut](
        items=[await _row(db, receipt_id, today=today) for receipt_id in ids],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),
    )


async def get_receipt(db: AsyncSession, receipt_id: UUID) -> AdminRepaymentReceiptOut:
    """One receipt as the panel shows it, read fresh so a confirmation made since is reflected."""
    return await _row(db, receipt_id)


async def list_overdue(
    db: AsyncSession, *, now: dt.datetime | None = None, calendar: BusinessDayCalendar | None = None
) -> AdminOverdueRepaymentsOut:
    """Every investor ticket a business is late repaying, most working days late first, then largest amount. A
    ticket with no receipt at all is here too: the receipts list cannot show it."""
    today = ict_date(now or dt.datetime.now(dt.timezone.utc))
    rows = (
        await db.execute(
            select(RepaymentDay, Tranche, FundingWindow)
            .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
            .join(FundingWindow, FundingWindow.id == Tranche.window_id)
            .where(
                RepaymentDay.status == "DUE",
                RepaymentDay.settled_at.is_(None),
                RepaymentDay.due_date < today,
                FundingWindow.status.in_(_REPAYING_ROUNDS),
            )
            .order_by(RepaymentDay.due_date, RepaymentDay.id)
        )
    ).all()
    by_tranche: dict[UUID, tuple[Tranche, FundingWindow, list[RepaymentDay]]] = {}
    for day, tranche, window in rows:
        by_tranche.setdefault(tranche.id, (tranche, window, []))[2].append(day)

    items: list[AdminOverdueRepaymentOut] = []
    for tranche, window, days in by_tranche.values():
        late = overdue_summary(days, today, calendar)
        project = await db.get(Project, window.project_id)
        items.append(
            AdminOverdueRepaymentOut(
                tranche_id=tranche.id,
                window_id=window.id,
                project_name=project.legal_name if project else "",
                reference_code=tranche.reference_code,
                business=ActorOut.model_validate(await db.get(User, window.sme_user_id)),
                investor=ActorOut.model_validate(await db.get(User, tranche.investor_id)),
                overdue_days=late.days,
                oldest_due_date=late.oldest_due_date,
                working_days_late=late.working_days_late,
                overdue_net_vnd=late.net_vnd,
            )
        )
    items.sort(
        key=lambda item: (-item.working_days_late, -item.overdue_net_vnd, item.project_name, item.reference_code)
    )
    return AdminOverdueRepaymentsOut(
        as_of=today,
        tickets=len(items),
        businesses=len({item.business.id for item in items}),
        overdue_days=sum(item.overdue_days for item in items),
        overdue_net_vnd=sum(item.overdue_net_vnd for item in items),
        items=items,
    )


async def get_receipt_url(
    db: AsyncSession, receipt_id: UUID, *, actor: User, ip_address: str | None
) -> AdminReceiptUrlResponse:
    """A 10-minute read URL for the business's receipt. Every view is audited: it shows two people's bank details."""
    transfers = await _transfers(db, receipt_id)
    file = await db.get(EvidenceFile, receipt_id)
    if file is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    append_audit(
        db,
        entity_type="EVIDENCE_FILE",
        entity_id=receipt_id,
        action="REPAYMENT_RECEIPT_VIEWED",
        actor_id=actor.id,
        after_state={"tranche_id": str(transfers[0].tranche_id), "expires_in": RECEIPT_URL_TTL_SECONDS},
        ip_address=ip_address,
    )
    await db.commit()
    return AdminReceiptUrlResponse(
        url=await asyncio.to_thread(r2.presign_get, file.storage_key, expires_in=RECEIPT_URL_TTL_SECONDS),
        expires_in=RECEIPT_URL_TTL_SECONDS,
        content_type=file.content_type,
        original_filename=file.original_filename,
    )
