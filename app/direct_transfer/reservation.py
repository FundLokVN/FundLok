"""Ticket reservation (HANDOFF-04 B5). Spec: funding-window.md rules 3-7, 9.

`SELECT ... FOR UPDATE` on the window row serialises concurrent reservations so capacity never exceeds the
target. Idempotent by `Idempotency-Key`.
"""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import fees
from app.direct_transfer.accounts import get_usable_account
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import Tranche
from app.direct_transfer.refs import unique_reference_code
from app.direct_transfer.windows import capacity_left, check_borrower_cap, lock_window


def _fingerprint(window_id: UUID, investor_id: UUID, amount_vnd: int) -> str:
    return f"{window_id}:{investor_id}:{amount_vnd}"


async def reserve_ticket(
    db: AsyncSession,
    *,
    window_id: UUID,
    investor_id: UUID,
    amount_vnd: int,
    ack_risk_disclosure: bool,
    payment_account_id: UUID,
    idempotency_key: str,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Tranche:
    p = resolve(params)
    if not idempotency_key:
        raise bad_request("Idempotency-Key is required", "IDEMPOTENCY_KEY_REQUIRED")
    fingerprint = _fingerprint(window_id, investor_id, amount_vnd)
    existing = (await db.execute(select(Tranche).where(Tranche.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
        return existing
    if not isinstance(amount_vnd, int) or isinstance(amount_vnd, bool) or amount_vnd <= 0:
        raise bad_request("amount_vnd must be a positive integer", "INVALID_AMOUNT")
    if not ack_risk_disclosure:
        raise bad_request("The risk disclosure must be acknowledged", "RISK_ACK_REQUIRED")

    window = await lock_window(db, window_id)
    # Re-check idempotency after taking the lock (a concurrent request with the same key may have just committed).
    existing = (await db.execute(select(Tranche).where(Tranche.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
        return existing

    if window.sme_user_id == investor_id:
        raise not_found("Window not found")
    if window.flow == "COMMIT_THEN_ACCEPT":
        raise conflict("USE_COMMITMENTS", "This window takes commitments, not reservations")
    if window.status != "OPEN" or now >= window.window_end:
        raise conflict("WINDOW_NOT_OPEN", "The window is not open for reservations")
    if await fees.investor_has_unpaid_invoice(db, investor_id, p, now=now):
        raise conflict("INVESTOR_FEE_UNPAID", "A FundLok invoice is unpaid")
    account = await get_usable_account(db, account_id=payment_account_id, user_id=investor_id, holder_role="INVESTOR")

    live = (
        await db.execute(
            select(Tranche.id).where(
                Tranche.window_id == window.id,
                Tranche.investor_id == investor_id,
                Tranche.status.in_(("RESERVED", "SIGNED", "ATTESTED", "CONFIRMED", "UNDER_REVIEW", "ACTIVE")),
            )
        )
    ).first()
    if live is not None:
        raise conflict("ALREADY_HAS_LIVE_TRANCHE", "You already have a live ticket on this listing")

    capacity = await capacity_left(db, window)
    if capacity <= 0 or amount_vnd > capacity:
        raise conflict("CAPACITY_EXCEEDED", "The amount exceeds the remaining capacity")
    if amount_vnd < window.min_ticket_vnd:
        # Last-ticket rule (I-6, PROVISIONAL(Q-D3)): the final ticket may be below the minimum when it takes
        # exactly the remaining capacity.
        if not (capacity < window.min_ticket_vnd and amount_vnd == capacity):
            raise conflict("BELOW_MIN_TICKET", "The amount is below the minimum ticket")

    cap = await check_borrower_cap(
        db, window_id=window.id, project_id=window.project_id, target_vnd=window.target_vnd,
        declared_outside_debt_vnd=window.declared_outside_debt_vnd, now=now, params=p,
    )

    tranche = Tranche(
        window_id=window.id,
        investor_id=investor_id,
        investor_account_id=account.id,
        investor_type=account.holder_type,
        amount_vnd=amount_vnd,
        status="RESERVED",
        reference_code=await unique_reference_code(db),
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        reserved_at=now,
        hold_expires_at=now + dt.timedelta(hours=p.hold_hours),
    )
    db.add(tranche)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise conflict("ALREADY_HAS_LIVE_TRANCHE", "You already have a live ticket on this listing") from exc

    if capacity - amount_vnd == 0:
        window.status = "FULL"
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="TRANCHE_TRANSITION", now=now,
        actor_id=investor_id, actor_role="INVESTOR",
        payload={"from": None, "to": "RESERVED", "amount_vnd": amount_vnd, "reference_code": tranche.reference_code},
    )
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="CAP_CHECK", now=now,
        actor_id=investor_id, actor_role="INVESTOR", payload=cap,
    )
    return tranche
