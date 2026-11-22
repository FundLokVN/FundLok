"""Tranche state machine (funding-window.md section 5; HANDOFF-04 section 4.3 plus E4 additions).

Every transition goes through `transition_tranche`, which validates the move, stamps the timestamp columns,
appends an `evidence_log` row and fires the CIC hooks. No other code assigns `tranche.status`.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import cic
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import Tranche

# (from, to) -> description (numbering as in funding-window.md section 5).
ALLOWED_TRANSITIONS: dict[tuple[str, str], str] = {
    ("RESERVED", "SIGNED"): "1 investor signs",
    ("SIGNED", "ATTESTED"): "2 receipt matches",
    ("ATTESTED", "CONFIRMED"): "3 SME confirms or dispute window lapses",
    ("CONFIRMED", "ACTIVE"): "4 window closes",
    ("RESERVED", "EXPIRED"): "5 hold lapses",
    ("SIGNED", "UNDER_REVIEW"): "6 receipt mismatch",
    ("ATTESTED", "UNDER_REVIEW"): "7 SME disputes",
    ("UNDER_REVIEW", "CONFIRMED"): "8 staff: received",
    ("UNDER_REVIEW", "VOID"): "9 staff: not received",
    ("SIGNED", "EXPIRED"): "10 hold lapses with no receipt (E4)",
    ("CONFIRMED", "VOID"): "11 below 50% / KYB fail / fraud before ACTIVE (E4, PROVISIONAL(Q-D8))",
    ("EXPIRED", "UNDER_REVIEW"): "12 late receipt (PROVISIONAL(Q-D3))",
    ("UNDER_REVIEW", "ATTESTED"): "13 staff approve late money before the receipt cut-off (late-money-approval.md)",
}

TRACKED_STATES_FOR_CAPACITY = ("RESERVED", "SIGNED", "ATTESTED", "CONFIRMED")


def assert_transition_allowed(current: str, target: str) -> None:
    if (current, target) not in ALLOWED_TRANSITIONS:
        raise DirectTransferError(400, f"Invalid state transition: {current} → {target}", "INVALID_TRANSITION")


async def transition_tranche(
    db: AsyncSession,
    tranche: Tranche,
    to_status: str,
    *,
    now: dt.datetime,
    actor_id=None,
    actor_role: str | None = None,
    reason: str | None = None,
    void_reason: str | None = None,
    review_cause: str | None = None,
    confirm_deadline: dt.datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> Tranche:
    from_status = tranche.status
    assert_transition_allowed(from_status, to_status)
    if (from_status, to_status) == ("UNDER_REVIEW", "ATTESTED") and tranche.review_cause != "LATE_MONEY":
        # Transition 13 exists only for staff-approved late money; every other review ends CONFIRMED or VOID.
        raise DirectTransferError(400, f"Invalid state transition: {from_status} → {to_status}", "INVALID_TRANSITION")

    tranche.status = to_status
    if to_status == "SIGNED":
        tranche.signed_at = now
    elif to_status == "ATTESTED":
        tranche.attested_at = now
        tranche.confirm_deadline = confirm_deadline
    elif to_status == "CONFIRMED":
        tranche.confirmed_at = now
        tranche.review_cause = None
    elif to_status == "ACTIVE":
        tranche.activated_at = now
    elif to_status == "EXPIRED":
        tranche.expired_at = now
    elif to_status == "UNDER_REVIEW":
        tranche.review_cause = review_cause
    elif to_status == "VOID":
        tranche.voided_at = now
        tranche.void_reason = void_reason
        tranche.review_cause = None
    await db.flush()

    await log_event(
        db,
        entity_type="TRANCHE",
        entity_id=tranche.id,
        event_type="TRANCHE_TRANSITION",
        now=now,
        actor_id=actor_id,
        actor_role=actor_role,
        reason=reason,
        payload={
            "from": from_status,
            "to": to_status,
            "void_reason": void_reason,
            "review_cause": review_cause,
            **(extra or {}),
        },
    )

    # CIC: follow the money (D-022). Real-time report at attestation; correction (new event) on a later void.
    if to_status == "ATTESTED":
        await cic.emit_attested(db, tranche, now=now)
    elif to_status == "VOID":
        await cic.emit_void(db, tranche, now=now, reason=void_reason or "VOID")
    return tranche
