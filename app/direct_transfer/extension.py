"""Automatic extension (D-042). Spec: docs/specs/repayment_schedule/automatic-extension.md.

The SME files month m's revenue by the 5th of month m+1. If the instalments due in m exceed s x revenue, every
investor's loan is extended by the same Δ working days: each pays the extra interest of Δ days, and its remaining
balance is spread over its remaining days plus Δ, from the first working day after the filing. The daily amount never
rises; the backstop never moves. No staff step, no fee.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import ICT
from app.direct_transfer.calendar_utils import default_calendar, first_working_day_after, ict_date
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event, store_evidence_file
from app.direct_transfer.models import ExtensionFiling, FundingWindow, RepaymentDay, Tranche
from app.direct_transfer.repayment import rdiv, report_deadline_for, split_instalment_interest, tax_nearest
from app.direct_transfer.storage import EvidenceStorage
from app.lending.models import Contract, ScoreRun
from app.notifications.models import EVENT_EXTENSION_APPLIED
from app.notifications.service import create_notification
from app.repayment_schedule.models import Facility, ScheduleVersion

EVIDENCE_KINDS = ("TAX_DECLARATION", "BANK_INFLOW")


def _half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# pure formulas (spec section 6)
# --------------------------------------------------------------------------- #
def shortfall_vnd(due_vnd: int, revenue_share, revenue_vnd: int, *, loan_days: int = 1, month_days: int = 1) -> int:
    """U = instalments due in the month - s x evidenced revenue. A partial first or last month counts only the share of
    revenue for the working days the loan repays in it (loan_days / month_days)."""
    obligation = Decimal(str(revenue_share)) * revenue_vnd * loan_days / month_days
    return due_vnd - _half_up(obligation)


def extension_days(shortfall: int, due_vnd: int, room_days: int | None, params: DirectTransferParams | None = None) -> int:
    """Δ = ceil(U / due x 21), up to the next multiple of 5, capped at the room left to the backstop. 0 without a
    shortfall."""
    p = resolve(params)
    if shortfall <= 0 or due_vnd <= 0:
        return 0
    raw = -(-shortfall * p.extension_days_per_month // due_vnd)
    delta = -(-raw // p.extension_day_step) * p.extension_day_step
    return delta if room_days is None else max(0, min(delta, room_days))


@dataclasses.dataclass(frozen=True)
class InvestorTerms:
    extra_interest_vnd: int
    new_total_vnd: int
    new_daily_vnd: int
    fee_increase_vnd: int


def investor_terms(
    *, principal_vnd: int, annual_rate_pct, delta_days: int, total_vnd: int, paid_vnd: int, days_left: int,
    old_daily_vnd: int, params: DirectTransferParams | None = None,
) -> InvestorTerms:
    """One investor's extension: extra = P x r x Δ / 252, new T = T + extra, new daily = floor((new T - paid) /
    (days left + Δ)), fee += P x 2.5% x Δ / 252 (nearest dong, 0.5 up). The daily amount may never rise."""
    p = resolve(params)
    extra = _half_up(Decimal(principal_vnd) * Decimal(str(annual_rate_pct)) / 100 * delta_days / p.extension_basis_days)
    new_total = total_vnd + extra
    new_daily = (new_total - paid_vnd) // (days_left + delta_days)
    if new_daily > old_daily_vnd:
        raise conflict(
            "EXTENSION_RAISES_DAILY", f"The new daily amount {new_daily} would exceed the current {old_daily_vnd}"
        )
    fee = rdiv(principal_vnd * p.investor_fee_bps_per_year * delta_days, 10_000 * p.extension_basis_days)
    return InvestorTerms(extra, new_total, new_daily, fee)


# --------------------------------------------------------------------------- #
# filing and applying
# --------------------------------------------------------------------------- #
def _month_bounds(month: dt.date) -> tuple[dt.date, dt.date]:
    first = month.replace(day=1)
    return first, (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _working_days(first: dt.date, end: dt.date, calendar) -> int:
    return sum(1 for i in range((end - first).days) if calendar.is_business_day(first + dt.timedelta(days=i)))


async def file_extension(
    db: AsyncSession,
    *,
    window_id: UUID,
    sme_user_id: UUID,
    period_month: dt.date,
    revenue_vnd: int,
    evidence_kind: str,
    content: bytes,
    content_type: str,
    filename: str | None,
    storage: EvidenceStorage,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> ExtensionFiling:
    """The SME's filing for `period_month`, decided at once (no staff step): EXTENDED, NO_SHORTFALL or NO_ROOM."""
    p = resolve(params)
    cal = default_calendar()
    if evidence_kind not in EVIDENCE_KINDS:
        raise bad_request("Evidence must be a tax declaration or a bank inflow statement", "INVALID_EVIDENCE_KIND")
    if revenue_vnd < 0:
        raise bad_request("Revenue cannot be negative", "INVALID_REVENUE")
    window = (await db.execute(select(FundingWindow).where(FundingWindow.id == window_id).with_for_update())).scalar_one_or_none()
    if window is None or window.sme_user_id != sme_user_id:
        raise not_found("Round not found")
    first, nxt = _month_bounds(period_month)
    today = ict_date(now)
    if not (nxt <= today <= nxt.replace(day=p.extension_filing_last_day)):
        raise conflict("FILING_CLOSED", f"File {first:%m/%Y} between {nxt:%d/%m} and {nxt.replace(day=p.extension_filing_last_day):%d/%m}")
    if (await db.execute(select(ExtensionFiling.id).where(ExtensionFiling.window_id == window.id, ExtensionFiling.period_month == first))).first():
        raise conflict("ALREADY_FILED", "This month was already filed")

    tranches = (
        await db.execute(select(Tranche).where(Tranche.window_id == window.id, Tranche.status == "ACTIVE").order_by(Tranche.id))
    ).scalars().all()
    if not tranches:
        raise conflict("NOT_REPAYING", "Only a loan that is repaying can be extended")
    rows = (
        await db.execute(
            select(RepaymentDay)
            .where(RepaymentDay.tranche_id.in_([t.id for t in tranches]), RepaymentDay.kind == "INSTALMENT")
            .order_by(RepaymentDay.tranche_id, RepaymentDay.day_index)
            .with_for_update()
        )
    ).scalars().all()
    # A Late day (no proof) or a day in dispute blocks the filing (D-041, late-status.md rule 7).
    if any(r.status in ("LATE", "REPORTED_MISSED") for r in rows):
        raise conflict("LATE_DAY_BLOCKS_FILING", "A repayment day is late or in dispute: no extension until it is settled")
    contract = await db.get(Contract, window.contract_id)
    score_run = await db.get(ScoreRun, contract.score_run_id) if contract and contract.score_run_id else None
    if score_run is None or score_run.daily_repayment_rate is None:
        raise conflict("REVENUE_SHARE_UNKNOWN", "The loan's revenue share s is not set")

    in_month = [r for r in rows if first <= r.due_date < nxt]
    due = sum(r.gross_vnd for r in in_month)
    shortfall = shortfall_vnd(
        due, score_run.daily_repayment_rate, revenue_vnd,
        loan_days=len({r.due_date for r in in_month}) or 1, month_days=_working_days(first, nxt, cal) or 1,
    )
    granted = (
        await db.execute(select(func.coalesce(func.sum(ExtensionFiling.delta_days), 0)).where(ExtensionFiling.window_id == window.id))
    ).scalar_one()
    facilities = {r.facility_id for r in rows}
    rooms = []
    for facility_id in facilities:
        facility = await db.get(Facility, facility_id)
        if facility.backstop_day_index is not None:
            rooms.append(facility.backstop_day_index - facility.business_day_count - granted)
    delta = extension_days(shortfall, due, min(rooms) if rooms else None, p)

    evidence = await store_evidence_file(
        db, storage, owner_user_id=sme_user_id, purpose="SME_STATEMENT", content=content, content_type=content_type,
        filename=filename, params=p,
    )
    filing = ExtensionFiling(
        window_id=window.id, sme_user_id=sme_user_id, period_month=first, evidence_kind=evidence_kind,
        evidence_file_id=evidence.id, revenue_vnd=revenue_vnd, revenue_share=score_run.daily_repayment_rate,
        due_vnd=due, shortfall_vnd=shortfall, delta_days=delta, filed_at=now,
        status="EXTENDED" if delta else ("NO_SHORTFALL" if shortfall <= 0 else "NO_ROOM"),
    )
    if delta:
        filing.start_date = first_working_day_after(today, cal)
        filing.previous_rows = await _apply(db, window=window, tranches=tranches, rows=rows, delta=delta, start=filing.start_date, now=now, params=p)
    db.add(filing)
    await db.flush()
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="EXTENSION_FILED", now=now, actor_id=sme_user_id,
        actor_role="SME", file_sha256=evidence.sha256,
        payload={"filing_id": str(filing.id), "status": filing.status, "due_vnd": due, "shortfall_vnd": shortfall, "delta_days": delta},
    )
    return filing


async def _apply(
    db: AsyncSession, *, window: FundingWindow, tranches: list[Tranche], rows: list[RepaymentDay], delta: int,
    start: dt.date, now: dt.datetime, params: DirectTransferParams,
) -> list[dict]:
    """Re-spread every investor's days from `start` and append Δ days; returns the rows it replaced (the old version)."""
    cal = default_calendar()
    previous: list[dict] = []
    # Per facility: extra interest, and the new version's daily amount, final instalment and day count.
    totals: dict[UUID, dict[str, int]] = {}
    for tranche in tranches:
        mine = [r for r in rows if r.tranche_id == tranche.id]
        before = [r for r in mine if r.due_date < start]
        future = [r for r in mine if r.due_date >= start]
        if not future:
            raise conflict("NO_DAYS_LEFT", "The loan has no repayment day left to extend")
        terms = investor_terms(
            principal_vnd=tranche.amount_vnd, annual_rate_pct=window.annual_rate_pct, delta_days=delta,
            total_vnd=sum(r.gross_vnd for r in mine), paid_vnd=sum(r.gross_vnd for r in before), days_left=len(future),
            old_daily_vnd=future[0].gross_vnd, params=params,
        )
        previous += [
            {"tranche_id": str(r.tranche_id), "day_index": r.day_index, "due_date": r.due_date.isoformat(),
             "gross_vnd": r.gross_vnd, "principal_vnd": r.principal_vnd, "interest_vnd": r.interest_vnd,
             "tax_withheld_vnd": r.tax_withheld_vnd}
            for r in future
        ]
        dates = [r.due_date for r in future]
        while len(dates) < len(future) + delta:
            dates.append(first_working_day_after(dates[-1], cal))
        balance = terms.new_total_vnd - sum(r.gross_vnd for r in before)
        grosses = [terms.new_daily_vnd] * (len(dates) - 1) + [balance - terms.new_daily_vnd * (len(dates) - 1)]
        interests = split_instalment_interest(grosses, tranche.amount_vnd - sum(r.principal_vnd for r in before))
        withholding = params.withholding_bps if tranche.investor_type == "INDIVIDUAL" else 0
        taxes = [tax_nearest(i, withholding) for i in interests[:-1]]
        total_tax = rdiv((sum(r.interest_vnd for r in before) + sum(interests)) * withholding, 10_000)
        taxes.append(min(interests[-1], max(0, total_tax - sum(r.tax_withheld_vnd for r in before) - sum(taxes))))
        facility_id = mine[0].facility_id
        for i, (due_date, gross, interest, tax) in enumerate(zip(dates, grosses, interests, taxes)):
            row = future[i] if i < len(future) else RepaymentDay(
                tranche_id=tranche.id, facility_id=facility_id, scheduled_payment_id=None, kind="INSTALMENT",
                day_index=future[-1].day_index + i - len(future) + 1, due_date=due_date, status="DUE",
                report_deadline=report_deadline_for(due_date, params),
            )
            row.gross_vnd, row.interest_vnd, row.principal_vnd = gross, interest, gross - interest
            row.tax_withheld_vnd, row.net_vnd = tax, gross - tax
            db.add(row)
        tranche.investor_fee_total_vnd += terms.fee_increase_vnd
        sums = totals.setdefault(facility_id, {"extra": 0, "daily": 0, "final": 0, "days": 0})
        sums["extra"] += terms.extra_interest_vnd
        sums["daily"] += terms.new_daily_vnd
        sums["final"] += grosses[-1]
        sums["days"] = max(sums["days"], len(before) + len(dates))
        create_notification(
            db, user_id=tranche.investor_id, event=EVENT_EXTENSION_APPLIED, entity_type="TRANCHE", entity_id=tranche.id,
            data={"window_id": str(window.id), "tranche_id": str(tranche.id), "delta_days": delta, "start_date": start.isoformat(),
                  "new_daily_vnd": terms.new_daily_vnd, "extra_interest_vnd": terms.extra_interest_vnd},
        )
    for facility_id, sums in totals.items():
        # T grows by the extra interest; the backstop day never moves (it is immutable in the database too).
        facility = await db.get(Facility, facility_id)
        facility.interest_vnd = int(facility.interest_vnd) + sums["extra"]
        facility.total_obligation_vnd = int(facility.total_obligation_vnd) + sums["extra"]
        latest = (
            await db.execute(select(func.max(ScheduleVersion.version_number)).where(ScheduleVersion.facility_id == facility_id))
        ).scalar_one()
        # A new version beside the old one; the old per-investor rows are kept on the filing (`previous_rows`).
        db.add(ScheduleVersion(
            facility_id=facility_id, version_number=(latest or 0) + 1, daily_amount_vnd=sums["daily"],
            day_count=sums["days"], remainder_vnd=sums["final"] - sums["daily"], final_instalment_vnd=sums["final"],
        ))
    # The SME is also the loan's guarantor (standing_offer.py): one notice covers both.
    create_notification(
        db, user_id=window.sme_user_id, event=EVENT_EXTENSION_APPLIED, entity_type="FUNDING_WINDOW", entity_id=window.id,
        data={"window_id": str(window.id), "delta_days": delta, "start_date": start.isoformat()},
    )
    await db.flush()
    return previous
