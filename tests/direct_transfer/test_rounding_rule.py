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


# --- many combinations (term 1-6, rates up to 20%, principals 10M-100M, pooled lender shares) -------------------

from app.direct_transfer.close import investor_rate_pct  # noqa: E402
from app.direct_transfer.config import resolve  # noqa: E402

PARAMS = resolve(None)  # investors earn the full graded rate (D-044)
from app.direct_transfer.repayment import rdiv, split_gross  # noqa: E402
from app.repayment_schedule.origination import compute_origination  # noqa: E402

RATES = [Decimal(r) for r in ("0.5", "1", "1.5", "5", "7.5", "12", "15", "15.443", "16", "19.99", "20")]
# round figures, plus awkward ones where nearest-rounded daily tax adds up above the exact total (low rates)
PRINCIPALS = list(range(10_000_000, 100_000_001, 1_000_000)) + [10_626_000, 13_756_000, 14_695_000, 15_634_000]
COMBOS = [(p, r, t) for t in range(1, 7) for r in RATES for p in PRINCIPALS]


def _grosses(principal, rate, term):
    rate = investor_rate_pct(rate, term, PARAMS)  # the graded rate in full (D-044)
    o = compute_origination(
        principal_vnd=principal, annual_rate_pct=rate, contractual_term_months=term,
        working_days_per_month=21,
    )
    return o, [o.daily_amount_vnd] * (o.business_day_count - 1) + [o.final_instalment_vnd]


def _unclamped_last_tax(interests):
    early = sum(tax_nearest(i, 500) for i in interests[:-1])
    return rdiv(sum(interests) * 500, 10_000) - early


def test_every_combination_splits_principal_interest_and_tax_exactly():
    day = dt.date(2026, 11, 16)
    for principal, rate, term in COMBOS:
        o, grosses = _grosses(principal, rate, term)
        if _unclamped_last_tax(split_instalment_interest(grosses, principal)) < 0:
            continue  # the overshoot cases: see test_nearest_tax_never_overshoots_the_exact_total
        plan = project_ticket(
            amount_vnd=principal, loan_rate_pct=rate, term_months=term, individual=True, confirmed_on=day, close_on=day,
            params=PARAMS,
        )
        rows = [d for d in plan["days"] if d["kind"] == "INSTALMENT"]
        where = (principal, rate, term)
        assert sum(r["principal_vnd"] for r in rows) == principal, where
        assert sum(r["interest_vnd"] for r in rows) == o.interest_vnd, where
        assert [r["gross_vnd"] for r in rows] == grosses, where
        assert all(r["tax_withheld_vnd"] >= 0 and r["net_vnd"] == r["gross_vnd"] - r["tax_withheld_vnd"] for r in rows), where
        # No earlier day is adjusted: each is exactly the nearest dong of its own interest.
        assert all(r["tax_withheld_vnd"] == tax_nearest(r["interest_vnd"], 500) for r in rows[:-1]), where
        # Total tax is the exact 5% of the total interest, to the dong.
        assert sum(r["tax_withheld_vnd"] for r in rows) == rdiv(o.interest_vnd * 500, 10_000), where


def test_pooled_lender_shares_split_principal_and_interest_exactly():
    for principal, rate, term in COMBOS[::7]:
        o, grosses = _grosses(principal, rate, term)
        weights = [10_000_000, principal // 3 // 1_000_000 * 1_000_000 or 10_000_000]
        weights.append(principal - sum(weights)) if principal > sum(weights) else None
        weights = [w for w in weights if w > 0]
        splits = [split_gross(g, weights) for g in grosses]
        for i, w in enumerate(weights):
            mine = [s[i] for s in splits]
            interests = split_instalment_interest(mine, w)
            assert sum(interests) == sum(mine) - w, (principal, rate, term, w)
            assert all(g - x > 0 for g, x in zip(mine, interests)), (principal, rate, term, w)


@pytest.mark.xfail(  # remove this marker once Edward picks the fix
    strict=True,
    reason="OPEN, Edward to choose: at low rates (0.5% to 1.5%) nearest-rounded daily tax sums above the exact total, "
    "so the last day would need a negative true-up. The code clamps it to 0, withholding up to ~47 dong too much.",
)
def test_nearest_tax_never_overshoots_the_exact_total():
    """The last day's true-up must not be negative: nearest-rounded daily tax must not sum above the exact total."""
    offenders = []
    for principal, rate, term in COMBOS:
        _, grosses = _grosses(principal, rate, term)
        interests = split_instalment_interest(grosses, principal)
        if _unclamped_last_tax(interests) < 0:
            offenders.append((principal, str(rate), term))
    assert not offenders, offenders
