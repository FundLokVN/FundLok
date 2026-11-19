"""CIC event log writers (HANDOFF-04 B11). Spec: docs/specs/direct_transfer/cic-event-log.md.

Log now, send later: there is NO sender and this module imports no HTTP client. Events are idempotent by
`emit_key`; corrections are new events; `sent_at` stays NULL until a future connection calls `mark_sent`.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.models import CicEvent, FundingWindow, RepaymentDay, Tranche


async def emit_cic_event(
    db: AsyncSession,
    *,
    event_type: str,
    window_id: UUID,
    emit_key: str,
    payload: dict[str, Any],
    occurred_at: dt.datetime,
    tranche_id: UUID | None = None,
    repayment_day_id: UUID | None = None,
) -> tuple[CicEvent, bool]:
    """Insert the event unless one with this emit_key exists. Returns (event, created)."""
    stmt = (
        pg_insert(CicEvent)
        .values(
            event_type=event_type,
            window_id=window_id,
            emit_key=emit_key,
            payload=payload,
            occurred_at=occurred_at,
            tranche_id=tranche_id,
            repayment_day_id=repayment_day_id,
        )
        .on_conflict_do_nothing(index_elements=["emit_key"])
        .returning(CicEvent.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    event = (await db.execute(select(CicEvent).where(CicEvent.emit_key == emit_key))).scalar_one()
    return event, inserted is not None


async def _common_payload(db: AsyncSession, tranche: Tranche) -> tuple[FundingWindow, dict[str, Any]]:
    window = await db.get(FundingWindow, tranche.window_id)
    return window, {
        "window_id": str(window.id),
        "tranche_id": str(tranche.id),
        "contract_id": str(window.contract_id),
        "borrower_project_id": str(window.project_id),
        "lender_user_id": str(tranche.investor_id),
        "agreement_ref": tranche.reference_code,
    }


async def emit_attested(db: AsyncSession, tranche: Tranche, *, now: dt.datetime) -> tuple[CicEvent, bool]:
    if tranche.status != "ATTESTED":
        raise ValueError("emit_attested requires an ATTESTED tranche")
    window, payload = await _common_payload(db, tranche)
    payload.update(
        {
            "amount_vnd": tranche.amount_vnd,
            "annual_rate_pct": str(window.annual_rate_pct),
            "term_months": window.term_months,
            "transferred_at": (tranche.attested_at or now).isoformat(),
        }
    )
    return await emit_cic_event(
        db,
        event_type="ATTESTED",
        window_id=window.id,
        emit_key=f"attested:{tranche.id}",
        payload=payload,
        occurred_at=tranche.attested_at or now,
        tranche_id=tranche.id,
    )


async def emit_void(db: AsyncSession, tranche: Tranche, *, now: dt.datetime, reason: str) -> tuple[CicEvent, bool] | None:
    """Correction for a tranche that was reported at attestation. A tranche never attested has nothing to correct."""
    attested = (
        await db.execute(select(CicEvent.id).where(CicEvent.emit_key == f"attested:{tranche.id}"))
    ).first()
    if attested is None:
        return None
    window, payload = await _common_payload(db, tranche)
    payload.update({"reason": reason, "amount_vnd": tranche.amount_vnd})
    return await emit_cic_event(
        db,
        event_type="VOID",
        window_id=window.id,
        emit_key=f"void:{tranche.id}",
        payload=payload,
        occurred_at=now,
        tranche_id=tranche.id,
    )


async def emit_missed_day_correction(
    db: AsyncSession, day: RepaymentDay, *, now: dt.datetime, stage: str
) -> tuple[CicEvent, bool]:
    if stage not in ("reported", "resolved_paid"):
        raise ValueError("stage must be 'reported' or 'resolved_paid'")
    tranche = await db.get(Tranche, day.tranche_id)
    window, payload = await _common_payload(db, tranche)
    payload.update(
        {
            "due_date": day.due_date.isoformat(),
            "stage": stage,
            "principal_vnd": day.principal_vnd,
            "interest_vnd": day.interest_vnd,
        }
    )
    return await emit_cic_event(
        db,
        event_type="MISSED_DAY_CORRECTION",
        window_id=window.id,
        emit_key=f"missed:{day.id}:{stage}",
        payload=payload,
        occurred_at=now,
        tranche_id=tranche.id,
        repayment_day_id=day.id,
    )


async def emit_daily_balances(db: AsyncSession, *, on_date: dt.date, now: dt.datetime) -> int:
    """One DAILY_BALANCE per ACTIVE tranche per day, counting PRESUMED_PAID and MATCHED days as paid and excluding
    REPORTED_MISSED. Returns the number of events newly created."""
    tranches = (await db.execute(select(Tranche).where(Tranche.status == "ACTIVE"))).scalars().all()
    created = 0
    for tranche in tranches:
        days = (
            await db.execute(
                select(RepaymentDay).where(
                    RepaymentDay.tranche_id == tranche.id,
                    RepaymentDay.kind == "INSTALMENT",
                    RepaymentDay.due_date <= on_date,
                )
            )
        ).scalars().all()
        if not days:
            continue
        paid = [d for d in days if d.status in ("PRESUMED_PAID", "MATCHED")]
        missed = [d for d in days if d.status == "REPORTED_MISSED"]
        unpaid_interest = sum(d.interest_vnd for d in days if d.status in ("DUE", "LATE", "REPORTED_MISSED"))
        outstanding = tranche.amount_vnd - sum(d.principal_vnd for d in paid)
        window, payload = await _common_payload(db, tranche)
        payload.update(
            {
                "date": on_date.isoformat(),
                "outstanding_principal_vnd": outstanding,
                "unpaid_interest_vnd": unpaid_interest,
                "paid_days": len(paid),
                "missed_days": len(missed),
            }
        )
        _, was_created = await emit_cic_event(
            db,
            event_type="DAILY_BALANCE",
            window_id=window.id,
            emit_key=f"daily:{tranche.id}:{on_date.isoformat()}",
            payload=payload,
            occurred_at=now,
            tranche_id=tranche.id,
        )
        created += 1 if was_created else 0
    return created


async def pending_events(db: AsyncSession, *, limit: int = 500) -> list[CicEvent]:
    result = await db.execute(
        select(CicEvent).where(CicEvent.sent_at.is_(None)).order_by(CicEvent.occurred_at, CicEvent.created_at).limit(limit)
    )
    return list(result.scalars())


async def mark_sent(db: AsyncSession, event_ids: list[UUID], *, sent_at: dt.datetime) -> int:
    """The only permitted mutation of cic_events (NULL -> value); the seam a future CIC sender uses."""
    events = (await db.execute(select(CicEvent).where(CicEvent.id.in_(event_ids)))).scalars().all()
    for event in events:
        event.sent_at = sent_at
    await db.flush()
    return len(events)
