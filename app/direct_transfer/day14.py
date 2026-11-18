"""Day-14 transfer rule: QR codes, the on-time check, and the 15:00 no-show / late-money step.

Spec: docs/specs/direct_transfer/day14-transfer-rule.md (Loc, 18 Nov 2026), building on commit-then-accept-flow.md.

- Every QR a lender is shown has its own code: the ticket's reference plus 4 digits. It is valid 15 minutes, never
  past the reservation hold or (on a commit-then-accept window) 14:45 on Day 14. A new QR supersedes the last.
  This applies to every window, LEGACY included.
- On time = the receipt's memo carries a code FundLok issued for this ticket and the transfer time is inside that
  QR's validity. A receipt read to the minute only (OCR stores hh:mm:59) counts if that minute overlaps.
- On a window with Day-14 cut-offs (day14_times.window_cutoffs), late money is never returned by the SME: at 15:00
  the lender is locked and a top-up pledge records the amount (`process_day14_no_shows`). Lenders who sent nothing
  are locked as no-shows. On LEGACY windows the timing is recorded for staff and changes no outcome.
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
from app.direct_transfer.models import FundingWindow, LenderStanding, PaymentQr, TopUpPledge, Tranche, Transfer
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
# Windows the 15:00 step looks at: a commit-then-accept window is AWAITING_PAYMENT on Day 14.
_DAY14_WINDOW_STATES = ("OPEN", "FULL", "AWAITING_PAYMENT", "CLOSING")


# --------------------------------------------------------------------------- #
# QR codes
# --------------------------------------------------------------------------- #
def generate_qr_code(reference_code: str, rng: secrets.SystemRandom | None = None) -> str:
    """The ticket's reference + 4 random digits from a CSPRNG, e.g. FLAB12CD34 -> FLAB12CD344821."""
    chooser = (rng or secrets).choice  # type: ignore[union-attr]
    return reference_code + "".join(chooser("0123456789") for _ in range(QR_SUFFIX_DIGITS))


def code_in_memo(memo: str | None, reference_code: str) -> str | None:
    """The QR code a memo carries: the reference followed by 4 digits. Banks may drop spaces or punctuation and add
    their own words, so the memo is normalised and only the 4 characters after the reference are read."""
    text = normalize_memo(memo or "")
    ref = normalize_memo(reference_code)
    at = text.find(ref)
    if at < 0:
        return None
    suffix = text[at + len(ref): at + len(ref) + QR_SUFFIX_DIGITS]
    return ref + suffix if len(suffix) == QR_SUFFIX_DIGITS and suffix.isdigit() else None


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


async def current_qr(
    db: AsyncSession, *, tranche: Tranche, window: FundingWindow, now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> PaymentQr | None:
    """The QR to show: the latest one (even if expired; the lender then asks for a new one), or a first one issued
    now. None when no QR can be issued any more (the hold or the 14:45 cut-off has passed) and none exists."""
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
    tranche = (
        await db.execute(
            select(Tranche).where(Tranche.id == tranche_id, Tranche.investor_id == investor_id).with_for_update()
        )
    ).scalar_one_or_none()
    if tranche is None:
        raise not_found("Ticket not found")
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


def _minute_start(moment: dt.datetime) -> dt.datetime:
    return moment.replace(second=0, microsecond=0)


async def assess_payment_timing(
    db: AsyncSession, *, tranche: Tranche, window: FundingWindow, memo: str | None,
    transferred_at: dt.datetime | None, params: DirectTransferParams | None = None,
) -> PaymentTiming:
    qrs = list((await db.execute(select(PaymentQr).where(PaymentQr.tranche_id == tranche.id))).scalars())
    if not qrs:
        return NOT_JUDGED  # no QR code was ever shown for this ticket (issued before the rule): nothing to judge
    code = code_in_memo(memo, tranche.reference_code)
    qr = next((q for q in qrs if q.code == code), None) if code else None
    if qr is None:
        return PaymentTiming(LATE, NO_QR_CODE, code)
    if transferred_at is None or transferred_at.tzinfo is None:
        return PaymentTiming(None, None, qr.code)
    cutoffs = window_cutoffs(window, params)
    if cutoffs and _minute_start(transferred_at) > cutoffs.pay_deadline_at:
        return PaymentTiming(LATE, AFTER_CUTOFF, qr.code)
    if transferred_at < qr.issued_at - ISSUE_SKEW or _minute_start(transferred_at) > qr.expires_at:
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
# 15:00: no-shows and late money
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


async def lock_lender(
    db: AsyncSession, *, investor_id: UUID, window_id: UUID, now: dt.datetime, no_show: bool
) -> bool:
    """Lock the lender (Contract 4 Art. 5.4). Returns True if this locked them; idempotent per window."""
    standing = await db.get(LenderStanding, investor_id, with_for_update=True)
    if standing is None:
        standing = LenderStanding(investor_id=investor_id, status="GOOD", no_show_count=0)
        db.add(standing)
    if standing.status == "CLOSED":
        return False
    already = standing.status == "LOCKED" and standing.locked_window_id == window_id
    if already:
        return False
    if no_show:
        standing.no_show_count = (standing.no_show_count or 0) + 1
    newly = standing.status != "LOCKED"
    standing.status = "LOCKED"
    standing.locked_window_id = window_id
    if newly:
        standing.locked_at = now
        standing.unlocked_at = None
    await db.flush()
    return newly


async def _latest_disbursement(db: AsyncSession, tranche_id: UUID) -> Transfer | None:
    return (
        await db.execute(
            select(Transfer)
            .where(Transfer.tranche_id == tranche_id, Transfer.kind == "DISBURSEMENT")
            .order_by(Transfer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _received_amount(transfer: Transfer, tranche: Tranche) -> int:
    read = (transfer.parsed_fields or {}).get("amount_vnd")
    return read if isinstance(read, int) and read > 0 else tranche.amount_vnd


async def _record_late_money(
    db: AsyncSession, window: FundingWindow, tranche: Tranche, transfer: Transfer, now: dt.datetime,
    outcome: Day14Outcome,
) -> None:
    exists = (await db.execute(select(TopUpPledge.id).where(TopUpPledge.tranche_id == tranche.id))).first()
    if exists is not None:
        return
    if tranche.status in ("ATTESTED", "CONFIRMED", "ACTIVE"):
        return  # already accepted into the loan (staff or the SME decided): never undone here
    pledge = TopUpPledge(
        window_id=window.id, investor_id=tranche.investor_id, tranche_id=tranche.id, transfer_id=transfer.id,
        amount_vnd=_received_amount(transfer, tranche), reason=transfer.late_reason or EXPIRED_CODE, recorded_at=now,
    )
    db.add(pledge)
    if tranche.status == "SIGNED":
        await transition_tranche(
            db, tranche, "UNDER_REVIEW", now=now, actor_role=SYSTEM_ROLE, review_cause="LATE_MONEY",
            reason="Paid late on disbursement day",
        )
    if tranche.status == "EXPIRED":
        await transition_tranche(
            db, tranche, "UNDER_REVIEW", now=now, actor_role=SYSTEM_ROLE, review_cause="LATE_MONEY",
            reason="Paid late on disbursement day",
        )
    if tranche.status == "UNDER_REVIEW":
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


async def process_day14_no_shows(
    db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams | None = None
) -> Day14Outcome:
    """The 15:00 step, idempotent per ticket (safe to run again, e.g. after the 15:30 receipt cut-off):

    - a ticket still RESERVED or SIGNED with no receipt is a no-show: EXPIRED (NO_SHOW), lender locked;
    - a ticket whose receipt was judged LATE becomes a top-up pledge for the amount received: the ticket is VOID
      (LATE_MONEY_TOP_UP), the SME keeps the money, the lender is locked.
    Receipts still waiting for their fields are left for staff (their timing is not known yet)."""
    p = resolve(params)
    outcome = Day14Outcome()
    candidates = (
        await db.execute(
            select(FundingWindow).where(
                FundingWindow.pay_deadline_at.is_not(None), FundingWindow.status.in_(_DAY14_WINDOW_STATES)
            )
        )
    ).scalars().all()
    for candidate in candidates:
        cutoffs = window_cutoffs(candidate, p)
        if cutoffs is None or now < cutoffs.no_show_at:
            continue
        window = (
            await db.execute(select(FundingWindow).where(FundingWindow.id == candidate.id).with_for_update())
        ).scalar_one()
        tickets = (
            await db.execute(select(Tranche).where(Tranche.window_id == window.id).with_for_update())
        ).scalars().all()
        for tranche in tickets:
            if tranche.status in ("RESERVED", "SIGNED") and tranche.receipt_submitted_at is None:
                await transition_tranche(
                    db, tranche, "EXPIRED", now=now, actor_role=SYSTEM_ROLE, reason="NO_SHOW",
                    extra={"cause": "NO_SHOW"},
                )
                newly = await lock_lender(db, investor_id=tranche.investor_id, window_id=window.id, now=now, no_show=True)
                outcome.locked.append(LockedLender(tranche.investor_id, window.id, tranche.id, "NO_SHOW", newly))
                continue
            transfer = await _latest_disbursement(db, tranche.id)
            if transfer is not None and transfer.payment_timing == LATE:
                await _record_late_money(db, window, tranche, transfer, now, outcome)
    return outcome
