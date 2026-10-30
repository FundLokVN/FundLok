"""Golden numbers from Loc.md (HANDOFF-04 I-18): reproduced to the dong.

100M at 16% flat over 4 months = 84 working days (4 x 21, Q-011 resolved A), 10 investors of 10M.
"""
from sqlalchemy import func, select

from app.direct_transfer import fees
from app.direct_transfer.models import RepaymentDay
from app.repayment_schedule.models import Facility
from app.repayment_schedule.origination import compute_origination

from .helpers import at, close_now, fund_window

M = 1_000_000


def test_golden_origination_100m_16pct_84_days():
    from decimal import Decimal

    r = compute_origination(principal_vnd=100 * M, annual_rate_pct=Decimal("16"), contractual_term_months=4, working_days_per_month=21)
    assert r.business_day_count == 84
    assert r.interest_vnd == 5_333_333 and r.total_obligation_vnd == 105_333_333
    assert r.daily_amount_vnd == 1_253_968 and r.remainder_vnd == 21 and r.final_instalment_vnd == 1_253_989


async def test_golden_investor_split_119048_plus_6349(world):
    tr = await fund_window(world, [10 * M] * 10)
    window = await close_now(world, now=at(days=1))
    facility = await world.db.get(Facility, window.facility_id)
    assert facility.business_day_count == 84 and int(facility.total_obligation_vnd) == 105_333_333
    day1 = (
        await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id, RepaymentDay.kind == "INSTALMENT", RepaymentDay.day_index == 1))
    ).scalar_one()
    assert (day1.gross_vnd, day1.principal_vnd, day1.interest_vnd) == (125_397, 119_048, 6_349)
    # all ten investors together pay exactly the scheduled instalment (largest remainder: 8 pay 125,397, 2 pay 125,396)
    grosses = sorted(
        (await world.db.execute(select(RepaymentDay.gross_vnd).where(RepaymentDay.kind == "INSTALMENT", RepaymentDay.day_index == 1))).scalars().all()
    )
    assert sum(grosses) == 1_253_968 and set(grosses) == {125_396, 125_397}


async def test_golden_tax_317_and_net_125080(world):
    tr = await fund_window(world, [10 * M] * 10)
    await close_now(world, now=at(days=1))
    day1 = (
        await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id, RepaymentDay.kind == "INSTALMENT", RepaymentDay.day_index == 1))
    ).scalar_one()
    assert day1.tax_withheld_vnd == 317 and day1.net_vnd == 125_080


async def test_golden_840_transfers_per_loan(world):
    await fund_window(world, [10 * M] * 10)
    await close_now(world, now=at(days=1))
    n = (await world.db.execute(select(func.count()).select_from(RepaymentDay).where(RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    assert n == 840


def test_golden_fees_3_000_000_and_833_333():
    fee, vat, total = fees.sme_fee_parts(100 * M)
    assert (fee, vat, total) == (3_000_000, 300_000, 3_300_000)
    assert fees.investor_fee_total(10 * M, 4) == 83_333
    assert fees.investor_fee_total(100 * M, 4) == 833_333
