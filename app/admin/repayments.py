"""The admin Transactions tab's repayments: the receipts businesses upload when they repay an investor, for staff to
approve or deny by looking at them (ADR-009: no bank feed, so a person reads each receipt).

One receipt pays one investor for one or more days, a REPAYMENT transfer per day (app/market/repayment_router.py
`pay_with_receipt`). Approving matches each day (Edward's `match_day`: MATCHED, settled, the ledger posted; a day the
investor reported missing is settled through its dispute as PAID). Denying rejects the receipt and puts its days
back to DUE, so the business pays and uploads again; it is told why, by its bell and on the investment's page.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.funding import RECEIPT_URL_TTL_SECONDS, _http
from app.admin.schemas import (
    ActorOut,
    AdminRepaymentAccountOut,
    AdminReceiptUrlResponse,
    AdminRepaymentDayOut,
    AdminRepaymentReceiptOut,
    Page,
)
from app.direct_transfer.disputes import decide_dispute
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import (
    Dispute,
    EvidenceFile,
    FundingWindow,
    PaymentAccount,
    RepaymentDay,
    Tranche,
    Transfer,
)
from app.direct_transfer.repayment import match_day
from app.direct_transfer.tranches import mask_name
from app.lending.models import Project
from app.notifications import realtime
from app.notifications.models import EVENT_REPAYMENT_RECEIPT_REJECTED
from app.notifications.service import create_notification
from app.users.models import User
from app.utils import r2
from app.utils.audit import append_audit

# NEEDS_ACTION is the queue: a receipt with a day waiting on staff. DECIDED: approved or denied.
RECEIPT_FILTERS = ("NEEDS_ACTION", "DECIDED", "ALL")

APPROVED_REASON = "Approved after viewing the receipt"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _receipts():
    """One row per receipt: when it came in, and whether any day it pays still waits on staff."""
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


async def _transfers(db: AsyncSession, receipt_id: UUID, *, lock: bool = False) -> list[Transfer]:
    stmt = (
        select(Transfer)
        .where(Transfer.kind == "REPAYMENT", Transfer.evidence_file_id == receipt_id)
        .order_by(Transfer.created_at, Transfer.id)
    )
    transfers = (await db.execute(stmt.with_for_update() if lock else stmt)).scalars().all()
    if not transfers:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return list(transfers)


def _account(account: PaymentAccount | None) -> AdminRepaymentAccountOut | None:
    if account is None:
        return None
    return AdminRepaymentAccountOut(
        bank_name=account.bank_name, account_number=account.account_number, account_name=account.account_name
    )


async def _row(db: AsyncSession, receipt_id: UUID) -> AdminRepaymentReceiptOut:
    transfers = await _transfers(db, receipt_id)
    tranche = await db.get(Tranche, transfers[0].tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    project = await db.get(Project, window.project_id)
    business = await db.get(User, window.sme_user_id)
    investor = await db.get(User, tranche.investor_id)
    file = await db.get(EvidenceFile, receipt_id)
    day_ids = [t.repayment_day_id for t in transfers]
    days = {
        day.id: day
        for day in (await db.execute(select(RepaymentDay).where(RepaymentDay.id.in_(day_ids)))).scalars()
    }
    reported = (
        await db.execute(
            select(Dispute.id).where(Dispute.repayment_day_id.in_(day_ids), Dispute.status != "DECIDED").limit(1)
        )
    ).first() is not None
    statuses = {t.check_status for t in transfers}
    status = "AWAITING_FIELDS" if "AWAITING_FIELDS" in statuses else "REJECTED" if "REJECTED" in statuses else "MATCHED"
    rows = [
        AdminRepaymentDayOut(
            repayment_day_id=day.id,
            transfer_id=transfer.id,
            kind=day.kind,
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
    )


async def list_receipts(
    db: AsyncSession, *, status: str, page: int, page_size: int
) -> Page[AdminRepaymentReceiptOut]:
    """Businesses' repayment receipts, the ones waiting on staff first, then newest."""
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
    return Page[AdminRepaymentReceiptOut](
        items=[await _row(db, receipt_id) for receipt_id in ids],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),
    )


async def get_receipt(db: AsyncSession, receipt_id: UUID) -> AdminRepaymentReceiptOut:
    """One receipt as the review panel shows it, read fresh so a decision made elsewhere is reflected."""
    return await _row(db, receipt_id)


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


async def decide_receipt(
    db: AsyncSession, receipt_id: UUID, *, outcome: str, reason: str | None, actor: User, ip_address: str | None
) -> AdminRepaymentReceiptOut:
    """Approve or deny a business's repayment receipt after looking at it; nothing is typed in or compared.

    Approving means the investor was paid: each day the receipt pays is MATCHED and settled. Denying rejects the
    receipt (a note is required; the business sees it): its days are due again, for the business to pay and upload
    a receipt that shows it, and its bell rings. Only the days still waiting on staff are decided. Final, and
    audited with the admin who made it.
    """
    approve = outcome == "APPROVED"
    reason = (reason or "").strip()
    if not approve and not reason:
        raise HTTPException(
            status_code=422, detail="Say why the receipt is denied", headers={"X-Error-Code": "REASON_REQUIRED"}
        )
    reason = reason or APPROVED_REASON
    transfers = await _transfers(db, receipt_id, lock=True)
    tranche = await db.get(Tranche, transfers[0].tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    # Nobody checks money they paid or were paid.
    if actor.id in (tranche.investor_id, window.sme_user_id):
        raise HTTPException(status_code=403, detail="You cannot review a repayment you are part of")
    waiting = [t for t in transfers if t.check_status == "AWAITING_FIELDS"]
    if not waiting:
        raise HTTPException(
            status_code=409,
            detail="Nothing on this receipt waits for a decision",
            headers={"X-Error-Code": "NOTHING_TO_DECIDE"},
        )
    tranche_id, sme_user_id, now = tranche.id, window.sme_user_id, _now()
    try:
        for transfer in waiting:
            day = await db.get(RepaymentDay, transfer.repayment_day_id)
            if approve:
                report = (
                    await db.execute(
                        select(Dispute).where(Dispute.repayment_day_id == day.id, Dispute.status != "DECIDED")
                    )
                ).scalar_one_or_none()
                if report is not None:
                    # The investor said it never arrived; the receipt settles that as paid.
                    await decide_dispute(
                        db, dispute_id=report.id, staff_id=actor.id, outcome="PAID", reason=reason, now=now
                    )
                    day.transfer_id = transfer.id
                    transfer.confirmation_level = "CONFIRMED"
                else:
                    await match_day(
                        db, day_id=day.id, staff_id=actor.id, reason=reason, now=now, transfer_id=transfer.id
                    )
                transfer.check_status = "MATCHED"
            else:
                transfer.check_status = "REJECTED"
                if day.transfer_id == transfer.id:
                    day.transfer_id = None
                # Marked paid on this receipt: due again. A day the investor reported keeps its report.
                if day.status == "PRESUMED_PAID" and day.settled_at is None:
                    day.status = "DUE"
                    day.marked_paid_at = None
                    day.marked_paid_by = None
                await log_event(
                    db, entity_type="REPAYMENT_DAY", entity_id=day.id, event_type="REPAYMENT_DAY_REPORTED", now=now,
                    actor_id=actor.id, actor_role="ADMIN", reason=reason,
                    payload={"event": "RECEIPT_REJECTED", "transfer_id": str(transfer.id)},
                )
    except DirectTransferError as exc:
        raise _http(exc) from exc
    net = sum(t.amount_vnd for t in waiting)
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche_id,
        action="REPAYMENT_RECEIPT_APPROVED" if approve else "REPAYMENT_RECEIPT_DENIED",
        actor_id=actor.id,
        after_state={
            "receipt_id": str(receipt_id),
            "repayment_day_ids": [str(t.repayment_day_id) for t in waiting],
            "net_vnd": net,
            "check_status": "MATCHED" if approve else "REJECTED",
            "reason": reason,
        },
        ip_address=ip_address,
    )
    if not approve:
        investor = await db.get(User, tranche.investor_id)
        create_notification(
            db,
            user_id=sme_user_id,
            event=EVENT_REPAYMENT_RECEIPT_REJECTED,
            entity_type="TRANCHE",
            entity_id=tranche_id,
            data={
                "tranche_id": str(tranche_id),
                "memo": tranche.reference_code,
                "investor": mask_name(investor.full_name or investor.email),
                "amount_vnd": net,
                "reason": reason,
            },
        )
    await db.commit()
    if not approve:
        await realtime.publish([sme_user_id])
    db.expire_all()
    return await _row(db, receipt_id)
