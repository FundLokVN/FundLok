"""Day-14 transfer rule: QR codes, the on-time check, and the 15:00 / 15:35 no-show and late-money steps.

Spec: docs/specs/direct_transfer/day14-transfer-rule.md (Loc, 18 Nov 2026), building on commit-then-accept-flow.md.

- Every QR a lender is shown has its own code: the ticket's reference plus 4 digits. It is valid 15 minutes, never
  past the reservation hold or (on a window with Day-14 cut-offs) 14:45 on Day 14. A new QR supersedes the last.
  This applies to every window, LEGACY included.
- On time = the receipt's memo carries a code FundLok issued for this ticket and the transfer time is inside that
  QR's validity. A receipt read to the minute only (OCR stores hh:mm:59) counts if that minute overlaps.
- On a window with Day-14 cut-offs (day14_times.window_cutoffs), late money is never returned by the SME:
  * 15:00 (`no_show_at`): every lender still unpaid, or whose money was late, is locked. Nothing else changes yet:
    receipts are taken until 15:30 and a lender who paid in time may still upload theirs.
  * After 15:30 (`receipt_deadline_at`, the 15:35 run): a ticket with still no receipt is a no-show (EXPIRED,
    counted); a receipt that matched in every way but time becomes a top-up pledge for the amount received (the
    ticket is VOID, the SME keeps the money); a lender whose receipt turned out fine is unlocked again. Anything
    else (a mismatch, an open dispute, a receipt still waiting to be read) is left to staff (spec D-16).
  On LEGACY windows the timing is recorded for staff and changes no outcome.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.day14_times import Day14Cutoffs, window_cutoffs
from app.direct_transfer.errors import DirectTransferError, conflict, not_found
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import (
    Commitment,
    Dispute,
    FundingWindow,
    LenderStanding,
    PaymentQr,
    TopUpPledge,
    Tranche,
    Transfer,
    WindowLoanContract,
)
from app.direct_transfer.receipts import normalize_memo
from app.direct_transfer.tranche_state import transition_tranche

ON_TIME = "ON_TIME"
LATE = "LATE"
EXPIRED_CODE = "EXPIRED_CODE"
NO_QR_CODE = "NO_QR_CODE"
AFTER_CUTOFF = "AFTER_CUTOFF"

QR_SUFFIX_DIGITS = 4
# A transfer stamped up to a minute before its QR was issued is a clock difference, not an older code.
ISSUE_SKEW = dt.timedelta(minutes=1)
SYSTEM_ROLE = "SYSTEM"
# The counted no-show that closes a lender's account for good (Money flow FINAL v1.0).
SECOND_NO_SHOW = 2
# Windows the Day-14 steps look at: a commit-then-accept window is AWAITING_PAYMENT on Day 14.
_DAY14_WINDOW_STATES = ("OPEN", "FULL", "AWAITING_PAYMENT", "CLOSING")


# --------------------------------------------------------------------------- #
# QR codes
# --------------------------------------------------------------------------- #
def generate_qr_code(reference_code: str, rng: secrets.SystemRandom | None = None) -> str:
    """The ticket's reference + 4 random digits from a CSPRNG, e.g. FLAB12CD34 -> FLAB12CD344821."""
    chooser = (rng or secrets).choice  # type: ignore[union-attr]
    return reference_code + "".join(chooser("0123456789") for _ in range(QR_SUFFIX_DIGITS))


def code_in_memo(memo: str | None, reference_code: str, issued: set[str] | frozenset[str] = frozenset()) -> str | None:
    """The QR code a memo carries: the reference followed by 4 digits. Banks may drop spaces or punctuation and add
    their own words, so the memo is normalised and every occurrence of the reference is read. A code FundLok issued
    wins; otherwise the first reference-plus-4-digits found (an unknown code), or None."""
    text = normalize_memo(memo or "")
    ref = normalize_memo(reference_code)
    found: list[str] = []
    at = text.find(ref)
    while at >= 0:
        suffix = text[at + len(ref): at + len(ref) + QR_SUFFIX_DIGITS]
        if len(suffix) == QR_SUFFIX_DIGITS and suffix.isdigit():
            found.append(ref + suffix)
        at = text.find(ref, at + 1)
    return next((code for code in found if code in issued), found[0] if found else None)


async def latest_qr(db: AsyncSession, tranche_id: UUID) -> PaymentQr | None:
    return (
        await db.execute(
            select(PaymentQr).where(PaymentQr.tranche_id == tranche_id).order_by(PaymentQr.issued_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


def _qr_cap(tranche: Tranche, cutoffs: Day14Cutoffs | None) -> dt.datetime:
    cap = tranche.hold_expires_at
    return min(cap, cutoffs.pay_deadline_at) if cutoffs else cap


def _check_payable(tranche: Tranche, cutoffs: Day14Cutoffs | None, now: dt.datetime) -> None:
    if tranche.status != "SIGNED" or tranche.receipt_submitted_at is not None:
        raise conflict("TRANCHE_NOT_PAYABLE", "This ticket is not waiting for a transfer")
    if cutoffs and now >= cutoffs.pay_deadline_at:
        raise conflict("PAY_DEADLINE_PASSED", "Payments for this round closed at 14:45 on disbursement day")
    if now >= tranche.hold_expires_at:
        raise conflict("HOLD_EXPIRED", "The reservation hold has expired")


async def _issue(
    db: AsyncSession, tranche: Tranche, cutoffs: Day14Cutoffs | None, now: dt.datetime, p: DirectTransferParams
) -> PaymentQr:
    expires_at = min(now + dt.timedelta(minutes=p.qr_validity_minutes), _qr_cap(tranche, cutoffs))
    for _ in range(10):
        code = generate_qr_code(tranche.reference_code)
        taken = (await db.execute(select(PaymentQr.id).where(PaymentQr.code == code))).first()
        if taken is None:
            break
    else:  # pragma: no cover - 10 collisions in 10,000 codes for one reference
        raise DirectTransferError(503, "Could not allocate a QR code", "REFERENCE_EXHAUSTED")
    qr = PaymentQr(tranche_id=tranche.id, code=code, issued_at=now, expires_at=expires_at)
    db.add(qr)
    await db.flush()
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="QR_ISSUED", now=now, actor_role="INVESTOR",
        payload={"code": code, "expires_at": expires_at.isoformat()},
    )
    return qr


async def _lock_ticket(db: AsyncSession, tranche_id: UUID, investor_id: UUID) -> Tranche:
    tranche = (
        await db.execute(
            select(Tranche).where(Tranche.id == tranche_id, Tranche.investor_id == investor_id).with_for_update()
        )
    ).scalar_one_or_none()
    if tranche is None:
        raise not_found("Ticket not found")
    return tranche


async def current_qr(
    db: AsyncSession, *, tranche: Tranche, window: FundingWindow, now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> PaymentQr | None:
    """The QR to show: the latest one (even if expired; the lender then asks for a new one), or a first one issued
    now. None when no QR can be issued any more (the hold or the 14:45 cut-off has passed) and none exists."""
    existing = await latest_qr(db, tranche.id)
    if existing is not None:
        return existing
    # Two first reads at once (two tabs) must not both issue: take the ticket's lock, then look again.
    tranche = await _lock_ticket(db, tranche.id, tranche.investor_id)
    existing = await latest_qr(db, tranche.id)
    if existing is not None:
        return existing
    cutoffs = window_cutoffs(window, params)
    try:
        _check_payable(tranche, cutoffs, now)
    except DirectTransferError:
        return None
    return await _issue(db, tranche, cutoffs, now, resolve(params))


async def issue_new_qr(
    db: AsyncSession, *, tranche_id: UUID, investor_id: UUID, now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> PaymentQr:
    """"Get a new QR": a new code valid 15 minutes. The previous QR is superseded. At most once a minute."""
    p = resolve(params)
    tranche = await _lock_ticket(db, tranche_id, investor_id)
    window = await db.get(FundingWindow, tranche.window_id)
    cutoffs = window_cutoffs(window, p)
    _check_payable(tranche, cutoffs, now)
    previous = await latest_qr(db, tranche.id)
    if previous is not None:
        wait = (previous.issued_at + dt.timedelta(seconds=p.qr_min_interval_seconds) - now).total_seconds()
        if wait > 0:
            err = DirectTransferError(429, "Wait a moment before asking for another QR", "QR_TOO_SOON")
            err.retry_after = math.ceil(wait)
            raise err
        if previous.superseded_at is None:
            previous.superseded_at = now
    return await _issue(db, tranche, cutoffs, now, p)


# --------------------------------------------------------------------------- #
# the on-time check
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class PaymentTiming:
    timing: str | None  # ON_TIME, LATE, or None (nothing to judge yet)
    late_reason: str | None
    qr_code: str | None

    @property
    def late(self) -> bool:
        return self.timing == LATE


NOT_JUDGED = PaymentTiming(None, None, None)


def _upper_bound(moment: dt.datetime) -> dt.datetime:
    """The earliest the transfer can have happened, for the "not after" tests. OCR stores a time read to the minute
    only as hh:mm:59 (bank_receipt.py), so such a time could be anywhere in that minute; a time with real seconds is
    taken as it is."""
    if moment.second == 59 and moment.microsecond == 0:
        return moment.replace(second=0)
    return moment


async def assess_payment_timing(
    db: AsyncSession, *, tranche: Tranche, window: FundingWindow, memo: str | None,
    transferred_at: dt.datetime | None, params: DirectTransferParams | None = None,
) -> PaymentTiming:
    cutoffs = window_cutoffs(window, params)
    qrs = list((await db.execute(select(PaymentQr).where(PaymentQr.tranche_id == tranche.id))).scalars())
    if not qrs and cutoffs is None:
        return NOT_JUDGED  # LEGACY ticket that never showed a QR code (paid before the rule): nothing to judge
    code = code_in_memo(memo, tranche.reference_code, {q.code for q in qrs})
    qr = next((q for q in qrs if q.code == code), None) if code else None
    if qr is None:
        # No QR shown, a bare reference, or a code FundLok never issued: late money on Day 14 (spec section 1).
        return PaymentTiming(LATE, NO_QR_CODE, code)
    if transferred_at is None or transferred_at.tzinfo is None:
        return PaymentTiming(None, None, qr.code)
    earliest = _upper_bound(transferred_at)
    if cutoffs and earliest > cutoffs.pay_deadline_at:
        return PaymentTiming(LATE, AFTER_CUTOFF, qr.code)
    if transferred_at < qr.issued_at - ISSUE_SKEW or earliest > qr.expires_at:
        return PaymentTiming(LATE, EXPIRED_CODE, qr.code)
    return PaymentTiming(ON_TIME, None, qr.code)


async def record_payment_timing(
    db: AsyncSession, *, tranche: Tranche, window: FundingWindow, transfer: Transfer, memo: str | None,
    transferred_at: dt.datetime | None, params: DirectTransferParams | None = None,
) -> PaymentTiming:
    timing = await assess_payment_timing(
        db, tranche=tranche, window=window, memo=memo, transferred_at=transferred_at, params=params
    )
    transfer.qr_code = timing.qr_code
    transfer.payment_timing = timing.timing
    transfer.late_reason = timing.late_reason
    return timing


def is_late_money(window: FundingWindow, timing: PaymentTiming, params: DirectTransferParams | None = None) -> bool:
    """Late money that becomes a top-up: only on a window with Day-14 cut-offs."""
    return timing.late and window_cutoffs(window, params) is not None


# --------------------------------------------------------------------------- #
# 15:00 lock, 15:35 decision
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class LockedLender:
    investor_id: UUID
    window_id: UUID
    tranche_id: UUID
    reason: str  # NO_SHOW or LATE_MONEY
    newly_locked: bool


@dataclasses.dataclass(frozen=True)
class RecordedPledge:
    pledge_id: UUID
    investor_id: UUID
    window_id: UUID
    tranche_id: UUID
    amount_vnd: int


@dataclasses.dataclass
class Day14Outcome:
    locked: list[LockedLender] = dataclasses.field(default_factory=list)
    pledges: list[RecordedPledge] = dataclasses.field(default_factory=list)
    released: list[UUID] = dataclasses.field(default_factory=list)  # lenders unlocked: their money was in time


async def lock_lender(
    db: AsyncSession, *, investor_id: UUID, window_id: UUID, now: dt.datetime, no_show: bool
) -> bool:
    """Lock the lender (Contract 4 Art. 5.4). Returns True if this locked them. `no_show` counts one more no-show
    (the caller does it once per ticket, when it expires the ticket)."""
    standing = await db.get(LenderStanding, investor_id, with_for_update=True)
    if standing is None:
        standing = LenderStanding(investor_id=investor_id, status="GOOD", no_show_count=0)
        db.add(standing)
    if standing.status == "CLOSED":
        return False
    if no_show:
        standing.no_show_count = (standing.no_show_count or 0) + 1
    if no_show and standing.no_show_count >= SECOND_NO_SHOW:
        # Money flow FINAL v1.0: "A second no-show closes the account for good." Unlocking is no longer possible.
        standing.status = "CLOSED"
        standing.closed_at = now
        standing.locked_window_id = window_id
        standing.locked_at = standing.locked_at or now
        await db.flush()
        return True
    newly = standing.status != "LOCKED"
    standing.status = "LOCKED"
    standing.locked_window_id = window_id
    if newly:
        standing.locked_at = now
        standing.unlocked_at = None
    await db.flush()
    return newly


async def void_no_show(db: AsyncSession, *, tranche: Tranche, now: dt.datetime) -> None:
    """A no-show's loan never takes effect: its folded pledges and its Day-13 loan contract become VOID (NO_SHOW)."""
    for c in (await db.execute(select(Commitment).where(Commitment.tranche_id == tranche.id))).scalars():
        if c.status != "VOID":
            c.status, c.void_reason = "VOID", "NO_SHOW"
    contract = (
        await db.execute(
            select(WindowLoanContract).where(
                WindowLoanContract.window_id == tranche.window_id,
                WindowLoanContract.investor_id == tranche.investor_id,
                WindowLoanContract.status != "VOID",
            )
        )
    ).scalar_one_or_none()
    if contract is not None:
        contract.status, contract.void_reason = "VOID", "NO_SHOW"
    await db.flush()


async def _release_if_only_this_window(
    db: AsyncSession, *, investor_id: UUID, window_id: UUID, now: dt.datetime
) -> bool:
    """Unlock a lender locked at 15:00 for this round whose receipt then proved them in time. Not if anything else
    on the round still holds them (a no-show, a pledge), nor if another round locked them."""
    standing = await db.get(LenderStanding, investor_id, with_for_update=True)
    if standing is None or standing.status != "LOCKED" or standing.locked_window_id != window_id:
        return False
    held = (
        await db.execute(
            select(Tranche.id).where(
                Tranche.window_id == window_id,
                Tranche.investor_id == investor_id,
                (Tranche.status.in_(("RESERVED", "SIGNED", "UNDER_REVIEW")))
                | ((Tranche.status == "EXPIRED") & Tranche.receipt_submitted_at.is_(None))
                | ((Tranche.status == "VOID") & (Tranche.void_reason == "LATE_MONEY_TOP_UP")),
            )
        )
    ).first()
    if held is not None:
        return False
    standing.status = "GOOD"
    standing.unlocked_at = now
    await db.flush()
    return True


async def _latest_receipt(db: AsyncSession, tranche_id: UUID) -> Transfer | None:
    """The ticket's latest disbursement receipt that staff have not rejected."""
    return (
        await db.execute(
            select(Transfer)
            .where(
                Transfer.tranche_id == tranche_id,
                Transfer.kind == "DISBURSEMENT",
                Transfer.check_status != "REJECTED",
            )
            .order_by(Transfer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _has_open_dispute(db: AsyncSession, tranche_id: UUID) -> bool:
    return (
        await db.execute(select(Dispute.id).where(Dispute.tranche_id == tranche_id, Dispute.status != "DECIDED"))
    ).first() is not None


def _received_amount(transfer: Transfer, tranche: Tranche) -> int:
    read = (transfer.parsed_fields or {}).get("amount_vnd")
    return read if isinstance(read, int) and read > 0 else tranche.amount_vnd


async def _record_late_money(
    db: AsyncSession, window: FundingWindow, tranche: Tranche, transfer: Transfer, now: dt.datetime,
    outcome: Day14Outcome,
) -> None:
    pledge = TopUpPledge(
        window_id=window.id, investor_id=tranche.investor_id, tranche_id=tranche.id, transfer_id=transfer.id,
        amount_vnd=_received_amount(transfer, tranche), reason=transfer.late_reason or EXPIRED_CODE, recorded_at=now,
    )
    db.add(pledge)
    await transition_tranche(
        db, tranche, "VOID", now=now, actor_role=SYSTEM_ROLE, void_reason="LATE_MONEY_TOP_UP",
        reason="Late money kept by the SME as the lender's top-up",
    )
    await db.flush()
    newly = await lock_lender(db, investor_id=tranche.investor_id, window_id=window.id, now=now, no_show=False)
    outcome.pledges.append(
        RecordedPledge(
            pledge_id=pledge.id, investor_id=tranche.investor_id, window_id=window.id, tranche_id=tranche.id,
            amount_vnd=pledge.amount_vnd,
        )
    )
    outcome.locked.append(LockedLender(tranche.investor_id, window.id, tranche.id, "LATE_MONEY", newly))
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="TOP_UP_PLEDGE_RECORDED", now=now,
        actor_role=SYSTEM_ROLE,
        payload={"pledge_id": str(pledge.id), "amount_vnd": pledge.amount_vnd, "reason": pledge.reason},
    )


async def day14_windows_due(
    db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams | None = None
) -> list[UUID]:
    """Windows whose 15:00 has come, oldest first (a stable order, so two runs take locks the same way)."""
    candidates = (
        await db.execute(
            select(FundingWindow)
            .where(FundingWindow.pay_deadline_at.is_not(None), FundingWindow.status.in_(_DAY14_WINDOW_STATES))
            .order_by(FundingWindow.id)
        )
    ).scalars().all()
    due = []
    for window in candidates:
        cutoffs = window_cutoffs(window, params)
        if cutoffs is not None and now >= cutoffs.no_show_at:
            due.append(window.id)
    return due


async def process_day14_window(
    db: AsyncSession, *, window_id: UUID, now: dt.datetime, params: DirectTransferParams | None = None,
    outcome: Day14Outcome | None = None,
) -> Day14Outcome:
    """One window's Day-14 step, idempotent per ticket. The caller commits (one window per transaction).

    Tickets are locked in id order, skipping any another transaction holds (an upload, a staff decision): a rerun
    picks them up. The window row itself is not locked, so this never waits on the ticket -> window order that
    uploads and staff decisions use."""
    p = resolve(params)
    outcome = outcome if outcome is not None else Day14Outcome()
    window = await db.get(FundingWindow, window_id)
    cutoffs = window_cutoffs(window, p)
    if cutoffs is None or now < cutoffs.no_show_at:
        return outcome
    decide = now >= cutoffs.receipt_deadline_at
    tickets = (
        await db.execute(
            select(Tranche)
            .where(Tranche.window_id == window.id)
            .order_by(Tranche.id)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    pledged = set(
        (await db.execute(select(TopUpPledge.tranche_id).where(TopUpPledge.window_id == window.id))).scalars()
    )
    for tranche in tickets:
        unpaid = tranche.status in ("RESERVED", "SIGNED") and tranche.receipt_submitted_at is None
        if unpaid:
            if decide:
                # 15:30 has passed with no receipt: a no-show, counted once (the ticket leaves SIGNED here).
                await transition_tranche(
                    db, tranche, "EXPIRED", now=now, actor_role=SYSTEM_ROLE, reason="NO_SHOW",
                    extra={"cause": "NO_SHOW"},
                )
                await void_no_show(db, tranche=tranche, now=now)
            newly = await lock_lender(
                db, investor_id=tranche.investor_id, window_id=window.id, now=now, no_show=decide
            )
            outcome.locked.append(LockedLender(tranche.investor_id, window.id, tranche.id, "NO_SHOW", newly))
            continue

        receipt = await _latest_receipt(db, tranche.id)
        late = receipt is not None and receipt.payment_timing == LATE
        if late and tranche.status in ("SIGNED", "UNDER_REVIEW"):
            if (
                decide
                and tranche.id not in pledged
                and tranche.status == "UNDER_REVIEW"
                and tranche.review_cause == "LATE_MONEY"
                and receipt.check_status == "MATCHED"
                and not await _has_open_dispute(db, tranche.id)
            ):
                await _record_late_money(db, window, tranche, receipt, now, outcome)
            else:
                # Late, but not (yet) a clean top-up: locked; staff settle the rest (spec D-16).
                newly = await lock_lender(
                    db, investor_id=tranche.investor_id, window_id=window.id, now=now, no_show=False
                )
                outcome.locked.append(LockedLender(tranche.investor_id, window.id, tranche.id, "LATE_MONEY", newly))
            continue

        if decide and tranche.status in ("ATTESTED", "CONFIRMED", "ACTIVE"):
            if await _release_if_only_this_window(db, investor_id=tranche.investor_id, window_id=window.id, now=now):
                outcome.released.append(tranche.investor_id)
    return outcome


async def process_day14_no_shows(
    db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams | None = None
) -> Day14Outcome:
    """Every due window in one go (tests and one-off use). The scheduled job runs `process_day14_window` per window
    with a commit in between (app/scheduled_jobs/day14.py)."""
    outcome = Day14Outcome()
    for window_id in await day14_windows_due(db, now=now, params=params):
        await process_day14_window(db, window_id=window_id, now=now, params=params, outcome=outcome)
    return outcome
