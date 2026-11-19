"""Late interest (docs/specs/repayment_schedule/late-interest.md, D-041). The calculation only: no ledger leg yet."""
import datetime as dt

from app.direct_transfer.repayment import late_interest_vnd

DUE = dt.date(2026, 11, 4)


def test_late_interest_uses_150_percent_of_rate_on_principal_and_10_percent_on_interest():
    # 10 days late on 1,000,000 principal and 50,000 interest at r = 16%: 24% on principal, 10% on interest.
    # 1,000,000 x 24% x 10/365 = 6,575.34 ; 50,000 x 10% x 10/365 = 136.99 ; total 6,712.33 -> 6,712
    got = late_interest_vnd(1_000_000, 50_000, annual_rate_pct="16.000", due_date=DUE, received_on=DUE + dt.timedelta(days=10))
    assert got == 6_712


def test_principal_rate_is_capped_at_30_percent():
    # r = 24%: 150% would be 36%, capped at 30%. 365 days on 1,000,000 principal: exactly 300,000.
    assert late_interest_vnd(1_000_000, 0, annual_rate_pct=24, due_date=DUE, received_on=DUE + dt.timedelta(days=365)) == 300_000
    assert late_interest_vnd(1_000_000, 0, annual_rate_pct=20, due_date=DUE, received_on=DUE + dt.timedelta(days=365)) == 300_000
    assert late_interest_vnd(1_000_000, 0, annual_rate_pct=16, due_date=DUE, received_on=DUE + dt.timedelta(days=365)) == 240_000


def test_calendar_days_after_due_date_on_365_day_year_rounded_half_up():
    # Calendar days, weekends included: Wed 4 Nov to Mon 9 Nov is 5 days.
    # 73,000 x 10% x 5/365 = 100.0 ; 36,500 x 24% x 5/365 = 120.0
    assert late_interest_vnd(36_500, 73_000, annual_rate_pct=16, due_date=DUE, received_on=dt.date(2026, 11, 9)) == 220
    # 0.5 rounds up: 1,825 x 10% x 1/365 = 0.5 -> 1 ; 1,824 -> 0.4997 -> 0
    assert late_interest_vnd(0, 1_825, annual_rate_pct=16, due_date=DUE, received_on=DUE + dt.timedelta(days=1)) == 1
    assert late_interest_vnd(0, 1_824, annual_rate_pct=16, due_date=DUE, received_on=DUE + dt.timedelta(days=1)) == 0


def test_no_late_interest_when_received_by_end_of_due_date():
    assert late_interest_vnd(1_000_000, 50_000, annual_rate_pct=16, due_date=DUE, received_on=DUE) == 0
    assert late_interest_vnd(1_000_000, 50_000, annual_rate_pct=16, due_date=DUE, received_on=DUE - dt.timedelta(days=1)) == 0
