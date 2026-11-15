"""Investors earn the full graded rate (D-044, Appendix M 2.4): no flat investor return, no second figure.

The 2.5% yearly lender service fee is separate (invoiced, never in the repayment days).
"""
from decimal import Decimal

import pytest

from app.direct_transfer.close import investor_rate_pct
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.repayment import principal_for_day
from app.market.projection import project_ticket
from app.repayment_schedule.origination import compute_origination


def test_investor_earns_the_full_graded_rate():
    assert not hasattr(DirectTransferParams(), "investor_return_bps")
    for term in (1, 2, 4, 6):
        assert investor_rate_pct(Decimal("15"), term, DirectTransferParams()) == Decimal("15")
    assert investor_rate_pct(Decimal("15.443"), 3, DirectTransferParams()) == Decimal("15.443")


@pytest.mark.parametrize("term", [2, 4])
def test_offer_total_equals_repayment_plan_total(term):
    """The offer card (the ticket projection) and the repayment plan (the origination schedule) are one figure."""
    import datetime as dt

    amount, rate = 100_000_000, Decimal("15")
    orig = compute_origination(principal_vnd=amount, annual_rate_pct=rate, contractual_term_months=term)
    plan_total = orig.daily_amount_vnd * (orig.business_day_count - 1) + orig.final_instalment_vnd
    day = dt.date(2026, 11, 16)  # a Monday
    offer = project_ticket(
        amount_vnd=amount, loan_rate_pct=rate, term_months=term, individual=False, confirmed_on=day, close_on=day
    )
    instalments = [d for d in offer["days"] if d["kind"] == "INSTALMENT"]
    assert sum(d["gross_vnd"] for d in instalments) == plan_total == orig.total_obligation_vnd
    assert offer["basis"]["total_vnd"] == orig.total_obligation_vnd
    assert offer["basis"]["interest_vnd"] == orig.interest_vnd == amount * 15 * term // 1200
    assert sum(principal_for_day(amount, i, orig.business_day_count) for i in range(1, orig.business_day_count + 1)) == amount
