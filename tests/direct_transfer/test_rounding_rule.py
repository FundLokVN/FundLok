"""D-045 rounding: interest part = A x I / T rounded down, tax to the nearest dong, principal the remainder, the last
instalment closes the total and the tax. Handbook 10.8 (P1: 100M at 15%, 2 months, 42 instalments)."""
import datetime as dt
from decimal import Decimal

import pytest

from app.direct_transfer.repayment import split_instalment_interest, tax_nearest
from app.market.projection import project_ticket

# lender principal: (daily, day-1 interest, tax/day, transfer/day, last instalment, total tax)
VECTORS = {
    40_000_000: (976_190, 23_809, 1_190, 975_000, 976_210, 50_000),
    30_000_000: (732_142, 17_857, 893, 731_249, 732_178, 37_500),
    20_000_000: (488_095, 11_904, 595, 487_500, 488_105, 25_000),
    10_000_000: (244_047, 5_952, 298, 243_749, 244_073, 12_500),
}


@pytest.mark.parametrize("amount", list(VECTORS))
def test_p1_money_check_matches_handbook_10_8(amount):
    daily, interest, tax, transfer, last, total_tax = VECTORS[amount]
    day = dt.date(2026, 11, 16)
    plan = project_ticket(
        amount_vnd=amount, loan_rate_pct=Decimal("15"), term_months=2, individual=True, confirmed_on=day, close_on=day
    )
    rows = [d for d in plan["days"] if d["kind"] == "INSTALMENT"]
    assert len(rows) == 42
    first, final = rows[0], rows[-1]
    assert (first["gross_vnd"], first["interest_vnd"], first["tax_withheld_vnd"], first["net_vnd"]) == (
        daily, interest, tax, transfer,
    )
    assert final["gross_vnd"] == last
    assert sum(r["tax_withheld_vnd"] for r in rows) == total_tax
    assert sum(r["principal_vnd"] for r in rows) == amount
    assert sum(r["gross_vnd"] for r in rows) == amount + amount * 15 * 2 // 1200
    assert all(r["principal_vnd"] > 0 and r["tax_withheld_vnd"] <= r["interest_vnd"] for r in rows)


def test_interest_rounds_down_and_the_last_day_closes_it():
    grosses = [732_142] * 41 + [732_178]
    interest = split_instalment_interest(grosses, 30_000_000)
    assert interest[0] == 17_857 and sum(interest) == 750_000


def test_tax_rounds_to_the_nearest_dong():
    assert tax_nearest(17_857, 500) == 893  # 892.85
    assert tax_nearest(5_952, 500) == 298  # 297.6
    assert tax_nearest(23_809, 500) == 1_190  # 1190.45
