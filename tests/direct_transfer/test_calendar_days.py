"""SCRUM-625 [AM-E20]: every deadline and window counts calendar time (D-046, D-049, Loc 15 Nov 2026).

Daily repayments stay Monday to Friday; only the deadlines below stopped skipping weekends.
"""
import dataclasses
import datetime as dt

from sqlalchemy import select

from app.core.timezone import ICT
from app.direct_transfer import repayment
from app.direct_transfer.calendar_utils import calendar_deadline
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import Dispute, Transfer

from .helpers import at, attested_tranche, close_now, fund_window, make_investor, open_window, pay_days_through
from .test_disputes import sme_disputed
from .test_repayment import day_rows, ict

import pytest

M = 1_000_000


async def test_sme_confirm_window_is_24_calendar_hours_across_a_weekend(world):
    await open_window(world)
    inv = await make_investor(world)
    friday = at(days=4, hours=6)  # a receipt checked Fri 6 Nov 2026 16:00 ICT
    t = await attested_tranche(world, inv, 10 * M, now=friday)
    assert t.attested_at == friday + dt.timedelta(minutes=5)  # the helper checks the receipt five minutes after
    assert t.confirm_deadline == t.attested_at + dt.timedelta(hours=24)
    assert t.confirm_deadline == dt.datetime(2026, 11, 7, 16, 5, tzinfo=ICT)  # Saturday 16:05, not Tuesday


async def test_lender_report_window_is_24_hours_after_the_due_date_ends(loan):
    world, _, _ = loan
    wed = (await day_rows(world.db, kind="INSTALMENT", day_index=1))[0]  # due Wed 4 Nov
    assert wed.report_deadline == ict(2026, 11, 6)  # the due date ends Thu 00:00, plus 24 hours
    fri = (await day_rows(world.db, kind="INSTALMENT", day_index=3))[0]  # due Fri 6 Nov
    assert fri.report_deadline == ict(2026, 11, 8)  # Sunday 00:00, weekend not skipped


async def test_late_report_cap_is_30_calendar_days(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 5))  # marked paid Wed 4 Nov 10:00
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=1))[0]
    assert day.report_deadline == ict(2026, 11, 5, 10)  # the proof plus 24 hours
    cap = ict(2026, 12, 5, 10)  # report_deadline + 30 days
    with pytest.raises(DirectTransferError) as info:
        await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="too late", file_id=None, now=cap, params=world.params)
    assert info.value.code == "REPORT_WINDOW_CLOSED"
    out = await repayment.report_missing_day(
        world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="just in time", file_id=None,
        now=cap - dt.timedelta(seconds=1), params=world.params,
    )
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == out.id))).scalar_one()
    assert d.cause == "LATE_REPORT"


async def test_close_wait_is_calendar_days(world):
    await fund_window(world, [60 * M])
    inv = await make_investor(world)
    await attested_tranche(world, inv, 10 * M, now=at(days=14, hours=-1))
    window = await close_now(world, now=at(days=14))  # Mon 16 Nov 10:00 ICT
    assert window.status == "CLOSING"
    assert window.close_wait_until == at(days=16)  # exactly two days later, Wed 18 Nov 10:00


async def test_refund_deadline_is_calendar_days(world):
    tr = await fund_window(world, [10 * M] * 4)  # below the minimum raise: every tranche refunded
    window = await close_now(world, now=at(days=14))
    assert window.status == "FAILED"
    refunds = (await world.db.execute(select(Transfer).where(Transfer.kind == "REFUND"))).scalars().all()
    assert len(refunds) == len(tr) and all(r.due_by == at(days=16) for r in refunds)


async def test_dispute_response_deadline_is_calendar_days(loan):
    world, _, tr = loan
    friday = ict(2026, 11, 6, 10)
    await pay_days_through(world, ict(2026, 11, 7), paid_at=dt.time(9))  # each day marked paid 09:00 on its due date
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=3))[0]  # due Fri 6 Nov
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="not received", file_id=None, now=friday, params=world.params)
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    assert d.response_deadline == ict(2026, 11, 8, 10)  # Sunday 10:00, not Tuesday


async def test_sme_dispute_response_deadline_is_calendar_days(world):
    inv, t, d2 = await sme_disputed(world, now=at(days=4, hours=6))  # Fri 16:00, disputed 16:30
    assert d2.response_deadline == dt.datetime(2026, 11, 8, 16, 30, tzinfo=ICT)


def test_calendar_deadline_is_a_pure_timedelta():
    moment = dt.datetime(2026, 11, 6, 16, 0, tzinfo=ICT)
    assert calendar_deadline(moment, hours=24) == dt.datetime(2026, 11, 7, 16, 0, tzinfo=ICT)
    assert calendar_deadline(moment, days=2) == dt.datetime(2026, 11, 8, 16, 0, tzinfo=ICT)
    with pytest.raises(ValueError):
        calendar_deadline(dt.datetime(2026, 11, 6, 16, 0), days=1)


def test_no_deadline_params_count_working_days():
    # working_days_per_month is the origination basis (21 repayment days a month), not a deadline: it stays.
    names = {f.name for f in dataclasses.fields(DirectTransferParams)} - {"working_days_per_month"}
    assert not [n for n in names if "working_days" in n], names
