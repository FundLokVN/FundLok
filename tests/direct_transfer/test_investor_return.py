"""Investors earn a flat return over the whole loan (investor_return_bps, 2.5%), not the business's yearly rate.

The loan is formed at that return as a yearly rate (2.5% over 4 months is 7.5% a year), capped at the business's own
rate so a short loan never goes above it (or above the legal cap).
"""
from decimal import Decimal

from app.direct_transfer.close import investor_rate_pct
from app.direct_transfer.config import DirectTransferParams

PARAMS = DirectTransferParams()  # the product default: 2.5% over the loan


def test_two_and_a_half_percent_over_the_loan_whatever_its_term():
    for term, yearly in ((2, Decimal("15")), (3, Decimal("10")), (4, Decimal("7.5")), (6, Decimal("5"))):
        assert investor_rate_pct(Decimal("16"), term, PARAMS) == yearly
        # Over the term, that is 2.5% of the principal.
        assert yearly * term / 12 == Decimal("2.5")


def test_never_above_the_business_s_own_rate():
    # 2.5% over 1 month would be 30% a year: the business's 15.44% is the most it can be.
    assert investor_rate_pct(Decimal("15.443"), 1, PARAMS) == Decimal("15.443")


def test_without_a_flat_return_investors_earn_the_business_s_rate():
    assert investor_rate_pct(Decimal("16"), 4, DirectTransferParams(investor_return_bps=None)) == Decimal("16")
