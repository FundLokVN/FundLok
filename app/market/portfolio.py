"""Each side's own money, as the Transactions and Analytics pages show it, read from the direct-transfer tickets,
repayment days and fee invoices (ADR-009: the money moves between the investor's and the business's own bank
accounts; these are FundLok's records of it).

The investor: each investment they transferred (out), and each working day the business repaid them (in, after the tax
the business keeps back); month by month, what was invested and what came back (the capital returned and the earnings
after tax, apart), by industry, and where the invested capital stands.

The business: each investment it received (in), each working day it repaid an investor (out, what it transferred) and
FundLok's fee invoices (out); its loan (the sum of its investments, each repaying on its own), month by month, against
its verified revenue, and what makes up its business score.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from uuid import UUID

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.close import investor_rate_pct
from app.direct_transfer.config import resolve
from app.direct_transfer.models import FeeInvoice, FundingWindow, RepaymentDay, Tranche
from app.direct_transfer.tranches import mask_name
from app.lending.models import Contract, Project, ScoreRun
from app.underwriting.grading.params import load_params
from app.underwriting.grading.rollup import compute_rollup
from app.underwriting.models import ScoreRunInput
from app.users.models import User

# Money received by the business: the investment counts.
RECEIVED = ("CONFIRMED", "ACTIVE")
# Money sent, waiting for FundLok's check.
CHECKING = ("ATTESTED", "UNDER_REVIEW")
# A day the business paid (by its word until FundLok matches the receipt), or the investor said never arrived.
PAID = ("PRESUMED_PAID", "MATCHED")
SHOWN_DAYS = (*PAID, "REPORTED_MISSED")


def _month(day: dt.date) -> str:
    return day.strftime("%Y-%m")


def _months(first: dt.date, last: dt.date) -> list[str]:
    """Every month from `first`'s to `last`'s, as "YYYY-MM"."""
    out, year, month = [], first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


async def _tickets(db: AsyncSession, investor_id: UUID) -> list[tuple[Tranche, Project | None]]:
    return list(
        (
            await db.execute(
                select(Tranche, Project)
                .join(FundingWindow, FundingWindow.id == Tranche.window_id)
                .outerjoin(Project, Project.id == FundingWindow.project_id)
                .where(Tranche.investor_id == investor_id)
            )
        ).all()
    )


async def _days(db: AsyncSession, tranche_ids: list[UUID], *, statuses=SHOWN_DAYS) -> list[RepaymentDay]:
    if not tranche_ids:
        return []
    stmt = select(RepaymentDay).where(RepaymentDay.tranche_id.in_(tranche_ids))
    if statuses is not None:
        stmt = stmt.where(RepaymentDay.status.in_(statuses))
    return list((await db.execute(stmt.order_by(RepaymentDay.due_date, RepaymentDay.day_index))).scalars())


def _ict_noon(day: dt.date) -> dt.datetime:
    # A day with no recorded moment: midday in Vietnam, so it shows on that date in any timezone near it.
    return dt.datetime.combine(day, dt.time(5, 0), tzinfo=dt.timezone.utc)


def _investment_state(tranche: Tranche) -> tuple[str, dt.datetime] | None:
    """An investment as a movement of money: COMPLETED once received, PENDING while FundLok checks the receipt,
    FAILED if it was voided after money was sent; None when no money moved (reserved, lapsed, nothing sent)."""
    if tranche.status in RECEIVED:
        return "COMPLETED", tranche.confirmed_at or tranche.reserved_at
    if tranche.status in CHECKING or (tranche.status == "SIGNED" and tranche.receipt_submitted_at):
        return "PENDING", tranche.receipt_submitted_at or tranche.attested_at or tranche.reserved_at
    if tranche.status == "VOID" and (tranche.receipt_submitted_at or tranche.attested_at):
        return "FAILED", tranche.voided_at or tranche.updated_at
    return None


def _repayment_rows(
    days: list[RepaymentDay], *, type_: str, sign: int, codes: dict[UUID, str], names: dict[UUID, str]
) -> list[dict]:
    """One row per investment and working day (the first-period interest folded in), for the amount transferred:
    COMPLETED when FundLok matched the business's receipt, PENDING while it is the business's word, FAILED when the
    investor reported it never arrived."""
    by_day: dict[tuple[UUID, dt.date], list[RepaymentDay]] = defaultdict(list)
    for day in days:
        by_day[(day.tranche_id, day.due_date)].append(day)
    rows = []
    for (tranche_id, due_date), group in by_day.items():
        states = {day.status for day in group}
        moments = [day.matched_at or day.marked_paid_at for day in group if day.matched_at or day.marked_paid_at]
        rows.append({
            "id": f"ret-{tranche_id}-{due_date.isoformat()}",
            "tranche_id": tranche_id,
            "reference": codes[tranche_id],
            "type": type_,
            "status": "FAILED" if "REPORTED_MISSED" in states else "COMPLETED" if states == {"MATCHED"} else "PENDING",
            "created_at": max(moments) if moments else _ict_noon(due_date),
            "amount": sign * sum(day.net_vnd for day in group),
            "counterparty": names[tranche_id],
            "method_key": "dailyRepayment",
        })
    return rows


async def investor_transactions(db: AsyncSession, *, investor_id: UUID) -> list[dict]:
    """Every movement of the investor's money, newest first: each investment (out, once a receipt is in) and each
    day the business repaid them (in: what reached them, after the tax the business keeps back)."""
    tickets = await _tickets(db, investor_id)
    names = {tranche.id: (project.legal_name if project else "—") for tranche, project in tickets}
    codes = {tranche.id: tranche.reference_code for tranche, _ in tickets}
    rows: list[dict] = []
    for tranche, _project in tickets:
        state = _investment_state(tranche)
        if state is None:
            continue
        rows.append({
            "id": f"inv-{tranche.id}",
            "tranche_id": tranche.id,
            "reference": tranche.reference_code,
            "type": "INVESTMENT",
            "status": state[0],
            "created_at": state[1],
            "amount": -tranche.amount_vnd,
            "counterparty": names[tranche.id],
            "method_key": "bankTransfer",
        })
    days = await _days(db, list(codes))
    rows += _repayment_rows(days, type_="RETURN", sign=1, codes=codes, names=names)
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows


async def investor_analytics(db: AsyncSession, *, investor_id: UUID, today: dt.date) -> dict:
    """The investor's portfolio over time. Monthly, from 12 months back (or the first investment, if earlier) to
    this month: capital invested to date, and what came back each month, split into the capital returned and the
    earnings after tax (interest less the tax the business keeps back). By industry: the capital invested in each.
    By status: that same capital received but not repaying yet, still out repaying, or already returned (nothing is
    written down in the MVP, but the slice is kept: the investor bears the loss). Repayment days count once the
    business paid them, unless the investor reported them missing."""
    tickets = [(t, p) for t, p in await _tickets(db, investor_id) if t.status in RECEIVED]
    if not tickets:
        return {"monthly": [], "allocation": [], "capital_status": [], "active_positions": 0}
    paid = [day for day in await _days(db, [t.id for t, _ in tickets]) if day.status in PAID]

    received_on = {t.id: ict_date(t.confirmed_at) if t.confirmed_at else today for t, _ in tickets}
    first = min(min(received_on.values()), (today.replace(day=1) - dt.timedelta(days=330)).replace(day=1))
    monthly, returned_so_far = [], 0
    for month in _months(first, today):
        invested = sum(t.amount_vnd for t, _ in tickets if _month(received_on[t.id]) <= month)
        this_month = [day for day in paid if _month(day.due_date) == month]
        principal = sum(day.principal_vnd for day in this_month)
        earned = sum(day.interest_vnd - day.tax_withheld_vnd for day in this_month)
        returned_so_far += principal + earned
        monthly.append({
            "month": month,
            "deployed_cumulative": invested,
            "returns_cumulative": returned_so_far,
            "returns_monthly": principal + earned,
            "principal_monthly": principal,
            "yield_monthly": earned,
        })

    by_industry: dict[str, int] = defaultdict(int)
    for tranche, project in tickets:
        by_industry[(project.industry if project else None) or ""] += tranche.amount_vnd
    principal_back: dict[UUID, int] = defaultdict(int)
    for day in paid:
        principal_back[day.tranche_id] += day.principal_vnd
    waiting = sum(t.amount_vnd for t, _ in tickets if t.status == "CONFIRMED")
    out = sum(t.amount_vnd - principal_back[t.id] for t, _ in tickets if t.status == "ACTIVE")
    back = sum(principal_back.values())
    return {
        "monthly": monthly,
        "allocation": [
            {"industry": industry, "deployed": amount}
            for industry, amount in sorted(by_industry.items(), key=lambda item: -item[1])
        ],
        "capital_status": [
            {"statusKey": "active", "amount": waiting},
            {"statusKey": "repaying", "amount": out},
            {"statusKey": "completed", "amount": back},
            {"statusKey": "writtenDown", "amount": 0},
        ],
        "active_positions": sum(1 for t, _ in tickets if t.amount_vnd - principal_back[t.id] > 0),
    }


# --------------------------------------------------------------------------- #
# the business
# --------------------------------------------------------------------------- #
async def _business_tickets(db: AsyncSession, sme_user_id: UUID) -> list[tuple[Tranche, User, FundingWindow]]:
    return list(
        (
            await db.execute(
                select(Tranche, User, FundingWindow)
                .join(FundingWindow, FundingWindow.id == Tranche.window_id)
                .join(User, User.id == Tranche.investor_id)
                .where(FundingWindow.sme_user_id == sme_user_id)
            )
        ).all()
    )


async def business_transactions(db: AsyncSession, *, sme_user_id: UUID) -> list[dict]:
    """Every movement of the business's money, newest first: each investment it received (in), each day it repaid
    an investor (out: what it transferred, the investor's tax kept back), and FundLok's fee invoices (out: PENDING
    until paid). Investors are masked, as everywhere the business looks."""
    tickets = await _business_tickets(db, sme_user_id)
    names = {t.id: mask_name(investor.full_name or investor.email) for t, investor, _ in tickets}
    codes = {t.id: t.reference_code for t, _, _ in tickets}
    rows: list[dict] = []
    for tranche, _investor, _window in tickets:
        state = _investment_state(tranche)
        if state is None:
            continue
        rows.append({
            "id": f"inv-{tranche.id}",
            "tranche_id": tranche.id,
            "reference": tranche.reference_code,
            "type": "DISBURSEMENT",
            "status": state[0],
            "created_at": state[1],
            "amount": tranche.amount_vnd,
            "counterparty": names[tranche.id],
            "method_key": "bankTransfer",
        })
    days = await _days(db, list(codes))
    rows += _repayment_rows(days, type_="REPAYMENT", sign=-1, codes=codes, names=names)
    invoices = (
        await db.execute(select(FeeInvoice).where(FeeInvoice.payer_user_id == sme_user_id))
    ).scalars().all()
    for invoice in invoices:
        rows.append({
            "id": f"fee-{invoice.id}",
            "tranche_id": None,
            "reference": invoice.invoice_number,
            "type": "FEE",
            "status": {"PAID": "COMPLETED", "VOID": "FAILED"}.get(invoice.status, "PENDING"),
            "created_at": invoice.paid_at or invoice.issued_at,
            "amount": -invoice.total_vnd,
            "counterparty": "FundLok",
            "method_key": "platformFee",
        })
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows


async def _score(db: AsyncSession, window: FundingWindow) -> tuple[float | None, list[dict], int | None]:
    """The business score its round was offered on, its four group scores and their weights, and the average
    monthly revenue it was sized against (the last 12 months filed). Nones when the round has no locked score."""
    contract = await db.get(Contract, window.contract_id)
    run = await db.get(ScoreRun, contract.score_run_id) if contract and contract.score_run_id else None
    if run is None or not run.factor_results:
        return None, [], None
    params = load_params()
    premiums, final = compute_rollup(params, run.factor_results)
    factors = [
        {"key": key, "score": round(premiums[key], 2), "weight": weight}
        for key, weight in params.final_grade.items()
        if key in premiums
    ]
    inputs = (await db.execute(select(ScoreRunInput).where(ScoreRunInput.score_run_id == run.id))).scalar_one_or_none()
    revenue = [int(month) for month in (inputs.monthly_revenue_vnd or [])][-12:] if inputs else []
    basis = sum(revenue) // len(revenue) if revenue else None
    return (run.final_grade_precise if run.final_grade_precise is not None else final), factors, basis


async def business_analytics(db: AsyncSession, *, sme_user_id: UUID, today: dt.date) -> dict:
    """The business's loan: the sum of its investments, each repaying on its own from its approval. What it owes in
    all (tax included: each day's gross, the investors' tax kept back but still owed by the business), what it has
    repaid and what is left, its daily amount, the backstop date, month by month against its verified average
    monthly revenue, and the four groups of its business score. Repaid counts a day the business paid, unless the
    investor reported it missing. None facility before any investment repays."""
    tickets = [(t, w) for t, _, w in await _business_tickets(db, sme_user_id) if t.status == "ACTIVE"]
    if not tickets:
        return {"facility": None, "months": [], "score_factors": [], "revenue_basis_vnd": None}
    days = await _days(db, [t.id for t, _ in tickets], statuses=None)
    paid = [day for day in days if day.status in PAID]
    window = max((w for _, w in tickets), key=lambda w: w.created_at)
    score, factors, revenue_basis = await _score(db, window)

    unpaid_dates = sorted({day.due_date for day in days if day.status not in PAID})
    next_date = unpaid_dates[0] if unpaid_dates else None
    # This month's days (or, before the first one, the month of the next one): the "period" the daily amount runs in.
    period = _month(today) if any(_month(day.due_date) == _month(today) for day in days) else (
        _month(next_date) if next_date else _month(today)
    )
    period_days = [day for day in days if _month(day.due_date) == period]
    # Working days paid in a row, back from the latest one due.
    settled = defaultdict(lambda: True)
    for day in days:
        settled[day.due_date] &= day.status in PAID
    streak = 0
    for date in sorted((d for d in settled if d <= today), reverse=True):
        if not settled[date]:
            break
        streak += 1

    first_due = min(day.due_date for day in days) if days else today
    months, repaid_so_far = [], 0
    for month in _months(first_due, max(today, first_due)):
        repaid = sum(day.gross_vnd for day in paid if _month(day.due_date) == month)
        repaid_so_far += repaid
        months.append({
            "month": month,
            "repaid": repaid,
            "repaid_cumulative": repaid_so_far,
            "revenue_share": repaid / revenue_basis if revenue_basis else 0.0,
        })

    rate = investor_rate_pct(Decimal(window.annual_rate_pct), window.term_months, resolve(None))
    return {
        "facility": {
            "principal": sum(t.amount_vnd for t, _ in tickets),
            "total_obligation": sum(day.gross_vnd for day in days),
            "interest_rate_pct": float(rate),
            "term_months": window.term_months,
            "disbursed_at": min(ict_date(t.confirmed_at) for t, _ in tickets if t.confirmed_at),
            "days_remaining": len(unpaid_dates),
            "target_daily": sum(day.gross_vnd for day in days if day.due_date == next_date) if next_date else 0,
            "current_period_total": sum(day.gross_vnd for day in period_days),
            "current_period_end_date": max((day.due_date for day in period_days), default=today),
            "on_time_streak": streak,
            "score": score,
        },
        "months": months,
        "score_factors": factors,
        "revenue_basis_vnd": revenue_basis,
    }
