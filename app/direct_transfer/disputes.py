"""Dispute queue, two-step proof, staff decision (HANDOFF-04 B10). Spec: docs/specs/direct_transfer/disputes.md.

Burden of proof (E6, Q-026): the payer proves first with a bank statement, then the counterparty. Disbursement:
investor, then SME. Repayment: SME, then investor. FundLok never judges automatically: every outcome is a
recorded staff decision with a mandatory reason.
"""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.direct_transfer import cic
from app.direct_transfer.calendar_utils import calendar_deadline
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import Dispute, DisputeEvidence, FundingWindow, RepaymentDay, Tranche, Transfer
from app.direct_transfer.refunds import create_refund_transfer
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.windows import capacity_left, refresh_window_fullness

TRANCHE_OUTCOMES = ("RECEIVED", "NOT_RECEIVED")
DAY_OUTCOMES = ("PAID", "NOT_PAID")


async def _add_evidence(
    db: AsyncSession, dispute: Dispute, *, party: str, kind: str, statement: str | None, file_id: UUID | None,
    submitted_by: UUID, now: dt.datetime,
) -> DisputeEvidence:
    row = DisputeEvidence(
        dispute_id=dispute.id, party=party, kind=kind, statement=statement, file_id=file_id,
        submitted_by=submitted_by, submitted_at=now,
    )
    db.add(row)
    await db.flush()
    await log_event(
        db, entity_type="DISPUTE", entity_id=dispute.id, event_type="DISPUTE_EVIDENCE", now=now,
        actor_id=submitted_by, actor_role=party,
        payload={"kind": kind, "statement": statement, "file_id": str(file_id) if file_id else None},
    )
    return row


async def open_tranche_dispute(
    db: AsyncSession,
    *,
    tranche: Tranche,
    cause: str,
    now: dt.datetime,
    raised_by: UUID | None = None,
    claim_statement: str | None = None,
    claim_file_id: UUID | None = None,
    params: DirectTransferParams | None = None,
) -> Dispute:
    p = resolve(params)
    if cause not in ("RECEIPT_MISMATCH", "SME_DISPUTE", "LATE_RECEIPT"):
        raise bad_request("Invalid dispute cause")
    window = await db.get(FundingWindow, tranche.window_id)
    if cause == "SME_DISPUTE":
        next_party, deadline = "INVESTOR", calendar_deadline(now, days=p.dispute_response_days)
    else:
        next_party, deadline = "STAFF", None
    dispute = Dispute(
        subject_type="TRANCHE", tranche_id=tranche.id, window_id=window.id, cause=cause, raised_by=raised_by,
        status="OPEN", required_next_party=next_party, response_deadline=deadline,
    )
    db.add(dispute)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise conflict("DISPUTE_ALREADY_OPEN", "A dispute is already open for this ticket") from exc
    await log_event(
        db, entity_type="DISPUTE", entity_id=dispute.id, event_type="DISPUTE_OPENED", now=now, actor_id=raised_by,
        payload={"subject": "TRANCHE", "tranche_id": str(tranche.id), "cause": cause, "required_next_party": next_party},
    )
    if claim_statement or claim_file_id:
        await _add_evidence(
            db, dispute, party="SME" if raised_by == window.sme_user_id else "STAFF", kind="CLAIM", statement=claim_statement,
            file_id=claim_file_id, submitted_by=raised_by or window.sme_user_id, now=now,
        )
    return dispute


async def open_missed_day_dispute(
    db: AsyncSession,
    *,
    day: RepaymentDay,
    cause: str,
    raised_by: UUID,
    statement: str | None,
    file_id: UUID | None,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Dispute:
    p = resolve(params)
    if cause not in ("MISSED_DAY", "LATE_REPORT"):
        raise bad_request("Invalid dispute cause")
    tranche = await db.get(Tranche, day.tranche_id)
    if cause == "MISSED_DAY":
        next_party, deadline = "SME", calendar_deadline(now, days=p.dispute_response_days)  # SME proves first
    else:
        next_party, deadline = "STAFF", None
    dispute = Dispute(
        subject_type="REPAYMENT_DAY", repayment_day_id=day.id, window_id=tranche.window_id, cause=cause,
        raised_by=raised_by, status="OPEN", required_next_party=next_party, response_deadline=deadline,
    )
    db.add(dispute)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise conflict("DISPUTE_ALREADY_OPEN", "A dispute is already open for this day") from exc
    await log_event(
        db, entity_type="DISPUTE", entity_id=dispute.id, event_type="DISPUTE_OPENED", now=now, actor_id=raised_by,
        payload={"subject": "REPAYMENT_DAY", "repayment_day_id": str(day.id), "cause": cause, "required_next_party": next_party},
    )
    await _add_evidence(
        db, dispute, party="INVESTOR", kind="CLAIM", statement=statement, file_id=file_id, submitted_by=raised_by, now=now
    )
    return dispute


async def _parties(db: AsyncSession, dispute: Dispute) -> tuple[UUID, UUID]:
    """(investor_user_id, sme_user_id) of the dispute's subject."""
    window = await db.get(FundingWindow, dispute.window_id)
    if dispute.subject_type == "TRANCHE":
        tranche = await db.get(Tranche, dispute.tranche_id)
    else:
        day = await db.get(RepaymentDay, dispute.repayment_day_id)
        tranche = await db.get(Tranche, day.tranche_id)
    return tranche.investor_id, window.sme_user_id


def _proof_order(dispute: Dispute) -> tuple[str, str]:
    """(first proving party, second proving party)."""
    if dispute.cause == "SME_DISPUTE":
        return "INVESTOR", "SME"
    return "SME", "INVESTOR"  # MISSED_DAY: SME proves first


async def submit_dispute_evidence(
    db: AsyncSession,
    *,
    dispute_id: UUID,
    user_id: UUID,
    statement: str | None,
    file_id: UUID | None,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Dispute:
    p = resolve(params)
    dispute = (
        await db.execute(select(Dispute).where(Dispute.id == dispute_id).options(selectinload(Dispute.evidence)).with_for_update())
    ).scalar_one_or_none()
    if dispute is None:
        raise not_found("Dispute not found")
    investor_id, sme_id = await _parties(db, dispute)
    if user_id == investor_id:
        party = "INVESTOR"
    elif user_id == sme_id:
        party = "SME"
    else:
        raise not_found("Dispute not found")
    if dispute.status == "DECIDED":
        raise conflict("DISPUTE_ALREADY_DECIDED", "This dispute has been decided")
    if not (statement and statement.strip()) and file_id is None:
        raise bad_request("A statement or a file is required", "EVIDENCE_REQUIRED")
    if dispute.required_next_party != party:
        raise conflict("NOT_YOUR_TURN", "It is not your turn to respond")

    proofs = (
        await db.execute(
            select(DisputeEvidence.id).where(DisputeEvidence.dispute_id == dispute.id, DisputeEvidence.kind.in_(("PROOF", "COUNTER_PROOF")))
        )
    ).all()
    kind = "PROOF" if not proofs else "COUNTER_PROOF"
    await _add_evidence(db, dispute, party=party, kind=kind, statement=statement, file_id=file_id, submitted_by=user_id, now=now)
    first, second = _proof_order(dispute)
    if not proofs:
        dispute.required_next_party = second
        dispute.response_deadline = calendar_deadline(now, days=p.dispute_response_days)
    else:
        dispute.required_next_party = "STAFF"
        dispute.response_deadline = None
    await db.flush()
    return dispute


async def escalate_to_bank_trace(
    db: AsyncSession, *, dispute_id: UUID, staff_id: UUID, reason: str, now: dt.datetime
) -> Dispute:
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    dispute = await db.get(Dispute, dispute_id)
    if dispute is None:
        raise not_found("Dispute not found")
    if dispute.status != "OPEN":
        raise conflict("INVALID_DISPUTE_STATE", "The dispute cannot be escalated")
    dispute.status = "BANK_TRACE"
    dispute.required_next_party = "STAFF"
    dispute.response_deadline = None
    dispute.bank_trace_reason = reason
    dispute.bank_trace_at = now
    await db.flush()
    await log_event(
        db, entity_type="DISPUTE", entity_id=dispute.id, event_type="DISPUTE_EVIDENCE", now=now, actor_id=staff_id,
        actor_role="ADMIN", reason=reason, payload={"kind": "BANK_TRACE"},
    )
    return dispute


async def advance_dispute_deadlines(db: AsyncSession, *, now: dt.datetime) -> int:
    """A required party that has not answered by the deadline hands the turn to staff (never auto-decides)."""
    rows = (
        await db.execute(
            select(Dispute).where(
                Dispute.status == "OPEN",
                Dispute.required_next_party.in_(("INVESTOR", "SME")),
                Dispute.response_deadline.is_not(None),
                Dispute.response_deadline <= now,
            ).with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for dispute in rows:
        waiting = dispute.required_next_party
        dispute.required_next_party = "STAFF"
        dispute.response_deadline = None
        await log_event(
            db, entity_type="DISPUTE", entity_id=dispute.id, event_type="DISPUTE_EVIDENCE", now=now,
            payload={"kind": "NO_RESPONSE", "party": waiting},
        )
    await db.flush()
    return len(rows)


async def decide_dispute(
    db: AsyncSession,
    *,
    dispute_id: UUID,
    staff_id: UUID,
    outcome: str,
    reason: str,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
    actor_role: str = "ADMIN",
) -> Dispute:
    """`actor_role="INVESTOR"` (with `staff_id` the investor): the investor who reported a day missing confirms it
    arrived after all, closing their own report as PAID."""
    p = resolve(params)
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    dispute = (await db.execute(select(Dispute).where(Dispute.id == dispute_id).with_for_update())).scalar_one_or_none()
    if dispute is None:
        raise not_found("Dispute not found")
    if dispute.status == "DECIDED":
        raise conflict("DISPUTE_ALREADY_DECIDED", "This dispute has already been decided")
    valid = TRANCHE_OUTCOMES if dispute.subject_type == "TRANCHE" else DAY_OUTCOMES
    if outcome not in valid:
        raise bad_request(f"Outcome must be one of {', '.join(valid)} for this dispute", "INVALID_OUTCOME")

    if dispute.subject_type == "TRANCHE":
        await _apply_tranche_outcome(db, dispute, outcome=outcome, staff_id=staff_id, reason=reason, now=now, params=p)
    else:
        await _apply_day_outcome(
            db, dispute, outcome=outcome, staff_id=staff_id, reason=reason, now=now, params=p, actor_role=actor_role
        )

    dispute.status = "DECIDED"
    dispute.outcome = outcome
    dispute.decided_by = staff_id
    dispute.decided_at = now
    dispute.decision_reason = reason
    dispute.required_next_party = "STAFF"
    dispute.response_deadline = None
    await db.flush()
    await log_event(
        db, entity_type="DISPUTE", entity_id=dispute.id, event_type="DISPUTE_DECISION", now=now, actor_id=staff_id,
        actor_role=actor_role, reason=reason, payload={"outcome": outcome, "cause": dispute.cause},
    )
    return dispute


async def _apply_tranche_outcome(
    db: AsyncSession, dispute: Dispute, *, outcome: str, staff_id: UUID, reason: str, now: dt.datetime,
    params: DirectTransferParams,
) -> None:
    tranche = (await db.execute(select(Tranche).where(Tranche.id == dispute.tranche_id).with_for_update())).scalar_one()
    window = (await db.execute(select(FundingWindow).where(FundingWindow.id == tranche.window_id).with_for_update())).scalar_one()
    transfer = (
        await db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id, Transfer.kind == "DISBURSEMENT"))
    ).scalars().first()
    if tranche.status == "ACTIVE" or (tranche.status == "CONFIRMED" and outcome == "RECEIVED"):
        # A 30-day dispute after an automatic confirmation (A5): RECEIVED leaves the ticket as it is.
        # shortcut: NOT_RECEIVED on a ticket already in a loan is only recorded (there is no ACTIVE -> VOID); Finance
        # unwinds it under its interim policy until that unwind is specified.
        return
    if outcome == "NOT_RECEIVED":
        await transition_tranche(
            db, tranche, "VOID", now=now, actor_id=staff_id, actor_role="ADMIN", reason=reason, void_reason="NOT_RECEIVED"
        )
        await refresh_window_fullness(db, window, now)
        return

    # RECEIVED
    joined_already = window.status in ("REPAYING", "FAILED", "REPAID", "CANCELLED")
    if tranche.excluded_at_close or joined_already:
        # PROVISIONAL(Q-D7): resolved in the investor's favour but after the loan formed: it never joins; the SME refunds.
        await transition_tranche(
            db, tranche, "VOID", now=now, actor_id=staff_id, actor_role="ADMIN", reason=reason,
            void_reason="EXCLUDED_AT_CLOSE",
        )
        await create_refund_transfer(db, tranche=tranche, window=window, now=now, reason="EXCLUDED_AT_CLOSE", params=params)
        return
    if dispute.cause == "LATE_RECEIPT":
        if await capacity_left(db, window) < tranche.amount_vnd:
            raise conflict("CAPACITY_EXCEEDED", "The window no longer has capacity for this ticket")
    await transition_tranche(db, tranche, "CONFIRMED", now=now, actor_id=staff_id, actor_role="ADMIN", reason=reason)
    if transfer is not None:
        transfer.confirmation_level = "CONFIRMED"
    await refresh_window_fullness(db, window, now)


async def _apply_day_outcome(
    db: AsyncSession, dispute: Dispute, *, outcome: str, staff_id: UUID, reason: str, now: dt.datetime,
    params: DirectTransferParams, actor_role: str = "ADMIN",
) -> None:
    if outcome == "NOT_PAID":
        # The SME could not show bank proof: the day is LATE (late-status.md rule 5). A day reported after it settled
        # (LATE_REPORT) keeps its status: the ledger is append-only.
        day = await db.get(RepaymentDay, dispute.repayment_day_id)
        if day.status == "REPORTED_MISSED":
            day.status = "LATE"
            day.late_marked_at = now
            await log_event(
                db, entity_type="REPAYMENT_DAY", entity_id=day.id, event_type="REPAYMENT_DAY_LATE", now=now,
                actor_id=staff_id, actor_role=actor_role, reason=reason, payload={"dispute_id": str(dispute.id)},
            )
        return
    from app.direct_transfer.repayment import match_day  # local import: repayment imports this module

    await match_day(
        db, day_id=dispute.repayment_day_id, staff_id=staff_id, reason=reason, now=now, params=params,
        actor_role=actor_role,
    )
    day = await db.get(RepaymentDay, dispute.repayment_day_id)
    await cic.emit_missed_day_correction(db, day, now=now, stage="resolved_paid")


async def list_disputes(db: AsyncSession, *, only_open: bool = True) -> list[Dispute]:
    stmt = select(Dispute).options(selectinload(Dispute.evidence)).order_by(Dispute.created_at)
    if only_open:
        stmt = stmt.where(Dispute.status != "DECIDED")
    return list((await db.execute(stmt)).scalars())


async def get_dispute_for_party(db: AsyncSession, *, dispute_id: UUID, user_id: UUID, is_staff: bool = False) -> Dispute:
    dispute = (
        await db.execute(select(Dispute).where(Dispute.id == dispute_id).options(selectinload(Dispute.evidence)))
    ).scalar_one_or_none()
    if dispute is None:
        raise not_found("Dispute not found")
    if is_staff:
        return dispute
    investor_id, sme_id = await _parties(db, dispute)
    if user_id not in (investor_id, sme_id):
        raise not_found("Dispute not found")
    return dispute
