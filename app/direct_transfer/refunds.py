"""Tracked refund transfers (funding-window.md rule 17). FundLok cannot pull money back: the SME refunds, and
the refund is tracked like any transfer (receipt, evidence log)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.calendar_utils import working_deadline
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import FundingWindow, Tranche, Transfer


async def create_refund_transfer(
    db: AsyncSession,
    *,
    tranche: Tranche,
    window: FundingWindow,
    now: dt.datetime,
    reason: str,
    params: DirectTransferParams | None = None,
) -> Transfer:
    """Idempotent: one REFUND transfer per tranche. `check_status = DUE` until the SME's refund receipt arrives."""
    p = resolve(params)
    existing = (
        await db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id, Transfer.kind == "REFUND"))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    refund = Transfer(
        kind="REFUND",
        tranche_id=tranche.id,
        from_account_id=window.sme_payment_account_id,
        to_account_id=tranche.investor_account_id,
        amount_vnd=tranche.amount_vnd,
        reference=tranche.reference_code,
        rail="MANUAL",
        confirmation_level="REPORTED",
        check_status="DUE",
        due_by=working_deadline(now, p.refund_working_days),
    )
    db.add(refund)
    await db.flush()
    await log_event(
        db, entity_type="TRANSFER", entity_id=refund.id, event_type="REFUND_RECORDED", now=now,
        reason=reason,
        payload={"tranche_id": str(tranche.id), "amount_vnd": tranche.amount_vnd, "due_by": refund.due_by.isoformat()},
    )
    return refund
