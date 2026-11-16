"""Repayment collection (HANDOFF-04 B9). Spec: docs/specs/direct_transfer/repayment-collection.md.

The facility schedule (one fixed daily amount, `app/repayment_schedule`) is split pro rata across the ACTIVE
tranches into per-investor `repayment_days`. The SME pays each investor the NET amount (gross minus the 5% tax
it withholds on individual investors' interest) directly; FundLok computes, shows QR codes and records evidence.
A day counts as paid unless the investor reports it missing by the deadline.

No arrears/lateness logic (MVP scope): a reported, unresolved day simply stays REPORTED_MISSED and blocks settlement.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import cic
from app.direct_transfer.calendar_utils import calendar_deadline, ict_date, start_of_day_ict
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.disputes import open_missed_day_dispute
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event
from app.direct_transfer.ledger_posting import post_repayment_day_settled
from app.direct_transfer.models import Dispute, FundingWindow, PaymentAccount, RepaymentDay, Tranche, Transfer
from app.direct_transfer.tranches import mask_name
from app.direct_transfer.vietqr import build_vietqr_payload
from app.repayment_schedule.calendar import BusinessDayCalendar
from app.repayment_schedule.models import Facility, ScheduledPayment
from app.repayment_schedule.state_machine import apply_transition
from app.users.models import User


class RepaymentInvariantError(Exception):
    pass


def rdiv(numerator: int, denominator: int) -> int:
    """Integer round-half-up division for non-negative operands."""
    return (2 * numerator + denominator) // (2 * denominator)


def split_gross(amount: int, weights: list[int]) -> list[int]:
    """Largest-remainder split of `amount` pro rata to `weights`; sums to `amount` exactly. Ties go to the earlier
    index (callers pass tranches in a stable order)."""
    total = sum(weights)
    bases = [amount * w // total for w in weights]
    rems = [amount * w % total for w in weights]
    leftover = amount - sum(bases)
    order = sorted(range(len(weights)), key=lambda i: (-rems[i], i))
    for i in order[:leftover]:
        bases[i] += 1
    return bases


def tax_nearest(interest: int, withholding_bps: int) -> int:
    """Withholding on one day's interest, rounded to the nearest dong (D-045, handbook 10.8)."""
    return rdiv(interest * withholding_bps, 10_000)


def split_instalment_interest(grosses: list[int], principal_total: int) -> list[int]:
    """Each day's interest part = gross x I / T rounded DOWN (I = total interest, T = total repayable); the last day
    takes the remainder so the interest sums to I exactly. Principal is gross minus this (D-045)."""
    total = sum(grosses)
    interest_total = total - principal_total
    parts = [g * interest_total // total for g in grosses[:-1]]
    parts.append(interest_total - sum(parts))
    return parts


def principal_for_day(principal_total: int, day_index: int, day_count: int) -> int:
    """Cumulative round-half-up so the principal over the term is exactly `principal_total`."""
    return rdiv(principal_total * day_index, day_count) - rdiv(principal_total * (day_index - 1), day_count)


def report_deadline_for(due_date: dt.date, params: DirectTransferParams, calendar: BusinessDayCalendar | None = None) -> dt.datetime:
    # 24 calendar hours after the due date ends. The Late-status spec (docs/specs/repayment_schedule/late-status.md,
    # PR #108, draft) will re-anchor this on the SME's proof time later.
    return calendar_deadline(start_of_day_ict(due_date + dt.timedelta(days=1)), hours=params.lender_report_hours)


async def generate_repayment_days(
    db: AsyncSession,
    *,
    window: FundingWindow,
    facility: Facility,
    tranches: list[Tranche],
    start_date: dt.date,
    params: DirectTransferParams | None = None,
    calendar: BusinessDayCalendar | None = None,
) -> int:
    """Create instalment rows (pro rata split of every scheduled day) and stub rows. Returns rows created."""
    p = resolve(params)
    tranches = list(tranches)
    weights = [t.amount_vnd for t in tranches]
    total = sum(weights)
    n_days = facility.business_day_count
    schedule = (
        await db.execute(select(ScheduledPayment).where(ScheduledPayment.facility_id == facility.id).order_by(ScheduledPayment.day_index))
    ).scalars().all()
    if len(schedule) != n_days:
        raise RepaymentInvariantError("schedule length does not match business_day_count")

    per_tranche: list[list[tuple[ScheduledPayment, int, int, int]]] = [[] for _ in tranches]  # (sp, gross, principal, interest)
    splits = [split_gross(int(sp.expected_amount_vnd), weights) for sp in schedule]
    for i, t in enumerate(tranches):
        grosses = [split[i] for split in splits]
        interests = split_instalment_interest(grosses, t.amount_vnd)
        for sp, gross, interest in zip(schedule, grosses, interests):
            principal = gross - interest
            if principal < 0:
                raise RepaymentInvariantError(f"negative principal for tranche {t.id} on day {sp.day_index}")
            per_tranche[i].append((sp, gross, principal, interest))

    created = 0
    for i, t in enumerate(tranches):
        rows = per_tranche[i]
        individual = t.investor_type == "INDIVIDUAL"
        taxes: list[int] = []
        if individual:
            for _, _, _, interest in rows[:-1]:
                taxes.append(tax_nearest(interest, p.withholding_bps))
            total_interest = sum(r[3] for r in rows)
            total_tax = rdiv(total_interest * p.withholding_bps, 10_000)
            last_interest = rows[-1][3]
            taxes.append(min(last_interest, max(0, total_tax - sum(taxes))))
        else:
            taxes = [0] * len(rows)
        for (sp, gross, principal, interest), tax in zip(rows, taxes):
            db.add(
                RepaymentDay(
                    tranche_id=t.id, facility_id=facility.id, scheduled_payment_id=sp.id, kind="INSTALMENT",
                    day_index=sp.day_index, due_date=sp.scheduled_date, principal_vnd=principal, interest_vnd=interest,
                    gross_vnd=gross, tax_withheld_vnd=tax, net_vnd=gross - tax, status="DUE",
                    report_deadline=report_deadline_for(sp.scheduled_date, p, calendar),
                )
            )
            created += 1
        if t.stub_interest_vnd > 0:
            tax = t.stub_interest_vnd * p.withholding_bps // 10_000 if individual else 0
            db.add(
                RepaymentDay(
                    tranche_id=t.id, facility_id=facility.id, scheduled_payment_id=None, kind="STUB", day_index=0,
                    due_date=start_date, principal_vnd=0, interest_vnd=t.stub_interest_vnd, gross_vnd=t.stub_interest_vnd,
                    tax_withheld_vnd=tax, net_vnd=t.stub_interest_vnd - tax, status="DUE",
                    report_deadline=report_deadline_for(start_date, p, calendar),
                )
            )
            created += 1
    await db.flush()
    return created


# --------------------------------------------------------------------------- #
# read models
# --------------------------------------------------------------------------- #
def _day_row(day: RepaymentDay, tranche: Tranche, investor: User, account: PaymentAccount | None) -> dict:
    payee = None
    qr = None
    if account is not None:
        payee = {
            "bank_name": account.bank_name, "bank_bin": account.bank_bin,
            "account_number": account.account_number, "account_name": account.account_name,
        }
        if day.net_vnd > 0:
            qr = build_vietqr_payload(
                bank_bin=account.bank_bin, account_number=account.account_number, amount_vnd=day.net_vnd, memo=tranche.reference_code
            )
    return {
        "repayment_day_id": day.id,
        "tranche_id": tranche.id,
        "kind": day.kind,
        "investor_display": mask_name(investor.full_name or investor.email),
        "due_date": day.due_date,
        "principal_vnd": day.principal_vnd,
        "interest_vnd": day.interest_vnd,
        "gross_vnd": day.gross_vnd,
        "tax_withheld_vnd": day.tax_withheld_vnd,
        "net_vnd": day.net_vnd,
        "status": day.status,
        "report_deadline": day.report_deadline,
        "payee": payee,
        "reference_code": tranche.reference_code,
        "vietqr_payload": qr,
    }


async def todays_payments(db: AsyncSession, *, sme_user_id: UUID, on_date: dt.date) -> dict:
    """What the SME owes each investor today (and any unsettled earlier days): one VietQR per investor row."""
    # With repay_from_approval, investments repay while their round is still open or closing.
    windows = (
        await db.execute(
            select(FundingWindow).where(
                FundingWindow.sme_user_id == sme_user_id,
                FundingWindow.status.in_(("OPEN", "FULL", "CLOSING", "REPAYING")),
            )
        )
    ).scalars().all()
    rows: list[dict] = []
    for window in windows:
        days = (
            await db.execute(
                select(RepaymentDay)
                .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
                .where(
                    Tranche.window_id == window.id,
                    RepaymentDay.due_date <= on_date,
                    RepaymentDay.settled_at.is_(None),
                )
                .order_by(RepaymentDay.due_date, Tranche.confirmed_at, Tranche.id, RepaymentDay.day_index)
            )
        ).scalars().all()
        for day in days:
            tranche = await db.get(Tranche, day.tranche_id)
            investor = await db.get(User, tranche.investor_id)
            account = await db.get(PaymentAccount, tranche.investor_account_id) if tranche.investor_account_id else None
            rows.append(_day_row(day, tranche, investor, account))
    totals = {
        "gross_vnd": sum(r["gross_vnd"] for r in rows),
        "tax_withheld_vnd": sum(r["tax_withheld_vnd"] for r in rows),
        "net_vnd": sum(r["net_vnd"] for r in rows),
    }
    return {"date": on_date, "rows": rows, "totals": totals}


async def tranche_schedule(db: AsyncSession, *, tranche_id: UUID, user_id: UUID) -> list[dict]:
    tranche = await db.get(Tranche, tranche_id)
    if tranche is None:
        raise not_found("Ticket not found")
    window = await db.get(FundingWindow, tranche.window_id)
    if user_id not in (tranche.investor_id, window.sme_user_id):
        raise not_found("Ticket not found")
    days = (
        await db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tranche_id).order_by(RepaymentDay.due_date, RepaymentDay.day_index))
    ).scalars().all()
    investor = await db.get(User, tranche.investor_id)
    account = await db.get(PaymentAccount, tranche.investor_account_id) if tranche.investor_account_id else None
    return [_day_row(d, tranche, investor, account) for d in days]


async def withholding_report(db: AsyncSession, *, sme_user_id: UUID, month: dt.date) -> dict:
    """Data for the SME's monthly withholding report (wording is Huy's). `month` is the first day of the month."""
    first = month.replace(day=1)
    nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    rows = (
        await db.execute(
            select(RepaymentDay, Tranche)
            .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
            .join(FundingWindow, FundingWindow.id == Tranche.window_id)
            .where(FundingWindow.sme_user_id == sme_user_id, RepaymentDay.due_date >= first, RepaymentDay.due_date < nxt)
        )
    ).all()
    by_tranche: dict[UUID, dict] = {}
    for day, tranche in rows:
        entry = by_tranche.get(tranche.id)
        if entry is None:
            investor = await db.get(User, tranche.investor_id)
            entry = by_tranche[tranche.id] = {
                "tranche_id": tranche.id,
                "investor_display": mask_name(investor.full_name or investor.email),
                "investor_type": tranche.investor_type,
                "interest_vnd": 0,
                "tax_withheld_vnd": 0,
            }
        entry["interest_vnd"] += day.interest_vnd
        entry["tax_withheld_vnd"] += day.tax_withheld_vnd
    out = list(by_tranche.values())
    return {
        "month": first.strftime("%Y-%m"),
        "rows": out,
        "totals": {
            "interest_vnd": sum(r["interest_vnd"] for r in out),
            "tax_withheld_vnd": sum(r["tax_withheld_vnd"] for r in out),
        },
    }


# --------------------------------------------------------------------------- #
# SME marks paid; presumption; investor reports
# --------------------------------------------------------------------------- #
async def _sme_day(db: AsyncSession, day_id: UUID, sme_user_id: UUID):
    day = (await db.execute(select(RepaymentDay).where(RepaymentDay.id == day_id).with_for_update())).scalar_one_or_none()
    if day is None:
        raise not_found("Repayment day not found")
    tranche = await db.get(Tranche, day.tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    if window.sme_user_id != sme_user_id:
        raise not_found("Repayment day not found")
    return day, tranche, window


async def mark_days_paid(
    db: AsyncSession,
    *,
    sme_user_id: UUID,
    repayment_day_ids: list[UUID],
    receipt_file_id: UUID | None,
    idempotency_key: str,
    now: dt.datetime,
) -> list[RepaymentDay]:
    if not idempotency_key:
        raise bad_request("Idempotency-Key is required", "IDEMPOTENCY_KEY_REQUIRED")
    if not repayment_day_ids:
        raise bad_request("No repayment days given")
    today = ict_date(now)
    result: list[RepaymentDay] = []
    for day_id in repayment_day_ids:
        day, tranche, window = await _sme_day(db, day_id, sme_user_id)
        if day.due_date > today:
            raise conflict("NOT_DUE_YET", "This day is not due yet")
        if day.status == "DUE":
            day.status = "PRESUMED_PAID"
            day.marked_paid_at = now
            day.marked_paid_by = sme_user_id
        if receipt_file_id is not None:
            key = f"{idempotency_key}:{day.id}"
            existing = (await db.execute(select(Transfer).where(Transfer.idempotency_key == key))).scalar_one_or_none()
            if existing is None:
                transfer = Transfer(
                    kind="REPAYMENT", tranche_id=tranche.id, repayment_day_id=day.id, from_account_id=window.sme_payment_account_id,
                    to_account_id=tranche.investor_account_id, amount_vnd=day.net_vnd, reference=tranche.reference_code,
                    rail="MANUAL", confirmation_level="REPORTED", check_status="AWAITING_FIELDS",
                    evidence_file_id=receipt_file_id, reported_by=sme_user_id, idempotency_key=key,
                )
                if day.net_vnd > 0:
                    db.add(transfer)
                    await db.flush()
                    day.transfer_id = transfer.id
        await log_event(
            db, entity_type="REPAYMENT_DAY", entity_id=day.id, event_type="REPAYMENT_DAY_REPORTED", now=now,
            actor_id=sme_user_id, actor_role="SME", payload={"event": "MARKED_PAID", "receipt_file_id": str(receipt_file_id) if receipt_file_id else None},
        )
        result.append(day)
    await db.flush()
    return result


async def presume_paid_days(db: AsyncSession, *, now: dt.datetime) -> int:
    """A day counts as paid unless reported: DUE rows whose due date is before today (ICT) become PRESUMED_PAID."""
    today = ict_date(now)
    res = await db.execute(
        update(RepaymentDay).where(RepaymentDay.status == "DUE", RepaymentDay.due_date < today).values(status="PRESUMED_PAID")
    )
    await db.flush()
    return res.rowcount or 0


async def report_missing_day(
    db: AsyncSession,
    *,
    day_id: UUID,
    investor_id: UUID,
    statement: str | None,
    file_id: UUID | None,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> RepaymentDay:
    p = resolve(params)
    day = (await db.execute(select(RepaymentDay).where(RepaymentDay.id == day_id).with_for_update())).scalar_one_or_none()
    if day is None:
        raise not_found("Repayment day not found")
    tranche = await db.get(Tranche, day.tranche_id)
    if tranche.investor_id != investor_id:
        raise not_found("Repayment day not found")
    if day.status == "REPORTED_MISSED":
        raise conflict("ALREADY_REPORTED", "This day was already reported")
    if day.due_date > ict_date(now):
        raise conflict("NOT_DUE_YET", "This day is not due yet")
    if day.status == "MATCHED":
        raise DirectTransferError(400, f"Invalid state transition: {day.status} → REPORTED_MISSED", "INVALID_TRANSITION")

    if now < day.report_deadline:
        # On time: the day stops counting as paid; the SME proves first (burden of proof, E6).
        try:
            await open_missed_day_dispute(
                db, day=day, cause="MISSED_DAY", raised_by=investor_id, statement=statement, file_id=file_id, now=now, params=p
            )
        except DirectTransferError as exc:
            if exc.code == "DISPUTE_ALREADY_OPEN":
                raise conflict("ALREADY_REPORTED", "This day was already reported") from exc
            raise
        day.status = "REPORTED_MISSED"
        day.reported_at = now
        day.reported_by = investor_id
        await db.flush()
        await cic.emit_missed_day_correction(db, day, now=now, stage="reported")
        await log_event(
            db, entity_type="REPAYMENT_DAY", entity_id=day.id, event_type="REPAYMENT_DAY_REPORTED", now=now,
            actor_id=investor_id, actor_role="INVESTOR", payload={"event": "REPORTED_MISSING", "late": False},
        )
        return day

    # Late (PROVISIONAL(Q-D14)): accepted into reconciliation without changing history, up to the cap.
    late_limit = calendar_deadline(day.report_deadline, days=p.late_report_cap_days)
    if now >= late_limit:
        await log_event(
            db, entity_type="REPAYMENT_DAY", entity_id=day.id, event_type="REPAYMENT_DAY_REPORTED", now=now,
            actor_id=investor_id, actor_role="INVESTOR", payload={"event": "LATE_REPORT_REFUSED"},
        )
        err = conflict("REPORT_WINDOW_CLOSED", "The report window for this day has closed")
        err.persist = True
        raise err
    try:
        await open_missed_day_dispute(
            db, day=day, cause="LATE_REPORT", raised_by=investor_id, statement=statement, file_id=file_id, now=now, params=p
        )
    except DirectTransferError as exc:
        if exc.code == "DISPUTE_ALREADY_OPEN":
            raise conflict("ALREADY_REPORTED", "This day was already reported") from exc
        raise
    await log_event(
        db, entity_type="REPAYMENT_DAY", entity_id=day.id, event_type="REPAYMENT_DAY_REPORTED", now=now,
        actor_id=investor_id, actor_role="INVESTOR", payload={"event": "LATE_REPORT", "status_unchanged": day.status},
    )
    return day


# --------------------------------------------------------------------------- #
# matching, settlement, facility closure
# --------------------------------------------------------------------------- #
async def _settle_day(db: AsyncSession, day: RepaymentDay, *, now: dt.datetime) -> None:
    if day.settled_at is not None:
        return
    tranche = await db.get(Tranche, day.tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    facility = await db.get(Facility, day.facility_id)
    day.settled_at = now
    if day.kind == "INSTALMENT":
        # collected_to_date tracks T0 only (INV-1); the stub obligation sits outside T0 (E3).
        facility.collected_to_date_vnd = int(facility.collected_to_date_vnd) + day.gross_vnd
    await post_repayment_day_settled(
        db, repayment_day=day, tranche=tranche, contract_id=window.contract_id, sme_user_id=window.sme_user_id
    )
    await db.flush()


async def match_day(
    db: AsyncSession,
    *,
    day_id: UUID,
    staff_id: UUID,
    reason: str,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
    transfer_id: UUID | None = None,
    actor_role: str = "ADMIN",
) -> RepaymentDay:
    """Staff confirm that a day was paid (matched to evidence). Also the PAID outcome of a missed-day dispute, and
    the investor confirming they received it (`actor_role="INVESTOR"`, `staff_id` the investor)."""
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    day = (await db.execute(select(RepaymentDay).where(RepaymentDay.id == day_id).with_for_update())).scalar_one_or_none()
    if day is None:
        raise not_found("Repayment day not found")
    if day.status == "MATCHED":
        return day
    day.status = "MATCHED"
    day.matched_at = now
    if transfer_id is not None:
        day.transfer_id = transfer_id
        transfer = await db.get(Transfer, transfer_id)
        if transfer is not None:
            transfer.confirmation_level = "CONFIRMED"
    await _settle_day(db, day, now=now)
    await log_event(
        db, entity_type="REPAYMENT_DAY", entity_id=day.id, event_type="REPAYMENT_DAY_REPORTED", now=now,
        actor_id=staff_id, actor_role=actor_role, reason=reason, payload={"event": "MATCHED"},
    )
    tranche = await db.get(Tranche, day.tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    await maybe_close_facility(db, window, now=now)
    return day


async def settle_days(db: AsyncSession, *, now: dt.datetime) -> int:
    """PRESUMED_PAID days past their report deadline, with no open dispute, count as collected (ledger posted)."""
    open_disputes = select(Dispute.repayment_day_id).where(Dispute.status != "DECIDED", Dispute.repayment_day_id.is_not(None))
    days = (
        await db.execute(
            select(RepaymentDay)
            .where(
                RepaymentDay.status == "PRESUMED_PAID",
                RepaymentDay.settled_at.is_(None),
                RepaymentDay.report_deadline <= now,
                RepaymentDay.id.not_in(open_disputes),
            )
            .order_by(RepaymentDay.due_date, RepaymentDay.day_index)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    windows: dict[UUID, FundingWindow] = {}
    for day in days:
        await _settle_day(db, day, now=now)
        tranche = await db.get(Tranche, day.tranche_id)
        windows[tranche.window_id] = await db.get(FundingWindow, tranche.window_id)
    for window in windows.values():
        await maybe_close_facility(db, window, now=now)
    return len(days)


async def maybe_close_facility(db: AsyncSession, window: FundingWindow, *, now: dt.datetime) -> bool:
    """Each of the window's facilities settles once all its repayment rows are settled; once every one has and the
    window is REPAYING, the window is REPAID and the SME is unlocked (PROVISIONAL(Q-D16): "repaid in full" =
    facility SETTLED and every row settled). One facility per window when the loan forms at close; one per
    investment with repay_from_approval. True when the window became REPAID."""
    facility_ids = (
        await db.execute(
            select(RepaymentDay.facility_id)
            .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
            .where(Tranche.window_id == window.id)
            .distinct()
        )
    ).scalars().all()
    if not facility_ids:
        return False
    all_settled = True
    for facility_id in facility_ids:
        facility = await db.get(Facility, facility_id)
        if facility.status == "SETTLED":
            continue
        unsettled = (
            await db.execute(
                select(func.count()).select_from(RepaymentDay).where(
                    RepaymentDay.facility_id == facility_id, RepaymentDay.settled_at.is_(None)
                )
            )
        ).scalar_one()
        if unsettled:
            all_settled = False
            continue
        balance = int(facility.total_obligation_vnd) - int(facility.collected_to_date_vnd)
        if balance != 0:
            raise RepaymentInvariantError(f"all rows settled but facility balance is {balance}")
        apply_transition(db, facility, to_status="SETTLED", trigger="OBLIGATION_SATISFIED", context={"balance_vnd": 0})
    if not all_settled or window.status != "REPAYING":
        await db.flush()
        return False
    window.status = "REPAID"
    await db.flush()
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_REPAID", now=now,
        payload={"facility_ids": [str(f) for f in facility_ids]},
    )
    return True
