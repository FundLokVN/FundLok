"""A projected repayment schedule for an investor's ticket before its round forms the loan.

The real days are created when the round closes (app/direct_transfer/close.py `_form_loan` ->
`generate_repayment_days`). Until then the investor sees what those days would be, with the same arithmetic: the
investors' rate (`investor_rate_pct`: their flat return over the loan, capped at the business's rate), the term
(`compute_origination`), the business-day calendar, `principal_for_day`, the withholding on interest for individual
investors, and the first-period (stub) interest from the day the transfer was confirmed.

Two assumptions, both said on the page: the round closes on its last day (the latest it can, so the dates are the
latest they can be), and the ticket is sized on its own. The real schedule splits the whole loan's days pro rata
across every confirmed ticket, so a day can differ by a đồng. Projected, never guaranteed: repayment depends on the
business, and the investor bears the credit risk.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from app.direct_transfer.calendar_utils import count_business_days, default_calendar, first_working_day_after
from app.direct_transfer.close import compute_stub_interest, investor_rate_pct
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.repayment import principal_for_day, rdiv
from app.repayment_schedule.calendar import BusinessDayCalendar
from app.repayment_schedule.origination import compute_origination


@dataclasses.dataclass(frozen=True)
class ProjectedDay:
    kind: str  # INSTALMENT | STUB
    day_index: int
    due_date: dt.date
    principal_vnd: int
    interest_vnd: int
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int


def project_ticket(
    *,
    amount_vnd: int,
    loan_rate_pct: Decimal,
    term_months: int,
    individual: bool,
    confirmed_on: dt.date,
    close_on: dt.date,
    params: DirectTransferParams | None = None,
    calendar: BusinessDayCalendar | None = None,
) -> dict:
    """The ticket's days if its round closed on `close_on`: the first-period interest (due on the start date) and
    one instalment per business day, with the totals. Mirrors generate_repayment_days for one ticket."""
    p = resolve(params)
    cal = calendar or default_calendar()
    annual_rate_pct = investor_rate_pct(loan_rate_pct, term_months, p)
    start = first_working_day_after(close_on, cal)
    orig = compute_origination(
        principal_vnd=amount_vnd, annual_rate_pct=annual_rate_pct, contractual_term_months=term_months,
        working_days_per_month=p.working_days_per_month,
    )
    n_days = orig.business_day_count
    dates = cal.generate_business_days(start, n_days)

    instalments = []
    for index, due in enumerate(dates, start=1):
        gross = orig.final_instalment_vnd if index == n_days else orig.daily_amount_vnd
        principal = principal_for_day(amount_vnd, index, n_days)
        instalments.append((index, due, principal, gross - principal, gross))
    # Withholding as generate_repayment_days does it: floored per day, the last day carrying the true-up.
    if individual:
        taxes = [interest * p.withholding_bps // 10_000 for _, _, _, interest, _ in instalments[:-1]]
        total_tax = rdiv(sum(i[3] for i in instalments) * p.withholding_bps, 10_000)
        taxes.append(min(instalments[-1][3], max(0, total_tax - sum(taxes))))
    else:
        taxes = [0] * len(instalments)

    days: list[ProjectedDay] = []
    stub = compute_stub_interest(
        amount_vnd, annual_rate_pct, count_business_days(confirmed_on, start, cal), p.stub_basis_days
    )
    if stub > 0:
        stub_tax = stub * p.withholding_bps // 10_000 if individual else 0
        days.append(ProjectedDay("STUB", 0, start, 0, stub, stub, stub_tax, stub - stub_tax))
    for (index, due, principal, interest, gross), tax in zip(instalments, taxes):
        days.append(ProjectedDay("INSTALMENT", index, due, principal, interest, gross, tax, gross - tax))

    tax_total = sum(day.tax_withheld_vnd for day in days)
    return {
        "assumed_close_date": close_on,
        "start_date": start,
        "business_day_count": n_days,
        # Each step of the arithmetic, for a person to follow: the return, the days, the daily amount, the dates,
        # the first-period interest and the tax.
        "basis": {
            "principal_vnd": amount_vnd,
            "loan_rate_pct": Decimal(loan_rate_pct),
            "investor_return_pct": None,  # D-044: investors earn the full graded rate, no flat return
            "investor_rate_pct": annual_rate_pct,
            "term_months": term_months,
            "working_days_per_month": p.working_days_per_month,
            "business_day_count": n_days,
            "interest_vnd": orig.interest_vnd,
            "total_vnd": orig.total_obligation_vnd,
            "daily_vnd": orig.daily_amount_vnd,
            "final_vnd": orig.final_instalment_vnd,
            "received_on": confirmed_on,
            "approved_on": close_on,
            "start_date": start,
            "end_date": dates[-1],
            "stub_days": count_business_days(confirmed_on, start, cal),
            "stub_basis_days": p.stub_basis_days,
            "stub_interest_vnd": stub,
            "individual": individual,
            "withholding_pct": Decimal(p.withholding_bps) / 100,
            "tax_vnd": tax_total,
        },
        "days": [dataclasses.asdict(day) for day in days],
        "totals": {
            "gross_vnd": sum(day.gross_vnd for day in days),
            "tax_withheld_vnd": tax_total,
            "net_vnd": sum(day.net_vnd for day in days),
        },
    }


def sum_by_day(days: list[dict]) -> list[dict]:
    """Many tickets' days as the business pays them: one row per date with the totals and how many tickets."""
    by_date: dict[dt.date, dict] = {}
    for day in days:
        row = by_date.setdefault(
            day["due_date"],
            {"due_date": day["due_date"], "principal_vnd": 0, "interest_vnd": 0, "gross_vnd": 0, "tax_withheld_vnd": 0,
             "net_vnd": 0, "tickets": 0},
        )
        row["principal_vnd"] += day["principal_vnd"]
        row["interest_vnd"] += day["interest_vnd"]
        row["gross_vnd"] += day["gross_vnd"]
        row["tax_withheld_vnd"] += day["tax_withheld_vnd"]
        row["net_vnd"] += day["net_vnd"]
        row["tickets"] += 1
    return [by_date[date] for date in sorted(by_date)]
