"""Window close: triggers, wait, 50% rule, refunds, common start date, stub interest, facility (HANDOFF-04 B8).

Spec: docs/specs/direct_transfer/funding-window.md rules 11-14, section 5. Idempotent and clock-injected:
`advance_window(now)` is safe to call repeatedly (ARQ job or test) and never sleeps.
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import fees
from app.direct_transfer.calendar_utils import (
    calendar_deadline,
    count_business_days,
    default_calendar,
    first_working_day_after,
    ict_date,
)
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.disputes import open_tranche_dispute
from app.direct_transfer.errors import DirectTransferError, conflict, not_found
from app.direct_transfer.evidence import log_event
from app.direct_transfer.ledger_posting import post_tranche_claims
from app.direct_transfer.models import FundingWindow, Tranche, Transfer
from app.direct_transfer.refs import unique_reference_code
from app.direct_transfer.refunds import create_refund_transfer
from app.direct_transfer.repayment import generate_repayment_days, split_gross
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.tranches import confirm_lapsed_tranches
from app.direct_transfer.windows import confirmed_total, lock_window
from app.lending.models import Contract, Holding, Listing
from app.repayment_schedule.calendar import BusinessDayCalendar
from app.repayment_schedule.origination import compute_origination, originate_facility
from app.repayment_schedule.state_machine import apply_transition

LIVE_TRANCHE_STATES = ("RESERVED", "SIGNED", "ATTESTED", "CONFIRMED", "UNDER_REVIEW")


def compute_stub_interest(amount_vnd: int, annual_rate_pct: Decimal, stub_days: int, basis_days: int) -> int:
    """P x r x business_days / 252, rounded half up to a whole dong (E3, PROVISIONAL(Q-D5))."""
    value = Decimal(amount_vnd) * annual_rate_pct / Decimal(100) * Decimal(stub_days) / Decimal(basis_days)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def investor_rate_pct(loan_rate_pct: Decimal, term_months: int, params: DirectTransferParams) -> Decimal:
    """The yearly rate the loan is formed at: the graded rate on the window, in full (D-044, Appendix M 2.4).

    Investors earn the full graded rate; the 2.5% yearly lender service fee is separate, invoiced, and never in
    these days. `term_months` and `params` are kept so the callers (here and app/market) need no change."""
    return Decimal(loan_rate_pct)


async def _tranches(db: AsyncSession, window_id: UUID, *, lock: bool = True) -> list[Tranche]:
    stmt = select(Tranche).where(Tranche.window_id == window_id).order_by(Tranche.confirmed_at.nulls_last(), Tranche.id)
    if lock:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt)).scalars())


async def begin_close(
    db: AsyncSession, window: FundingWindow, *, now: dt.datetime, reason: str, params: DirectTransferParams,
    calendar: BusinessDayCalendar | None = None,
) -> None:
    window.status = "CLOSING"
    window.close_reason = reason
    window.closing_started_at = now
    window.close_wait_until = calendar_deadline(now, days=params.close_wait_days)
    # Tickets that never reached a receipt cannot be accepted after the window (Civil Code Art. 394.1).
    for tranche in await _tranches(db, window.id):
        if tranche.status in ("RESERVED", "SIGNED") and tranche.receipt_submitted_at is None:
            await transition_tranche(db, tranche, "EXPIRED", now=now, reason="WINDOW_CLOSED")
    await db.flush()
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_CLOSING", now=now,
        payload={"reason": reason, "close_wait_until": window.close_wait_until.isoformat()},
    )


async def request_early_close(
    db: AsyncSession, *, window_id: UUID, sme_user_id: UUID, now: dt.datetime, params: DirectTransferParams | None = None,
    calendar: BusinessDayCalendar | None = None,
) -> FundingWindow:
    """SME early close: only when at least the minimum raise is already CONFIRMED (PROVISIONAL(Q-D4))."""
    p = resolve(params)
    window = await lock_window(db, window_id)
    if window.sme_user_id != sme_user_id:
        raise not_found("Window not found")
    if window.status not in ("OPEN", "FULL"):
        raise conflict("WINDOW_NOT_OPEN", "The window is not open")
    if await confirmed_total(db, window.id) < window.min_raise_vnd:
        raise conflict("BELOW_MIN_RAISE_FOR_EARLY_CLOSE", "The minimum raise has not been confirmed yet")
    await begin_close(db, window, now=now, reason="SME_EARLY", params=p, calendar=calendar)
    return window


async def cancel_window(
    db: AsyncSession, *, window_id: UUID, staff_id: UUID, reason: str, now: dt.datetime
) -> FundingWindow:
    """Admin cancel (KYB fail, fraud) before any money is confirmed. After money moved use the close flow."""
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    window = await lock_window(db, window_id)
    if window.status not in ("OPEN", "FULL"):
        raise conflict("WINDOW_NOT_CANCELLABLE", "The window cannot be cancelled")
    tranches = await _tranches(db, window.id)
    if any(t.status in ("ATTESTED", "CONFIRMED", "UNDER_REVIEW", "ACTIVE") or t.receipt_submitted_at for t in tranches if t.status not in ("EXPIRED", "VOID")):
        raise conflict("WINDOW_NOT_CANCELLABLE", "Money has already been reported on this window")
    for t in tranches:
        if t.status in ("RESERVED", "SIGNED"):
            await transition_tranche(db, t, "EXPIRED", now=now, actor_id=staff_id, actor_role="ADMIN", reason=reason)
    window.status = "CANCELLED"
    window.closed_at = now
    listing = await db.get(Listing, window.listing_id)
    listing.status = "CANCELLED"
    await db.flush()
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_CANCELLED", now=now, actor_id=staff_id,
        actor_role="ADMIN", reason=reason,
    )
    return window


async def _pending_tranches(db: AsyncSession, window_id: UUID) -> int:
    """Tranches whose outcome the close must still wait for: ATTESTED (SME dispute window), UNDER_REVIEW (except late
    receipts, which hold no capacity) and SIGNED with a submitted receipt (awaiting staff)."""
    rows = (await db.execute(select(Tranche).where(Tranche.window_id == window_id))).scalars().all()
    count = 0
    for t in rows:
        if t.status == "ATTESTED":
            count += 1
        elif t.status == "UNDER_REVIEW" and t.review_cause != "LATE_RECEIPT":
            count += 1
        elif t.status == "SIGNED" and t.receipt_submitted_at is not None:
            count += 1
    return count


async def advance_window(
    db: AsyncSession,
    *,
    window_id: UUID,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
    calendar: BusinessDayCalendar | None = None,
    invoice_provider: fees.InvoiceProvider | None = None,
) -> FundingWindow:
    """Drive one window through close. Safe to call repeatedly."""
    p = resolve(params)
    cal = calendar or default_calendar()
    window = await lock_window(db, window_id)
    if window.status not in ("OPEN", "FULL", "CLOSING"):
        return window

    await confirm_lapsed_tranches(db, now=now, window_id=window.id, params=p)

    if window.status in ("OPEN", "FULL"):
        reason = None
        if now >= window.window_end:
            reason = "DAY_14"
        elif window.status == "FULL":
            blocking = (
                await db.execute(
                    select(func.count()).select_from(Tranche).where(
                        Tranche.window_id == window.id, Tranche.status.in_(("RESERVED", "SIGNED"))
                    )
                )
            ).scalar_one()
            if not blocking:
                reason = "FULL"
        if reason is None:
            return window
        await begin_close(db, window, now=now, reason=reason, params=p, calendar=cal)

    if window.status == "CLOSING":
        if await _pending_tranches(db, window.id) == 0 or now >= window.close_wait_until:
            await finalize_window(db, window=window, now=now, params=p, calendar=cal, invoice_provider=invoice_provider)
    return window


async def advance_all_windows(db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams | None = None) -> int:
    ids = (await db.execute(select(FundingWindow.id).where(FundingWindow.status.in_(("OPEN", "FULL", "CLOSING"))))).scalars().all()
    for window_id in ids:
        await advance_window(db, window_id=window_id, now=now, params=params)
    return len(ids)


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
async def finalize_window(
    db: AsyncSession,
    *,
    window: FundingWindow,
    now: dt.datetime,
    params: DirectTransferParams,
    calendar: BusinessDayCalendar,
    invoice_provider: fees.InvoiceProvider | None = None,
) -> None:
    tranches = await _tranches(db, window.id)
    # 1. Anything not CONFIRMED at close never joins the loan (PROVISIONAL(Q-D7)).
    for t in tranches:
        if t.status in ("UNDER_REVIEW", "ATTESTED"):
            t.excluded_at_close = True
        elif t.status == "SIGNED":
            # Receipt submitted but never processed by staff: park it for review; it does not join.
            t.excluded_at_close = True
            await transition_tranche(
                db, t, "UNDER_REVIEW", now=now, review_cause="RECEIPT_MISMATCH", reason="UNPROCESSED_AT_CLOSE"
            )
            await open_tranche_dispute(db, tranche=t, cause="RECEIPT_MISMATCH", now=now, params=params)
        elif t.status == "RESERVED":
            await transition_tranche(db, t, "EXPIRED", now=now, reason="WINDOW_CLOSED")
    await db.flush()

    listing = await db.get(Listing, window.listing_id)
    if params.repay_from_approval:
        await _close_repaying_investments(
            db, window=window, tranches=tranches, listing=listing, now=now, params=params, calendar=calendar,
            invoice_provider=invoice_provider,
        )
        return

    confirmed = [t for t in tranches if t.status == "CONFIRMED"]
    total = sum(t.amount_vnd for t in confirmed)
    window.closed_at = now
    window.confirmed_total_vnd = total

    if total < window.min_raise_vnd:
        # Below 50%: no loan, every tranche voided and refunded by the SME, no fee. PROVISIONAL(Q-D8)
        for t in confirmed:
            await transition_tranche(db, t, "VOID", now=now, reason="BELOW_MIN_RAISE", void_reason="BELOW_MIN_RAISE")
            await create_refund_transfer(db, tranche=t, window=window, now=now, reason="BELOW_MIN_RAISE", params=params)
        window.status = "FAILED"
        listing.status = "CANCELLED"
        await db.flush()
        await log_event(
            db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_CLOSED", now=now,
            payload={"outcome": "FAILED", "confirmed_total_vnd": total, "min_raise_vnd": window.min_raise_vnd, "refunds": len(confirmed)},
        )
        return

    await _form_loan(db, window=window, confirmed=confirmed, total=total, listing=listing, now=now, params=params, calendar=calendar, invoice_provider=invoice_provider)


async def form_investment_loan(
    db: AsyncSession,
    *,
    tranche: Tranche,
    window: FundingWindow,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
    calendar: BusinessDayCalendar | None = None,
) -> None:
    """repay_from_approval: one approved (CONFIRMED) investment becomes its own loan at once, the way `_form_loan`
    forms the round's at close, for a round of one: a facility on the round's contract (at the investors' rate,
    starting the first working day after the day staff approved it), the investor's holding, the ticket ACTIVE, its
    repayment days with their stub, and the ledger claims. The SME's activation fee is still invoiced once, when the
    round closes."""
    p = resolve(params)
    cal = calendar or default_calendar()
    await lock_window(db, window.id)
    if tranche.status != "CONFIRMED":
        raise conflict("NOT_CONFIRMED", "Only a confirmed investment forms its loan")
    # The loan works from the approval: repayment starts the next working day after it, even when the loan forms
    # later (an investment approved before repay_from_approval, started by staff advancing its round).
    start_date = first_working_day_after(ict_date(tranche.confirmed_at or now), cal)
    term = window.term_months
    rate = investor_rate_pct(Decimal(window.annual_rate_pct), term, p)
    tranche.stub_receipt_date = await _receipt_date(db, tranche)
    tranche.stub_days = count_business_days(tranche.stub_receipt_date, start_date, cal)
    tranche.stub_interest_vnd = compute_stub_interest(tranche.amount_vnd, rate, tranche.stub_days, p.stub_basis_days)

    facility = originate_facility(
        db,
        contract_id=window.contract_id,
        principal_vnd=tranche.amount_vnd,
        annual_rate_pct=rate,
        contractual_term_months=term,
        payment_reference=await unique_reference_code(db),
        start_date=start_date,
        calendar=cal,
        working_days_per_month=p.working_days_per_month,
        actor_id=None,
    )
    await db.flush()
    context = {"source": "DIRECT_TRANSFER_APPROVAL", "tranche_id": str(tranche.id)}
    apply_transition(db, facility, to_status="APPROVED", trigger="ADMIN_APPROVE", context=context)
    apply_transition(db, facility, to_status="DISBURSED", trigger="DISBURSEMENT_POSTED", context=context)
    facility.disbursed_at = now
    apply_transition(db, facility, to_status="ACTIVE", trigger="SCHEDULE_ACTIVATED", context=context)
    await db.flush()

    holding = Holding(
        contract_id=window.contract_id, investor_id=tranche.investor_id, principal=tranche.amount_vnd,
        share_ratio=Decimal(1),  # the whole of its own loan
    )
    db.add(holding)
    await db.flush()
    tranche.holding_id = holding.id
    tranche.investor_fee_total_vnd = fees.investor_fee_total(tranche.amount_vnd, term, p) if p.fees_enabled else 0
    await transition_tranche(db, tranche, "ACTIVE", now=now, reason="APPROVED_REPAYING")

    if window.repayment_start_date is None:
        window.repayment_start_date = start_date
    contract = await db.get(Contract, window.contract_id)
    contract.funded_amount = int(contract.funded_amount or 0) + tranche.amount_vnd
    listing = await db.get(Listing, window.listing_id)
    listing.funded_amount = int(listing.funded_amount or 0) + tranche.amount_vnd
    await db.flush()

    await generate_repayment_days(
        db, window=window, facility=facility, tranches=[tranche], start_date=start_date, params=p, calendar=cal
    )
    await post_tranche_claims(db, tranche=tranche, contract_id=window.contract_id, sme_user_id=window.sme_user_id)
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="LOAN_FORMED", now=now,
        payload={
            "facility_id": str(facility.id), "principal_vnd": tranche.amount_vnd, "annual_rate_pct": str(rate),
            "start_date": start_date.isoformat(), "stub_interest_vnd": tranche.stub_interest_vnd,
        },
    )


async def _close_repaying_investments(
    db: AsyncSession, *, window: FundingWindow, tranches: list[Tranche], listing: Listing, now: dt.datetime,
    params: DirectTransferParams, calendar: BusinessDayCalendar, invoice_provider: fees.InvoiceProvider | None,
) -> None:
    """repay_from_approval at close: every investment already repays as its own loan (any approved one still
    waiting forms its loan now), so nothing is pooled and nobody is refunded; the round just closes. REPAYING if
    any investment repays, FAILED if none came in. The SME's activation fee is invoiced on what was raised."""
    for t in tranches:
        if t.status == "CONFIRMED":
            await form_investment_loan(db, tranche=t, window=window, now=now, params=params, calendar=calendar)
    active = [t for t in tranches if t.status == "ACTIVE"]
    total = sum(t.amount_vnd for t in active)
    window.closed_at = now
    window.confirmed_total_vnd = total
    if not active:
        window.status = "FAILED"
        listing.status = "CANCELLED"
        await db.flush()
        await log_event(
            db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_CLOSED", now=now,
            payload={"outcome": "FAILED", "confirmed_total_vnd": 0, "mode": "REPAY_FROM_APPROVAL"},
        )
        return
    window.status = "REPAYING"
    listing.status = "FUNDED" if total == window.target_vnd else "CLOSED"
    listing.funded_amount = total
    contract = await db.get(Contract, window.contract_id)
    contract.funded_amount = total
    contract.status = "ACTIVE_FUNDED"
    contract.activated_at = contract.activated_at or now
    await db.flush()
    await fees.issue_sme_activation_invoice(
        db, window=window, confirmed_total_vnd=total, now=now, provider=invoice_provider, params=params
    )
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_CLOSED", now=now,
        payload={
            "outcome": "REPAYING", "confirmed_total_vnd": total, "mode": "REPAY_FROM_APPROVAL", "investments": len(active),
        },
    )


async def _receipt_date(db: AsyncSession, t: Tranche) -> dt.date:
    transfer = (
        await db.execute(select(Transfer).where(Transfer.tranche_id == t.id, Transfer.kind == "DISBURSEMENT").order_by(Transfer.created_at))
    ).scalars().first()
    moment = (transfer.transferred_at if transfer and transfer.transferred_at else None) or t.attested_at or t.confirmed_at
    return ict_date(moment)


def _cap_headroom(principal: int, interest: int, params: DirectTransferParams, term_months: int) -> int:
    allowed = principal * params.rate_cap_pct * term_months // (100 * 12)
    return max(0, allowed - interest)


async def _form_loan(
    db: AsyncSession, *, window: FundingWindow, confirmed: list[Tranche], total: int, listing: Listing, now: dt.datetime,
    params: DirectTransferParams, calendar: BusinessDayCalendar, invoice_provider: fees.InvoiceProvider | None,
) -> None:
    start_date = first_working_day_after(ict_date(now), calendar)  # PROVISIONAL(Q-D4)
    term = window.term_months
    # Investors' return, not the business's all-in rate: see investor_rate_pct.
    rate = investor_rate_pct(Decimal(window.annual_rate_pct), term, params)

    # Stub interest per tranche: separate dated obligation, outside T0 (E3).
    for t in confirmed:
        t.stub_receipt_date = await _receipt_date(db, t)
        t.stub_days = count_business_days(t.stub_receipt_date, start_date, calendar)
        t.stub_interest_vnd = compute_stub_interest(t.amount_vnd, rate, t.stub_days, params.stub_basis_days)

    orig = compute_origination(
        principal_vnd=total, annual_rate_pct=rate, contractual_term_months=term,
        working_days_per_month=params.working_days_per_month,
    )
    stub_total = sum(t.stub_interest_vnd for t in confirmed)
    sme_fee, _, _ = fees.sme_fee_parts(total, params)

    def implied(extra: int) -> Decimal:
        return (Decimal(orig.total_obligation_vnd + extra) / Decimal(total) - 1) * 12 / Decimal(term)

    cap = Decimal(params.rate_cap_pct) / 100
    implied_no_fee = implied(stub_total)
    implied_with_fee = implied(stub_total + sme_fee)
    reduced = False
    if implied_no_fee > cap:
        # INV-5 / Dieu 468: reduce the stub to the headroom (largest remainder, stable order). PROVISIONAL(Q-D5).
        headroom = _cap_headroom(total, orig.interest_vnd, params, term)
        new_stubs = split_gross(headroom, [t.stub_interest_vnd or 0 for t in confirmed]) if stub_total > 0 else [0] * len(confirmed)
        for t, new in zip(confirmed, new_stubs):
            t.stub_interest_vnd = min(t.stub_interest_vnd, new)
        reduced = True
        stub_total = sum(t.stub_interest_vnd for t in confirmed)
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="STUB_CAP_TEST", now=now,
        payload={
            "principal_vnd": total, "t0_vnd": orig.total_obligation_vnd, "stub_total_vnd": stub_total,
            "activation_fee_vnd": sme_fee, "implied_nominal_without_fee": str(implied_no_fee),
            "implied_nominal_with_fee": str(implied_with_fee), "cap": str(cap), "stub_reduced": reduced,
            "within_cap_without_fee": implied_no_fee <= cap, "within_cap_with_fee": implied_with_fee <= cap,
        },
    )

    facility = originate_facility(
        db,
        contract_id=window.contract_id,
        principal_vnd=total,
        annual_rate_pct=rate,
        contractual_term_months=term,
        payment_reference=await unique_reference_code(db),
        start_date=start_date,
        calendar=calendar,
        working_days_per_month=params.working_days_per_month,
        actor_id=None,
    )
    await db.flush()
    apply_transition(db, facility, to_status="APPROVED", trigger="ADMIN_APPROVE", context={"source": "DIRECT_TRANSFER_WINDOW_CLOSE"})
    apply_transition(db, facility, to_status="DISBURSED", trigger="DISBURSEMENT_POSTED", context={"source": "DIRECT_TRANSFER_WINDOW_CLOSE"})
    facility.disbursed_at = now
    apply_transition(db, facility, to_status="ACTIVE", trigger="SCHEDULE_ACTIVATED", context={"source": "DIRECT_TRANSFER_WINDOW_CLOSE"})
    await db.flush()

    contract = await db.get(Contract, window.contract_id)
    for t in confirmed:
        holding = Holding(
            contract_id=window.contract_id,
            investor_id=t.investor_id,
            principal=t.amount_vnd,
            share_ratio=(Decimal(t.amount_vnd) / Decimal(total)).quantize(Decimal("0.00000001")),
        )
        db.add(holding)
        await db.flush()
        t.holding_id = holding.id
        t.investor_fee_total_vnd = fees.investor_fee_total(t.amount_vnd, term, params) if params.fees_enabled else 0
        await transition_tranche(db, t, "ACTIVE", now=now, reason="WINDOW_CLOSED")

    window.status = "REPAYING"
    window.repayment_start_date = start_date
    window.facility_id = facility.id
    listing.status = "FUNDED" if total == window.target_vnd else "CLOSED"
    listing.funded_amount = total
    contract.funded_amount = total
    contract.status = "ACTIVE_FUNDED"
    contract.activated_at = now
    await db.flush()

    await generate_repayment_days(
        db, window=window, facility=facility, tranches=confirmed, start_date=start_date, params=params, calendar=calendar
    )
    for t in confirmed:
        await post_tranche_claims(db, tranche=t, contract_id=window.contract_id, sme_user_id=window.sme_user_id)
    await fees.issue_sme_activation_invoice(
        db, window=window, confirmed_total_vnd=total, now=now, provider=invoice_provider, params=params
    )
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_CLOSED", now=now,
        payload={
            "outcome": "REPAYING", "confirmed_total_vnd": total, "repayment_start_date": start_date.isoformat(),
            "facility_id": str(facility.id), "tranches": len(confirmed),
        },
    )
