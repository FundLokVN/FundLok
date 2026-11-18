"""Commit-then-accept funding flow, slice S2: commit, withdraw, SME decision, time-driven advance (ADR-011).

Spec: docs/specs/direct_transfer/commit-then-accept-flow.md sections 4.1, 4.3, 5 and 6 (rules 1 to 5, 12, 15, 16),
as amended by Money flow FINAL v1.0 (Loc, locked 17 Nov 2026), which prevails:
  - Days 0 to 12: a lender pledges by pressing Confirm; nothing is signed and no money moves, so the Lender Service
    Agreement is not a gate here (it is signed with the loan contract on the lender's first Day 14).
  - A lender may withdraw a pledge until the end of Day 12. Pledges close early when the target is reached and a
    withdrawal reopens them ("Pledge window after a withdrawal"). Day 13 and Day 14 never move.
A pledge is still kept as a signed record through the platform signer (evidence of the Confirm, not a contract).
At acceptance one tranche per lender is created from the lender's pledges (state RESERVED, hold until the Day-14
`pay_deadline_at`, 14:45).

Every function takes `now`; none reads the wall clock. `advance_window` is idempotent and time-driven: running it
twice, or from the scheduler and the staff button at once, changes nothing the second time.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import fees
from app.direct_transfer.accounts import get_usable_account
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event, store_evidence_file
from app.direct_transfer.models import Commitment, EvidenceLog, FundingWindow, LenderStanding, Tranche
from app.direct_transfer.refs import unique_reference_code
from app.direct_transfer.signing import Signer, SignerIdentity
from app.direct_transfer.storage import EvidenceStorage
from app.direct_transfer.windows import lock_window
from app.lending.models import Listing
from app.notifications.models import (
    EVENT_COMMITMENT_RECEIVED,
    EVENT_CONTRACT_SIGNING_OPEN,
    EVENT_WINDOW_ACCEPTED,
    EVENT_WINDOW_DECLINED,
)
from app.notifications.service import create_notification
from app.users.models import User
from app.utils.audit import append_audit

COMMIT_THEN_ACCEPT = "COMMIT_THEN_ACCEPT"
DECISIONS = ("ACCEPT", "DECLINE")


# --------------------------------------------------------------------------- #
# totals and views
# --------------------------------------------------------------------------- #
async def pledged_total(db: AsyncSession, window_id: UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Commitment.amount_vnd), 0)).where(
                    Commitment.window_id == window_id, Commitment.status == "PLEDGED"
                )
            )
        ).scalar_one()
    )


async def _my_pledged_total(db: AsyncSession, window_id: UUID, investor_id: UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Commitment.amount_vnd), 0)).where(
                    Commitment.window_id == window_id,
                    Commitment.investor_id == investor_id,
                    Commitment.status == "PLEDGED",
                )
            )
        ).scalar_one()
    )


@dataclasses.dataclass(frozen=True)
class CommitmentView:
    id: UUID
    window_id: UUID
    amount_vnd: int
    status: str
    committed_at: dt.datetime
    my_committed_total_vnd: int
    window_committed_total_vnd: int
    window_target_vnd: int


async def commitment_view(db: AsyncSession, commitment: Commitment, window: FundingWindow | None = None) -> CommitmentView:
    window = window or await db.get(FundingWindow, commitment.window_id)
    return CommitmentView(
        id=commitment.id,
        window_id=commitment.window_id,
        amount_vnd=commitment.amount_vnd,
        status=commitment.status,
        committed_at=commitment.committed_at,
        my_committed_total_vnd=await _my_pledged_total(db, commitment.window_id, commitment.investor_id),
        window_committed_total_vnd=await pledged_total(db, commitment.window_id),
        window_target_vnd=window.target_vnd,
    )


async def list_investor_commitments(db: AsyncSession, *, window_id: UUID, investor_id: UUID) -> list[CommitmentView]:
    """The lender's own commitments to the window, newest first."""
    window = await db.get(FundingWindow, window_id)
    if window is None:
        raise not_found("Window not found")
    rows = (
        await db.execute(
            select(Commitment)
            .where(Commitment.window_id == window_id, Commitment.investor_id == investor_id)
            .order_by(Commitment.committed_at.desc(), Commitment.created_at.desc(), Commitment.id)
        )
    ).scalars().all()
    mine = await _my_pledged_total(db, window_id, investor_id)
    total = await pledged_total(db, window_id)
    return [
        CommitmentView(
            id=c.id, window_id=c.window_id, amount_vnd=c.amount_vnd, status=c.status, committed_at=c.committed_at,
            my_committed_total_vnd=mine, window_committed_total_vnd=total, window_target_vnd=window.target_vnd,
        )
        for c in rows
    ]


# --------------------------------------------------------------------------- #
# commit (spec 4.1, rules 2 and 3)
# --------------------------------------------------------------------------- #
def _fingerprint(window_id: UUID, investor_id: UUID, amount_vnd: int, account_id: UUID) -> str:
    return f"{window_id}:{investor_id}:{amount_vnd}:{account_id}"


async def _commitment_by_key(db: AsyncSession, key: str) -> Commitment | None:
    return (await db.execute(select(Commitment).where(Commitment.idempotency_key == key))).scalar_one_or_none()


async def commit_to_window(
    db: AsyncSession,
    *,
    window_id: UUID,
    investor_id: UUID,
    amount_vnd: int,
    ack_risk_disclosure: bool,
    terms_version: str,
    current_terms_version: str,
    payment_account_id: UUID,
    idempotency_key: str,
    now: dt.datetime,
    signer: Signer,
    storage: EvidenceStorage,
    params: DirectTransferParams | None = None,
) -> Commitment:
    """Pledge `amount_vnd` to a COMMIT_THEN_ACCEPT window. No money moves, nothing is signed by the lender and no
    tranche is created.

    `current_terms_version` is decided by the caller (the investing terms live in app/market, which this module does
    not import). When the pledges reach the target the window stays OPEN but takes no more pledges (409
    WINDOW_FULL) until a withdrawal frees capacity; the commitment phase still ends at `commit_closes_at`."""
    p = resolve(params)
    if not idempotency_key:
        raise bad_request("Idempotency-Key is required", "IDEMPOTENCY_KEY_REQUIRED")
    fingerprint = _fingerprint(window_id, investor_id, amount_vnd, payment_account_id)
    existing = await _commitment_by_key(db, idempotency_key)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
        return existing
    if not isinstance(amount_vnd, int) or isinstance(amount_vnd, bool) or amount_vnd <= 0:
        raise bad_request("amount_vnd must be a positive integer", "INVALID_AMOUNT")
    if not ack_risk_disclosure:
        raise bad_request("The risk disclosure must be acknowledged", "RISK_ACK_REQUIRED")
    if terms_version != current_terms_version:
        raise conflict("TERMS_OUTDATED", "The investing terms have changed; read them again before investing")

    window = await lock_window(db, window_id)
    # Re-check idempotency after taking the lock (a concurrent request with the same key may have just committed).
    existing = await _commitment_by_key(db, idempotency_key)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
        return existing

    if window.sme_user_id == investor_id:
        raise not_found("Window not found")
    if window.flow != COMMIT_THEN_ACCEPT or window.status != "OPEN" or now >= window.commit_closes_at:
        raise conflict("WINDOW_NOT_OPEN", "The window is not open for commitments")

    standing = await db.get(LenderStanding, investor_id)  # no row means GOOD
    if standing is not None and standing.status == "LOCKED":
        raise conflict("LENDER_LOCKED", "Your account is locked after a missed payment; unlock it first")
    if standing is not None and standing.status == "CLOSED":
        raise conflict("LENDER_CLOSED", "Your account was closed after repeated missed payments")
    if await fees.investor_has_unpaid_invoice(db, investor_id, p, now=now):
        raise conflict("INVESTOR_FEE_UNPAID", "A FundLok invoice is unpaid")
    account = await get_usable_account(db, account_id=payment_account_id, user_id=investor_id, holder_role="INVESTOR")

    capacity = window.target_vnd - await pledged_total(db, window.id)
    if capacity <= 0:
        raise conflict(
            "WINDOW_FULL", "The round is fully pledged; it reopens if a lender withdraws before the end of Day 12"
        )
    if amount_vnd > capacity:
        raise conflict("CAPACITY_EXCEEDED", "The amount exceeds the remaining capacity")
    if amount_vnd < window.min_ticket_vnd and amount_vnd != capacity:
        raise conflict("BELOW_MIN_TICKET", "The amount is below the minimum ticket")

    commitment_id = uuid4()
    investor = await db.get(User, investor_id)
    # The pledge is a signed document through the existing signer (open question 4: no OTP per commitment).
    document = json.dumps(
        {
            "document": "FUNDLOK_INVESTOR_PLEDGE",
            "terms_version": terms_version,
            "window_id": str(window.id),
            "listing_id": str(window.listing_id),
            "commitment_id": str(commitment_id),
            "investor_id": str(investor_id),
            "payment_account_id": str(account.id),
            "amount_vnd": amount_vnd,
            "annual_rate_pct": str(window.annual_rate_pct),
            "term_months": window.term_months,
            "committed_at": now.isoformat(),
        },
        sort_keys=True,
    ).encode("utf-8")
    signature = await signer.sign(
        document=document,
        signer=SignerIdentity(user_id=investor_id, display_name=(investor.full_name or investor.email), role="INVESTOR"),
        now=now,
    )
    stored = await store_evidence_file(
        db, storage, owner_user_id=investor_id, purpose="SIGNED_DOCUMENT", content=signature.signed_content,
        content_type="application/pdf", filename=f"pledge-{commitment_id}.pdf", params=p,
    )
    commitment = Commitment(
        id=commitment_id,
        window_id=window.id,
        investor_id=investor_id,
        investor_account_id=account.id,
        investor_type=account.holder_type,
        amount_vnd=amount_vnd,
        status="PLEDGED",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        pledge_file_key=stored.storage_key,
        pledge_sha256=signature.sha256,
        pledge_certificate=signature.certificate,
        committed_at=now,
    )
    db.add(commitment)
    await db.flush()
    await log_event(
        db, entity_type="COMMITMENT", entity_id=commitment.id, event_type="COMMITMENT_CREATED", now=now,
        actor_id=investor_id, actor_role="INVESTOR", file_sha256=signature.sha256,
        payload={"window_id": str(window.id), "amount_vnd": amount_vnd, "terms_version": terms_version},
    )
    append_audit(
        db, entity_type="COMMITMENT", entity_id=commitment.id, action="COMMITMENT_CREATED", actor_id=investor_id,
        after_state={"window_id": str(window.id), "amount_vnd": amount_vnd, "status": "PLEDGED"},
    )
    create_notification(
        db, user_id=investor_id, event=EVENT_COMMITMENT_RECEIVED, entity_type="FUNDING_WINDOW", entity_id=window.id,
        data={"window_id": str(window.id), "commitment_id": str(commitment.id), "amount_vnd": amount_vnd},
    )
    await db.flush()
    if capacity - amount_vnd == 0:
        await log_event(
            db, entity_type="WINDOW", entity_id=window.id, event_type="TARGET_REACHED", now=now,
            payload={"pledged_vnd": window.target_vnd},
        )
    return commitment


# --------------------------------------------------------------------------- #
# withdraw (Money flow FINAL v1.0 section 1, Days 0 to 12; ops rule "Pledge window after a withdrawal")
# --------------------------------------------------------------------------- #
async def withdraw_commitment(
    db: AsyncSession, *, commitment_id: UUID, investor_id: UUID, now: dt.datetime
) -> Commitment:
    """The lender withdraws one of its pledges, until the end of Day 12 (`commit_closes_at`). The pledge becomes
    VOID (WITHDRAWN) and its amount is free again, which reopens a round that had reached its target. A replay on
    an already-withdrawn pledge returns it unchanged. 404 for someone else's pledge; 409 WITHDRAW_CLOSED once the
    commitment phase has ended (the pledge list is final from then); 409 COMMITMENT_NOT_PLEDGED for a pledge that
    is no longer live for another reason."""
    commitment = await db.get(Commitment, commitment_id)
    if commitment is None or commitment.investor_id != investor_id:
        raise not_found("Commitment not found")
    window = await lock_window(db, commitment.window_id)
    commitment = (
        await db.execute(select(Commitment).where(Commitment.id == commitment_id).with_for_update())
    ).scalar_one()
    if commitment.status == "VOID" and commitment.void_reason == "WITHDRAWN":
        return commitment
    if window.status != "OPEN" or now >= window.commit_closes_at:
        raise conflict("WITHDRAW_CLOSED", "Pledges are final from the end of Day 12")
    if commitment.status != "PLEDGED":
        raise conflict("COMMITMENT_NOT_PLEDGED", "This pledge is no longer live")
    commitment.status = "VOID"
    commitment.void_reason = "WITHDRAWN"
    await db.flush()
    await log_event(
        db, entity_type="COMMITMENT", entity_id=commitment.id, event_type="COMMITMENT_WITHDRAWN", now=now,
        actor_id=investor_id, actor_role="INVESTOR",
        payload={"window_id": str(window.id), "amount_vnd": commitment.amount_vnd},
    )
    append_audit(
        db, entity_type="COMMITMENT", entity_id=commitment.id, action="COMMITMENT_WITHDRAWN", actor_id=investor_id,
        before_state={"status": "PLEDGED"}, after_state={"status": "VOID", "void_reason": "WITHDRAWN"},
    )
    await db.flush()
    return commitment


# --------------------------------------------------------------------------- #
# end of the commit phase
# --------------------------------------------------------------------------- #
async def _end_commit_phase(db: AsyncSession, window: FundingWindow, *, now: dt.datetime, reason: str) -> None:
    """OPEN -> AWAITING_SME at `commit_closes_at` (00:00 on Day 13). The pledge list is final from here. The
    deadlines never move (Money flow FINAL v1.0: "Day 13 and Day 14 do not move")."""
    window.status = "AWAITING_SME"
    await db.flush()
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="COMMIT_PHASE_ENDED", now=now,
        payload={
            "reason": reason,
            "sme_deadline_at": window.sme_deadline_at.isoformat(),
            "pay_deadline_at": window.pay_deadline_at.isoformat(),
        },
    )


# --------------------------------------------------------------------------- #
# decision (spec 4.3, rule 4)
# --------------------------------------------------------------------------- #
async def _pledges_by_lender(db: AsyncSession, window_id: UUID) -> dict[UUID, list[Commitment]]:
    rows = (
        await db.execute(
            select(Commitment)
            .where(Commitment.window_id == window_id, Commitment.status == "PLEDGED")
            .order_by(Commitment.committed_at, Commitment.created_at, Commitment.id)
            .with_for_update()
        )
    ).scalars().all()
    grouped: dict[UUID, list[Commitment]] = {}
    for c in rows:
        grouped.setdefault(c.investor_id, []).append(c)
    return grouped


async def _apply_accept(
    db: AsyncSession, window: FundingWindow, *, decision: str, now: dt.datetime, decided_at: dt.datetime,
    actor_id: UUID | None,
) -> None:
    """Create one tranche per lender from its PLEDGED commitments and fold them (rule 5)."""
    grouped = await _pledges_by_lender(db, window.id)
    for investor_id, pledges in grouped.items():
        latest = pledges[-1]
        tranche = Tranche(
            window_id=window.id,
            investor_id=investor_id,
            investor_account_id=latest.investor_account_id,
            investor_type=latest.investor_type,
            amount_vnd=sum(c.amount_vnd for c in pledges),
            status="RESERVED",
            reference_code=await unique_reference_code(db),
            reserved_at=now,
            hold_expires_at=window.pay_deadline_at,
        )
        db.add(tranche)
        await db.flush()
        for c in pledges:
            c.status = "FOLDED"
            c.tranche_id = tranche.id
        await log_event(
            db, entity_type="TRANCHE", entity_id=tranche.id, event_type="TRANCHE_TRANSITION", now=now,
            actor_id=actor_id, actor_role="SME" if actor_id else None,
            payload={
                "from": None, "to": "RESERVED", "amount_vnd": tranche.amount_vnd, "reference_code": tranche.reference_code,
                "commitment_ids": [str(c.id) for c in pledges], "source": "WINDOW_ACCEPTED",
            },
        )
        data = {
            "window_id": str(window.id), "tranche_id": str(tranche.id), "amount_vnd": tranche.amount_vnd,
            "pay_deadline_at": window.pay_deadline_at.isoformat(),
        }
        create_notification(db, user_id=investor_id, event=EVENT_WINDOW_ACCEPTED, entity_type="FUNDING_WINDOW", entity_id=window.id, data=data)
        create_notification(
            db, user_id=investor_id, event=EVENT_CONTRACT_SIGNING_OPEN, entity_type="TRANCHE", entity_id=tranche.id, data=data
        )
    window.status = "AWAITING_PAYMENT"
    window.sme_decision = decision
    window.sme_decided_at = decided_at
    window.sme_decided_by = actor_id


async def _apply_decline(
    db: AsyncSession, window: FundingWindow, *, decision: str, now: dt.datetime, decided_at: dt.datetime,
    actor_id: UUID | None,
) -> None:
    """No tranche, no money, no fee: every pledge VOID, the window FAILED, the listing CANCELLED."""
    grouped = await _pledges_by_lender(db, window.id)
    for investor_id, pledges in grouped.items():
        for c in pledges:
            c.status = "VOID"
            c.void_reason = decision
        create_notification(
            db, user_id=investor_id, event=EVENT_WINDOW_DECLINED, entity_type="FUNDING_WINDOW", entity_id=window.id,
            data={"window_id": str(window.id), "decision": decision},
        )
    window.status = "FAILED"
    window.close_reason = decision
    window.closed_at = now
    window.sme_decision = decision
    window.sme_decided_at = decided_at
    window.sme_decided_by = actor_id
    listing = await db.get(Listing, window.listing_id)
    if listing is not None:
        listing.status = "CANCELLED"


async def _record_decision(
    db: AsyncSession, window: FundingWindow, *, decision: str, now: dt.datetime, actor_id: UUID | None,
    idempotency_key: str | None, pledged: int,
) -> None:
    payload = {"decision": decision, "pledged_vnd": pledged, "target_vnd": window.target_vnd, "status": window.status}
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_DECISION", now=now, actor_id=actor_id,
        actor_role="SME" if actor_id else None, payload=payload,
    )
    append_audit(
        db, entity_type="FUNDING_WINDOW", entity_id=window.id, action="WINDOW_DECISION", actor_id=actor_id,
        before_state={"status": "AWAITING_SME"}, after_state=payload,
    )


async def decide_window(
    db: AsyncSession,
    *,
    window_id: UUID,
    sme_user_id: UUID,
    decision: str,
    idempotency_key: str,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> FundingWindow:
    """The SME accepts or declines. Only in AWAITING_SME and before `sme_deadline_at`; ACCEPT below 50% is allowed."""
    resolve(params)
    if not idempotency_key:
        raise bad_request("Idempotency-Key is required", "IDEMPOTENCY_KEY_REQUIRED")
    if decision not in DECISIONS:
        raise bad_request("decision must be ACCEPT or DECLINE", "INVALID_DECISION")
    window = await lock_window(db, window_id)
    if window.sme_user_id != sme_user_id:
        raise DirectTransferError(403, "Only the business that owns this round can decide", "FORBIDDEN")
    if window.flow != COMMIT_THEN_ACCEPT:
        raise conflict("DECISION_NOT_OPEN", "This round has no decision step")

    previous = (
        await db.execute(
            select(EvidenceLog).where(
                EvidenceLog.entity_type == "WINDOW", EvidenceLog.entity_id == window.id, EvidenceLog.event_type == "WINDOW_DECISION"
            )
        )
    ).scalars().all()
    for event in previous:
        if event.payload.get("idempotency_key") == idempotency_key:
            if event.payload.get("decision") != decision:
                raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
            return window  # a replay returns the first result

    if window.status != "AWAITING_SME":
        raise conflict("DECISION_NOT_OPEN", "The round is not waiting for the business's decision")
    if now >= window.sme_deadline_at:
        raise conflict("DECISION_DEADLINE_PASSED", "The deadline has passed; the automatic result stands")
    pledged = await pledged_total(db, window.id)
    if decision == "ACCEPT":
        if pledged <= 0:
            raise conflict("NOTHING_TO_ACCEPT", "There are no commitments to accept")
        await _apply_accept(db, window, decision="ACCEPTED", now=now, decided_at=now, actor_id=sme_user_id)
    else:
        await _apply_decline(db, window, decision="DECLINED", now=now, decided_at=now, actor_id=sme_user_id)
    await db.flush()
    await _record_decision(db, window, decision=decision, now=now, actor_id=sme_user_id, idempotency_key=idempotency_key, pledged=pledged)
    return window


# --------------------------------------------------------------------------- #
# advance (rule 12)
# --------------------------------------------------------------------------- #
async def advance_window(
    db: AsyncSession, *, window_id: UUID, now: dt.datetime, params: DirectTransferParams | None = None
) -> FundingWindow:
    """Every due transition of a COMMIT_THEN_ACCEPT window, in order: end of the commit phase, then the automatic
    decision at `sme_deadline_at`. An explicit decision has already left AWAITING_SME, so it is never overridden.
    LEGACY windows are returned untouched."""
    resolve(params)
    window = await lock_window(db, window_id)
    if window.flow != COMMIT_THEN_ACCEPT:
        return window

    if window.status == "OPEN" and now >= window.commit_closes_at:
        await _end_commit_phase(db, window, now=now, reason="COMMIT_CLOSES_AT")

    if window.status == "AWAITING_SME" and window.sme_decision is None and now >= window.sme_deadline_at:
        pledged = await pledged_total(db, window.id)
        if pledged >= window.min_raise_vnd:
            await _apply_accept(db, window, decision="AUTO_ACCEPTED", now=now, decided_at=window.sme_deadline_at, actor_id=None)
        else:
            await _apply_decline(db, window, decision="AUTO_DECLINED", now=now, decided_at=window.sme_deadline_at, actor_id=None)
        await db.flush()
        await _record_decision(db, window, decision=window.sme_decision, now=now, actor_id=None, idempotency_key=None, pledged=pledged)
    return window


async def advance_all(db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams | None = None) -> int:
    """`advance_window` for every COMMIT_THEN_ACCEPT window with something that can come due."""
    ids = (
        await db.execute(
            select(FundingWindow.id).where(
                FundingWindow.flow == COMMIT_THEN_ACCEPT, FundingWindow.status.in_(("OPEN", "AWAITING_SME"))
            )
        )
    ).scalars().all()
    for window_id in ids:
        await advance_window(db, window_id=window_id, now=now, params=params)
    return len(ids)
