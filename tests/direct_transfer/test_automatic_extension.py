"""Automatic extension (docs/specs/repayment_schedule/automatic-extension.md, D-042)."""
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.timezone import ICT
from app.direct_transfer import extension
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import RepaymentDay, Tranche
from app.lending.models import ScoreRun
from app.notifications.models import Notification
from app.repayment_schedule.models import Facility, ScheduleVersion

M = 1_000_000


# --------------------------------------------------------------------------- #
# Money flow worked example: 100M over 2 months at 15%, 42 working days; investors A 40M, B 30M, C 20M, D 10M.
# Month 1 (21 days) is paid at the original daily amounts; month-1 revenue 205,000,000; s = 0.2.
# --------------------------------------------------------------------------- #
EXAMPLE = {  # P: (T, original daily, extra, new T, new daily, fee increase)
    40 * M: (41_000_000, 976_190, 119_048, 41_119_048, 793_040, 19_841),
    30 * M: (30_750_000, 732_142, 89_286, 30_839_286, 594_780, 14_881),
    20 * M: (20_500_000, 488_095, 59_524, 20_559_524, 396_520, 9_921),
    10 * M: (10_250_000, 244_047, 29_762, 10_279_762, 198_260, 4_960),
}


def test_money_flow_worked_example_exact_figures():
    due = sum(21 * daily for _, daily, *_ in EXAMPLE.values())
    assert due == 51_249_954
    shortfall = extension.shortfall_vnd(due, Decimal("0.2"), 205_000_000)
    assert shortfall == 10_249_954
    delta = extension.extension_days(shortfall, due, room_days=None)
    assert delta == 5  # ceil(4.2) = 5, already a multiple of 5
    for principal, (total, daily, extra, new_total, new_daily, fee) in EXAMPLE.items():
        terms = extension.investor_terms(
            principal_vnd=principal, annual_rate_pct=15, delta_days=delta, total_vnd=total, paid_vnd=21 * daily,
            days_left=21, old_daily_vnd=daily,
        )
        assert terms == extension.InvestorTerms(extra, new_total, new_daily, fee), principal


def test_delta_rounds_up_to_a_multiple_of_five_and_is_capped_at_the_backstop_room():
    assert extension.extension_days(10_249_954, 51_249_954, room_days=None) == 5
    assert extension.extension_days(14_000_000, 49_000_000, room_days=None) == 10  # ceil(6.0) = 6 -> 10
    assert extension.extension_days(14_000_000, 49_000_000, room_days=7) == 7  # capped at the room to the backstop
    assert extension.extension_days(0, 49_000_000, room_days=28) == 0
    assert extension.extension_days(-5, 49_000_000, room_days=28) == 0


def test_partial_month_counts_revenue_for_the_loan_s_working_days_only():
    # 19 of the month's 21 working days: s x revenue x 19/21.
    assert extension.shortfall_vnd(23_825_392, Decimal("0.5"), 30_000_000, loan_days=19, month_days=21) == 23_825_392 - 13_571_429


def test_new_daily_amount_never_exceeds_the_old_one():
    with pytest.raises(DirectTransferError) as info:
        extension.investor_terms(
            principal_vnd=40 * M, annual_rate_pct=15, delta_days=5, total_vnd=41_000_000, paid_vnd=20_499_990,
            days_left=21, old_daily_vnd=700_000,
        )
    assert info.value.code == "EXTENSION_RAISES_DAILY"


# --------------------------------------------------------------------------- #
# filing (the `loan` fixture: 10 x 10M, 4 months at 16%, repaying from Wed 4 Nov 2026, one pooled facility)
# --------------------------------------------------------------------------- #
def ict(m, d, h=10):
    return dt.datetime(2026, m, d, h, tzinfo=ICT)


async def _file(world, window, *, revenue, now=ict(12, 3), month=dt.date(2026, 11, 1)):
    return await extension.file_extension(
        world.db, window_id=window.id, sme_user_id=world.sme.id, period_month=month, revenue_vnd=revenue,
        evidence_kind="TAX_DECLARATION", content=f"%PDF-1.4 declaration {uuid.uuid4()}".encode(),
        content_type="application/pdf", filename="declaration.pdf", storage=world.storage, now=now, params=world.params,
    )


async def _with_share(world, share="0.5"):
    world.score_run.daily_repayment_rate = Decimal(share)
    await world.db.flush()


async def _rows(db, tranche_id):
    return (
        await db.execute(
            select(RepaymentDay).where(RepaymentDay.tranche_id == tranche_id, RepaymentDay.kind == "INSTALMENT").order_by(RepaymentDay.day_index)
        )
    ).scalars().all()


async def test_filing_extends_every_investor_by_the_same_days_and_keeps_the_old_version(loan):
    world, window, tr = loan
    await _with_share(world)
    before = {t.id: await _rows(world.db, t.id) for t in tr}
    old_total = {t: sum(r.gross_vnd for r in rows) for t, rows in before.items()}
    old_daily = {t: rows[-2].gross_vnd for t, rows in before.items()}
    old_fee = {t.id: t.investor_fee_total_vnd for t in tr}
    facility = await world.db.get(Facility, window.facility_id)
    old_obligation = int(facility.total_obligation_vnd)

    filing = await _file(world, window, revenue=30 * M)
    # November: 19 of 21 working days; s x revenue x 19/21 = 13,571,429 against the instalments due.
    assert filing.status == "EXTENDED" and filing.delta_days == 10 and filing.start_date == dt.date(2026, 12, 4)
    assert filing.shortfall_vnd == filing.due_vnd - 13_571_429
    extra = round(10 * M * 16 * 10 / 100 / 252)  # 63,492 per investor
    for t in tr:
        rows = await _rows(world.db, t.id)
        assert len(rows) == len(before[t.id]) + 10  # the same Δ for every investor
        assert sum(r.gross_vnd for r in rows) == old_total[t.id] + extra
        new = [r for r in rows if r.due_date >= filing.start_date]
        assert len({r.gross_vnd for r in new[:-1]}) == 1 and new[0].gross_vnd <= old_daily[t.id]  # never higher
        assert all(r.gross_vnd == r.principal_vnd + r.interest_vnd for r in rows)
        assert sum(r.principal_vnd for r in rows) == t.amount_vnd
        assert t.investor_fee_total_vnd == old_fee[t.id] + 9_921  # 10M x 2.5% x 10 / 252
    # The facility's T grows by the extra interest and still equals its rows; the backstop does not move.
    total_rows = (
        await world.db.execute(select(func.sum(RepaymentDay.gross_vnd)).where(RepaymentDay.facility_id == facility.id, RepaymentDay.kind == "INSTALMENT"))
    ).scalar_one()
    assert int(facility.total_obligation_vnd) == old_obligation + 10 * extra == total_rows
    versions = (await world.db.execute(select(ScheduleVersion.version_number).where(ScheduleVersion.facility_id == facility.id))).scalars().all()
    assert sorted(versions) == [1, 2]
    assert len(filing.previous_rows) == sum(len([r for r in rows if r.due_date >= filing.start_date]) for rows in before.values())
    told = (await world.db.execute(select(Notification.user_id).where(Notification.event == "EXTENSION_APPLIED"))).scalars().all()
    assert sorted(map(str, told)) == sorted(map(str, [t.investor_id for t in tr] + [world.sme.id]))
    with pytest.raises(DirectTransferError) as info:
        await _file(world, window, revenue=30 * M)
    assert info.value.code == "ALREADY_FILED"


async def test_no_shortfall_records_the_filing_without_extending(loan):
    world, window, tr = loan
    await _with_share(world)
    filing = await _file(world, window, revenue=100 * M)
    assert filing.status == "NO_SHORTFALL" and filing.delta_days == 0 and filing.previous_rows is None
    assert len(await _rows(world.db, tr[0].id)) == 84


async def test_filing_window_is_the_first_five_days_of_the_next_month(loan):
    world, window, _ = loan
    await _with_share(world)
    for now in (ict(11, 30), ict(12, 6)):
        with pytest.raises(DirectTransferError) as info:
            await _file(world, window, revenue=30 * M, now=now)
        assert info.value.code == "FILING_CLOSED"


async def test_a_late_or_disputed_day_blocks_the_filing(loan):
    world, window, tr = loan
    await _with_share(world)
    (await _rows(world.db, tr[0].id))[0].status = "REPORTED_MISSED"
    with pytest.raises(DirectTransferError) as info:
        await _file(world, window, revenue=30 * M)
    assert info.value.code == "LATE_DAY_BLOCKS_FILING"


async def test_filing_needs_the_revenue_share_set_at_underwriting(loan):
    world, window, _ = loan
    with pytest.raises(DirectTransferError) as info:
        await _file(world, window, revenue=30 * M)
    assert info.value.code == "REVENUE_SHARE_UNKNOWN"
