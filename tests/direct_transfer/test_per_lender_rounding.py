"""Each lender's schedule is exactly its own contract's figures (docs/specs/repayment_schedule/per-lender-rounding.md).

The contract (Articles 4 and 7.2) prints `repayment_estimate(own principal, rate, term)`: the daily instalment and the
last instalment. The schedule used to be the pooled loan's day split across lenders, which is 1 to 30 dong off.
"""
import pytest
from sqlalchemy import select

from app.direct_transfer.models import RepaymentDay, Tranche
from app.loans.agreement.terms import repayment_estimate

from .helpers import at, close_now, fund_window

M = 1_000_000
UNEVEN = [13_700_000, 26_300_000, 10_100_000, 49_900_000]  # 100M, so the pooled day splits unevenly


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="E2 not built: the schedule still splits the pooled day (HANDOFF-07 E2)"
)
async def test_each_lenders_schedule_equals_its_own_contract_figures(world):
    await fund_window(world, UNEVEN)
    window = await close_now(world, now=at(days=1))
    tranches = (await world.db.execute(select(Tranche).where(Tranche.window_id == window.id))).scalars().all()
    for t in tranches:
        own = repayment_estimate(t.amount_vnd, world.window.annual_rate_pct, world.window.term_months)
        gross = [
            d.gross_vnd
            for d in (
                await world.db.execute(
                    select(RepaymentDay)
                    .where(RepaymentDay.tranche_id == t.id, RepaymentDay.kind == "INSTALMENT")
                    .order_by(RepaymentDay.day_index)
                )
            ).scalars()
        ]
        assert len(gross) == own.instalment_days
        assert set(gross[:-1]) == {own.daily_vnd}, (t.amount_vnd, set(gross[:-1]), own.daily_vnd)
        assert gross[-1] == own.final_instalment_vnd, (t.amount_vnd, gross[-1], own.final_instalment_vnd)
        assert sum(gross) == own.total_vnd
