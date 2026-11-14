"""The investor's side of a repayment receipt: the business says it paid them and uploads its bank receipt; the
investor checks their own account and confirms the money arrived (ADR-009: no bank feed, so the person who was paid
says so). Spec: docs/specs/direct_transfer/repayment-receipt-confirmation.md.

One receipt pays one investor for one or more days, a REPAYMENT transfer per day (app/market/repayment_router.py
`pay_with_receipt`). Confirming matches each day (Edward's `match_day`: MATCHED, settled, the ledger posted); a day
the investor had reported missing has that report closed as PAID. A repayment that did not arrive is reported from
the investment's schedule instead (report-missing: a dispute staff decide). Staff no longer approve receipts; they
can still read them (app/admin/repayments.py).
"""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.disputes import decide_dispute
from app.direct_transfer.errors import DirectTransferError
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
from app.market.receipt_auto import ACCEPTED, DENIED, system_check_state
from app.market.repayment_receipt_check import ENTITY_RECEIPT
from app.market.schemas import (
    RepaymentReceiptAccountOut,
    RepaymentReceiptDayOut,
    RepaymentReceiptOut,
    RepaymentSystemCheckOut,
)
from app.notifications import realtime
from app.notifications.models import EVENT_REPAYMENT_RECEIPT_CONFIRMED, EVENT_REPAYMENT_RECEIPT_SUBMITTED
from app.notifications.service import create_notification
from app.users.models import User
from app.utils.audit import append_audit

# AWAITING: a receipt with a day the investor has not confirmed. ALL adds the ones already settled either way.
RECEIPT_FILTERS = ("AWAITING", "ALL")

CONFIRMED_REASON = "The investor confirmed the repayment reached their account"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _http(exc: DirectTransferError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code})


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Receipt not found")


async def _transfers(db: AsyncSession, receipt_id: UUID, investor_id: UUID, *, lock: bool = False) -> list[Transfer]:
    """The receipt's transfers, if every one of them pays this investor; 404 otherwise (another investor's receipt
    looks exactly like one that does not exist)."""
    stmt = (
        select(Transfer, Tranche.investor_id)
        .join(Tranche, Tranche.id == Transfer.tranche_id)
        .where(Transfer.kind == "REPAYMENT", Transfer.evidence_file_id == receipt_id)
        .order_by(Transfer.created_at, Transfer.id)
    )
    rows = (await db.execute(stmt.with_for_update(of=Transfer) if lock else stmt)).all()
    if not rows or any(payee != investor_id for _, payee in rows):
        raise _not_found()
    return [transfer for transfer, _ in rows]


def _account(account: PaymentAccount | None) -> RepaymentReceiptAccountOut | None:
    if account is None:
        return None
    return RepaymentReceiptAccountOut(
        bank_name=account.bank_name, account_number=account.account_number, account_name=account.account_name
    )


async def _row(db: AsyncSession, receipt_id: UUID, investor_id: UUID) -> RepaymentReceiptOut:
    transfers = await _transfers(db, receipt_id, investor_id)
    tranche = await db.get(Tranche, transfers[0].tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    project = await db.get(Project, window.project_id)
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
    checks = {t.check_status for t in transfers}
    status = (
        "AWAITING_CONFIRMATION" if "AWAITING_FIELDS" in checks else "REJECTED" if "REJECTED" in checks else "CONFIRMED"
    )
    rows = [
        RepaymentReceiptDayOut(
            repayment_day_id=day.id,
            kind=day.kind,
            due_date=day.due_date,
            principal_vnd=day.principal_vnd,
            interest_vnd=day.interest_vnd,
            gross_vnd=day.gross_vnd,
            tax_withheld_vnd=day.tax_withheld_vnd,
            net_vnd=day.net_vnd,
            status=day.status,
        )
        for transfer in transfers
        if (day := days.get(transfer.repayment_day_id)) is not None
    ]
    rows.sort(key=lambda row: (row.due_date, row.kind != "STUB"))
    matched = [day.matched_at for day in days.values() if day.matched_at is not None]
    return RepaymentReceiptOut(
        id=receipt_id,
        tranche_id=tranche.id,
        reference_code=tranche.reference_code,
        project_name=project.legal_name if project else "",
        submitted_at=min(t.created_at for t in transfers),
        status=status,
        confirmed_at=max(matched) if status == "CONFIRMED" and matched else None,
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
        system_check=await system_check_for_investor(db, receipt_id),
    )


_RESULTS = {ACCEPTED: "MATCHES", DENIED: "DOES_NOT_MATCH"}


async def system_check_for_investor(db: AsyncSession, receipt_id: UUID) -> RepaymentSystemCheckOut | None:
    """What the system read off the receipt (receipt-ocr.md section 10), as advice: a confident match or mismatch
    whether or not automatic decisions are on, anything else unsure. Null when the receipt was not read."""
    state = await system_check_state(db, receipt_id, entity_type=ENTITY_RECEIPT)
    if state is None:
        return None
    result = _RESULTS.get(state.suggested or "", "UNSURE")
    return RepaymentSystemCheckOut(
        result=result,
        failure_codes=state.failure_codes if result == "DOES_NOT_MATCH" else [],
        checked_at=state.decided_at,
    )


async def list_receipts(
    db: AsyncSession, *, investor_id: UUID, status: str, limit: int
) -> list[RepaymentReceiptOut]:
    """The receipts businesses uploaded for repaying this investor, the ones waiting on them first, then newest."""
    mine = (
        select(Transfer.evidence_file_id)
        .join(Tranche, Tranche.id == Transfer.tranche_id)
        .where(Transfer.kind == "REPAYMENT", Tranche.investor_id == investor_id)
    )
    waiting = func.bool_or(Transfer.check_status == "AWAITING_FIELDS")
    stmt = (
        select(Transfer.evidence_file_id)
        .join(Tranche, Tranche.id == Transfer.tranche_id)
        .where(Transfer.kind == "REPAYMENT", Transfer.evidence_file_id.in_(mine))
        .group_by(Transfer.evidence_file_id)
        # A receipt that also pays someone else is not shown: it carries their bank details.
        .having(func.bool_and(Tranche.investor_id == investor_id))
    )
    if status == "AWAITING":
        stmt = stmt.having(waiting)
    ids = (
        await db.execute(
            stmt.order_by(case((waiting, 0), else_=1), func.min(Transfer.created_at).desc(), Transfer.evidence_file_id)
            .limit(limit)
        )
    ).scalars().all()
    return [await _row(db, receipt_id, investor_id) for receipt_id in ids]


async def confirm_receipt(
    db: AsyncSession, receipt_id: UUID, *, investor: User, ip_address: str | None
) -> RepaymentReceiptOut:
    """The investor says the repayment on this receipt reached their account: each day it pays that still waits is
    MATCHED and settled (the ledger posted, the loan closed once every day is), and a day they had reported missing
    has that report closed as PAID. Final, audited with the investor, and the business is told."""
    investor_id = investor.id
    transfers = await _transfers(db, receipt_id, investor_id, lock=True)
    waiting = [t for t in transfers if t.check_status == "AWAITING_FIELDS"]
    if not waiting:
        raise HTTPException(
            status_code=409,
            detail="Nothing on this receipt waits for your confirmation",
            headers={"X-Error-Code": "NOTHING_TO_CONFIRM"},
        )
    tranche = await db.get(Tranche, transfers[0].tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    tranche_id, sme_user_id, now = tranche.id, window.sme_user_id, _now()
    try:
        for transfer in waiting:
            day = await db.get(RepaymentDay, transfer.repayment_day_id)
            report = (
                await db.execute(
                    select(Dispute).where(Dispute.repayment_day_id == day.id, Dispute.status != "DECIDED")
                )
            ).scalar_one_or_none()
            if report is not None:
                # They said it never arrived; it did. Their own report closes as paid.
                await decide_dispute(
                    db, dispute_id=report.id, staff_id=investor_id, outcome="PAID", reason=CONFIRMED_REASON, now=now,
                    actor_role="INVESTOR",
                )
                day.transfer_id = transfer.id
                transfer.confirmation_level = "CONFIRMED"
            else:
                await match_day(
                    db, day_id=day.id, staff_id=investor_id, reason=CONFIRMED_REASON, now=now,
                    transfer_id=transfer.id, actor_role="INVESTOR",
                )
            transfer.check_status = "MATCHED"
    except DirectTransferError as exc:
        raise _http(exc) from exc
    net = sum(t.amount_vnd for t in waiting)
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche_id,
        action="REPAYMENT_RECEIPT_CONFIRMED",
        actor_id=investor_id,
        after_state={
            "receipt_id": str(receipt_id),
            "repayment_day_ids": [str(t.repayment_day_id) for t in waiting],
            "net_vnd": net,
            "check_status": "MATCHED",
            "reason": CONFIRMED_REASON,
        },
        ip_address=ip_address,
    )
    create_notification(
        db,
        user_id=sme_user_id,
        event=EVENT_REPAYMENT_RECEIPT_CONFIRMED,
        entity_type="TRANCHE",
        entity_id=tranche_id,
        data={
            "tranche_id": str(tranche_id),
            "memo": tranche.reference_code,
            "investor": mask_name(investor.full_name or investor.email),
            "amount_vnd": net,
        },
    )
    await db.commit()
    await realtime.publish([sme_user_id])
    db.expire_all()
    return await _row(db, receipt_id, investor_id)


async def notify_investor_of_receipt(
    db: AsyncSession, *, tranche: Tranche, receipt_id: UUID, net_vnd: int
) -> UUID:
    """Ring the investor's bell for a receipt the business just uploaded: check the account, then confirm. Staged on
    the caller's transaction (it commits); returns whom to push to once it has."""
    window = await db.get(FundingWindow, tranche.window_id)
    project = await db.get(Project, window.project_id)
    create_notification(
        db,
        user_id=tranche.investor_id,
        event=EVENT_REPAYMENT_RECEIPT_SUBMITTED,
        entity_type="TRANCHE",
        entity_id=tranche.id,
        data={
            "tranche_id": str(tranche.id),
            "receipt_id": str(receipt_id),
            "memo": tranche.reference_code,
            "project": project.legal_name if project else "",
            "amount_vnd": net_vnd,
        },
    )
    return tranche.investor_id
