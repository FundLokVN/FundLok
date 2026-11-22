"""Staff approve late money into the loan before the Day-14 receipt cut-off (docs/specs/direct_transfer/late-money-approval.md).

Late money (a right transfer outside its QR's 15 minutes, no QR, or after 14:45) is held `UNDER_REVIEW[LATE_MONEY]`
and holds no capacity. Until 15:30 staff may approve it: the ticket becomes `ATTESTED` (the SME's 24-hour receipt
check applies, never bypassed) and the loan forms with it. After 15:30 the 15:35 run turns it into a top-up pledge.
No ledger leg is posted here: the claim legs post at loan formation.
"""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.day14 import LATE, _has_open_dispute, _latest_receipt
from app.direct_transfer.day14_times import window_cutoffs
from app.direct_transfer.errors import DirectTransferError, conflict
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import EvidenceLog, FundingWindow, TopUpPledge, Tranche, Transfer
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.windows import capacity_left, lock_window, refresh_window_fullness
from app.notifications.models import EVENT_LATE_MONEY_APPROVED
from app.notifications.service import create_notification

EVENT_APPROVED = "LATE_MONEY_APPROVED"
# The round is still taking money; once it is closing or repaying a ticket can no longer join it.
APPROVABLE_WINDOW_STATUSES = ("OPEN", "AWAITING_PAYMENT")


def _is_late_money(tranche: Tranche) -> bool:
    return tranche.status == "UNDER_REVIEW" and tranche.review_cause == "LATE_MONEY"


async def approval_blocker(
    db: AsyncSession, *, tranche: Tranche, window: FundingWindow, transfer: Transfer, now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> str | None:
    """The error code that stops the approval (spec section 7), or None when staff can approve now."""
    p = resolve(params)
    receipt = await _latest_receipt(db, tranche.id)
    if (
        not _is_late_money(tranche)
        or receipt is None
        or receipt.id != transfer.id
        or transfer.check_status != "MATCHED"
        or transfer.payment_timing != LATE
        or await _has_open_dispute(db, tranche.id)
        or (await db.execute(select(TopUpPledge.id).where(TopUpPledge.tranche_id == tranche.id))).first() is not None
    ):
        return "NOT_LATE_MONEY"
    cutoffs = window_cutoffs(window, p)
    if cutoffs is None or window.status not in APPROVABLE_WINDOW_STATUSES or now >= cutoffs.receipt_deadline_at:
        return "LATE_MONEY_APPROVAL_CLOSED"
    bank_txn_id = (transfer.parsed_fields or {}).get("bank_txn_id")
    if bank_txn_id:
        used = (
            await db.execute(
                select(Transfer.id).where(
                    Transfer.bank_txn_id == bank_txn_id,
                    Transfer.kind.in_(("DISBURSEMENT", "REFUND")),
                    Transfer.id != transfer.id,
                )
            )
        ).first()
        if used is not None:
            return "DUPLICATE_BANK_TXN"
    if await capacity_left(db, window) < tranche.amount_vnd:
        return "CAPACITY_EXCEEDED"
    return None


async def approve_late_money(
    db: AsyncSession, *, transfer_id: UUID, staff_id: UUID, reason: str, now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Tranche:
    """Move one clean late-money ticket into the round. The caller commits and writes the audit row (it has the IP).
    Locks the transfer, the ticket, then the round, the order every staff decision uses."""
    p = resolve(params)
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    transfer = (
        await db.execute(select(Transfer).where(Transfer.id == transfer_id).with_for_update())
    ).scalar_one_or_none()
    if transfer is None or transfer.kind != "DISBURSEMENT" or transfer.tranche_id is None:
        raise DirectTransferError(404, "Transfer not found", "TRANSFER_NOT_FOUND")
    tranche = (
        await db.execute(select(Tranche).where(Tranche.id == transfer.tranche_id).with_for_update())
    ).scalar_one()
    if tranche.investor_id == staff_id:
        raise DirectTransferError(403, "You cannot review your own transfer", "OWN_TRANSFER")
    window = await lock_window(db, tranche.window_id)

    if not _is_late_money(tranche):
        approved_before = (
            await db.execute(
                select(EvidenceLog.id).where(
                    EvidenceLog.entity_type == "TRANCHE", EvidenceLog.entity_id == tranche.id,
                    EvidenceLog.event_type == EVENT_APPROVED,
                )
            )
        ).first()
        if approved_before is not None:
            raise conflict("ALREADY_APPROVED", "This late money was already approved")
        raise conflict("NOT_LATE_MONEY", "This ticket is not late money waiting for a decision")
    blocker = await approval_blocker(db, tranche=tranche, window=window, transfer=transfer, now=now, params=p)
    if blocker is not None:
        raise conflict(blocker, _DETAIL[blocker])

    cutoffs = window_cutoffs(window, p)
    if tranche.receipt_submitted_at is None:
        tranche.receipt_submitted_at = transfer.created_at
    transfer.bank_txn_id = (transfer.parsed_fields or {}).get("bank_txn_id") or transfer.bank_txn_id
    await transition_tranche(
        db, tranche, "ATTESTED", now=now, actor_id=staff_id, actor_role="ADMIN", reason=reason.strip(),
        confirm_deadline=now + dt.timedelta(hours=p.sme_confirm_hours),
    )
    tranche.review_cause = None
    transfer.confirmation_level = "ATTESTED"
    await refresh_window_fullness(db, window, now)
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type=EVENT_APPROVED, now=now, actor_id=staff_id,
        actor_role="ADMIN", reason=reason.strip(),
        payload={
            "transfer_id": str(transfer.id), "amount_vnd": tranche.amount_vnd, "late_reason": transfer.late_reason,
            "approved_before": cutoffs.receipt_deadline_at.isoformat(),
        },
    )
    create_notification(
        db, user_id=tranche.investor_id, event=EVENT_LATE_MONEY_APPROVED, entity_type="TRANCHE", entity_id=tranche.id,
        data={"window_id": str(window.id), "amount_vnd": tranche.amount_vnd},
    )
    await db.flush()
    return tranche


_DETAIL = {
    "NOT_LATE_MONEY": "This ticket is not clean late money waiting for a decision",
    "LATE_MONEY_APPROVAL_CLOSED": "Late money can be approved only until the receipt cut-off, while the round is open",
    "DUPLICATE_BANK_TXN": "This bank transaction already funds another ticket",
    "CAPACITY_EXCEEDED": "The round no longer has room for this ticket",
}
