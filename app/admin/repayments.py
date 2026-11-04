"""The admin Transactions tab's repayments: the receipts businesses upload when they repay an investor, for staff to
read (ADR-009: no bank feed). Read-only: the investor the receipt pays confirms it arrived
(app/market/repayment_receipts.py); staff no longer approve or deny receipts.

One receipt pays one investor for one or more days, a REPAYMENT transfer per day (app/market/repayment_router.py
`pay_with_receipt`). A day the investor says never arrived is reported from their schedule and decided by staff
through the dispute queue, as before.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.funding import RECEIPT_URL_TTL_SECONDS
from app.admin.schemas import (
    ActorOut,
    AdminRepaymentAccountOut,
    AdminReceiptUrlResponse,
    AdminRepaymentDayOut,
    AdminRepaymentReceiptOut,
    Page,
)
from app.direct_transfer.models import (
    Dispute,
    EvidenceFile,
    FundingWindow,
    PaymentAccount,
    RepaymentDay,
    Tranche,
    Transfer,
)
from app.lending.models import Project
from app.users.models import User
from app.utils import r2
from app.utils.audit import append_audit

# NEEDS_ACTION: a receipt with a day still waiting on the investor's confirmation. DECIDED: confirmed (or, before
# investors confirmed, approved or denied by staff).
RECEIPT_FILTERS = ("NEEDS_ACTION", "DECIDED", "ALL")


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


async def _row(db: AsyncSession, receipt_id: UUID) -> AdminRepaymentReceiptOut:
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
    return Page[AdminRepaymentReceiptOut](
        items=[await _row(db, receipt_id) for receipt_id in ids],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-total // page_size)),
    )


async def get_receipt(db: AsyncSession, receipt_id: UUID) -> AdminRepaymentReceiptOut:
    """One receipt as the panel shows it, read fresh so a confirmation made since is reflected."""
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
