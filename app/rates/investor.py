"""Investor target-yield estimate for the public /rate page. Illustrative only.

WHY THIS IS SERVER-SIDE
The reference prototype (FundLok_LaiSuat_Page_Prototype.html, rev 6) computed
this in the browser, with the bank reference rate (BULR) and the risk-tier ->
internal-rating mapping as JS constants. Lok's standing rule is that neither is
ever shown externally, and the prototype's own header warns that its comments
and constants ship to the browser in plain text. Here the browser sends a tier
name and receives the published walk-down (loan rate, expected loss, fee, what
the investor keeps, the annualised figure), and nothing it could solve for the
reference rate from: two loan rates with two unknown ratings and an unknown
BULR is underdetermined.

WHERE EACH NUMBER COMES FROM
  * Loan rate per tier: the grading engine's own `price()`
    (app/underwriting/grading/pricing.py), fed the tier's internal rating and
    the bank rate in force from `bank_rate_config` -- the same table admin
    score runs read. So the investor tab and a real score run agree on what a
    loan in each tier is priced at, including the rate cap.
  * Everything else is the prototype's calibration, carried over verbatim with
    its provenance (fundlok-pricing-formula, "LOCKED PARAMETERS, Lok
    2026-08-27" and the 12-month-commitment simulation table). Balanced is the
    only locked tier; Conservative/Growth ratings and loss rates, and the
    6-month duration factor, are flagged approximations in the prototype and
    remain so here.

NOTE ON CALIBRATION
The cadence multipliers were fitted at the locked scenario (loan rate 14%,
net 10.5%, 12 months). If the board rate moves, loan rates move with it and
the walk-down stays exact, but the annualised figure is an extrapolation of a
curve fitted at one point. Re-run the simulation when the board rate changes
materially.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.projects.engine_constraints import params
from app.underwriting.grading.pricing import price

RiskTier = Literal["conservative", "balanced", "growth"]
Cadence = Literal["none", "quarterly", "monthly", "weekly", "daily"]

RISK_TIERS: tuple[RiskTier, ...] = ("conservative", "balanced", "growth")
CADENCES: tuple[Cadence, ...] = ("none", "quarterly", "monthly", "weekly", "daily")
COMMITMENT_MONTHS: tuple[int, ...] = (6, 12)

# FundLok's fee on each loan, in percentage points. Public since prototype
# rev 5 ("FundLok's fee is now public, per Lok").
FEE_PCT = 2.5

# Internal rating assumed for each tier. NEVER returned to a caller: with the
# rating and the loan rate, the pricing formula solves for the bank rate.
_TIER_RATING: dict[str, float] = {"conservative": 90.0, "balanced": 75.0, "growth": 60.0}

# Expected loss (PD x LGD x EAD), percentage points. Balanced = locked PD 2% x
# LGD 50%; the other two are the prototype's proposed scaling.
_TIER_EXPECTED_LOSS_PCT: dict[str, float] = {
    "conservative": 0.5,
    "balanced": 1.0,
    "growth": 2.0,
}

# Platform average loan length. Drives both the no-reinvestment case and the
# "capital turns over N times" line the page shows.
AVG_LOAN_MONTHS = 4

# From the 12-month simulation table (17.27 / 10.5 = 1.6448). The 6-month value
# is scaled from it -- the prototype's one remaining approximation.
_DURATION_FACTOR: dict[int, float] = {6: 1.5344, 12: 1.6448}
_CADENCE_MULTIPLIER: dict[str, float] = {
    "quarterly": 0.6173,
    "monthly": 1.0,
    "weekly": 1.2402,
    "daily": 1.3145,
}


@dataclass(frozen=True)
class InvestorEstimate:
    loan_rate_pct: float
    expected_loss_pct: float
    fee_pct: float
    net_per_loan_pct: float
    net_apy_pct: float
    estimated_return_vnd: int
    avg_loan_months: int
    capital_turns: float


def loan_rate_for_tier(tier: str, bank_rate_pct: float) -> float:
    """What a loan in this tier is priced at, by the engine's own formula."""
    result = price(
        final_grade=_TIER_RATING[tier],
        bank_rate_pct=bank_rate_pct,
        # `price` also derives a repayment schedule from these. Only the rate is
        # used here, and the rate does not depend on them, so any positive
        # values do; 1 keeps the Decimal arithmetic trivially exact.
        loan_size_vnd=Decimal(1),
        duration_months=1,
        revenue_y1_vnd=Decimal(1),
        params=params(),
    )
    return result.interest_rate_pct


def estimate(
    *,
    amount_vnd: int,
    commitment_months: int,
    tier: str,
    cadence: str,
    bank_rate_pct: float,
) -> InvestorEstimate:
    loan_rate = loan_rate_for_tier(tier, bank_rate_pct)
    expected_loss = _TIER_EXPECTED_LOSS_PCT[tier]
    net_per_loan = loan_rate - FEE_PCT - expected_loss

    # Two regimes, not one formula with a knob. Reinvesting: capital recycles,
    # use the simulation-derived factors. Not reinvesting: one ~4-month loan
    # then idle cash, so scale by the share of the window the money worked.
    # (The prototype's rev 4 used a multiplier here and reported 3.26% for a
    # 6-month commitment where the honest answer is 7.00%.)
    if cadence == "none":
        factor = AVG_LOAN_MONTHS / commitment_months
    else:
        factor = _DURATION_FACTOR[commitment_months] * _CADENCE_MULTIPLIER[cadence]
    net_apy = net_per_loan * factor

    return InvestorEstimate(
        loan_rate_pct=round(loan_rate, 2),
        expected_loss_pct=expected_loss,
        fee_pct=FEE_PCT,
        net_per_loan_pct=round(net_per_loan, 2),
        net_apy_pct=round(net_apy, 2),
        estimated_return_vnd=round(amount_vnd * net_apy / 100),
        avg_loan_months=AVG_LOAN_MONTHS,
        capital_turns=round(commitment_months / AVG_LOAN_MONTHS, 1),
    )
