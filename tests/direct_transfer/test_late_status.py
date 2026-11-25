"""Late status for repayment days (docs/specs/repayment_schedule/late-status.md, AM-P8).

The `loan` fixture: 10 investors x 10M, repayment from Wed 4 Nov 2026 (each investor has a stub and an instalment that
day). A day with no payment proof by 18:00 ICT on its due date is LATE.
"""
import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select

from app.core.timezone import ICT
from app.direct_transfer import disputes, fees, repayment
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import Dispute, InvestorFeeAccrual, RepaymentDay
from app.ledger.models import LedgerEntry
from app.notifications.models import Notification

from .helpers import receipt_file


def ict(m, d, h=0, mi=0):
    return dt.datetime(2026, m, d, h, mi, tzinfo=ICT)


async def _days(db, **where):
    stmt = select(RepaymentDay)
    for k, v in where.items():
        stmt = stmt.where(getattr(RepaymentDay, k) == v)
    return (await db.execute(stmt.order_by(RepaymentDay.due_date, RepaymentDay.day_index))).scalars().all()


async def _pay(world, day_ids, *, now):
    return await repayment.mark_days_paid(
        world.db, sme_user_id=world.sme.id, repayment_day_ids=day_ids, receipt_file_id=(await receipt_file(world)).id,
        idempotency_key=f"pay-{uuid.uuid4().hex}", now=now, params=world.params,
    )


async def test_day_with_no_proof_becomes_late_at_18h_on_its_due_date(loan):
    world, _, tr = loan
    assert await repayment.mark_late_days(world.db, now=ict(11, 4, 17, 59), params=world.params) == 0
    assert await repayment.mark_late_days(world.db, now=ict(11, 4, 18), params=world.params) == 20  # stubs + day 1
    assert await repayment.mark_late_days(world.db, now=ict(11, 5, 0, 10), params=world.params) == 0  # idempotent
    wed = await _days(world.db, due_date=dt.date(2026, 11, 4))
    assert {d.status for d in wed} == {"LATE"} and {d.late_marked_at for d in wed} == {ict(11, 4, 18)}
    told = (await world.db.execute(select(Notification.user_id, Notification.event))).all()
    assert sum(1 for _, e in told if e == "REPAYMENT_DAY_LATE") == 20
    assert {u for u, e in told if e == "REPAYMENT_DAY_LATE"} == {t.investor_id for t in tr}
    assert [u for u, e in told if e == "REPAYMENT_DAY_OVERDUE_SME"] == [world.sme.id]  # once per run, not per day


async def test_late_day_is_never_presumed_paid_settled_or_posted_to_the_ledger(loan):
    world, _, _ = loan
    before = (await world.db.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()
    await repayment.mark_late_days(world.db, now=ict(11, 6), params=world.params)
    assert await repayment.settle_days(world.db, now=ict(12, 31)) == 0
    late = await _days(world.db, status="LATE")
    assert len(late) == 30 and all(d.settled_at is None and d.marked_paid_at is None for d in late)
    assert (await world.db.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == before


async def test_day_with_proof_and_investor_silence_settles_24_hours_after_the_proof(loan):
    world, _, _ = loan
    wed = [d.id for d in await _days(world.db, due_date=dt.date(2026, 11, 4))]
    paid = await _pay(world, wed, now=ict(11, 4, 10))
    assert {d.status for d in paid} == {"PRESUMED_PAID"} and {d.report_deadline for d in paid} == {ict(11, 5, 10)}
    assert await repayment.mark_late_days(world.db, now=ict(11, 4, 18), params=world.params) == 0  # proof in time
    assert await repayment.settle_days(world.db, now=ict(11, 5, 9, 59)) == 0
    assert await repayment.settle_days(world.db, now=ict(11, 5, 10)) == 20


async def test_late_day_cleared_when_sme_pays_with_receipt(loan):
    world, _, tr = loan
    await repayment.mark_late_days(world.db, now=ict(11, 4, 18), params=world.params)
    mine = [d.id for d in await _days(world.db, tranche_id=tr[0].id, due_date=dt.date(2026, 11, 4))]
    days = await _pay(world, mine, now=ict(11, 6, 9))
    assert {d.status for d in days} == {"PRESUMED_PAID"} and {d.late_cleared_at for d in days} == {ict(11, 6, 9)}
    assert {d.report_deadline for d in days} == {ict(11, 7, 9)}  # the investor's 24 hours start at the proof


async def test_marking_a_newer_day_while_an_older_day_is_unpaid_is_refused(loan):
    world, _, tr = loan
    thursday = (await _days(world.db, tranche_id=tr[0].id, due_date=dt.date(2026, 11, 5)))[0]
    with pytest.raises(DirectTransferError) as info:
        await _pay(world, [thursday.id], now=ict(11, 5, 10))
    assert info.value.code == "OLDER_DAYS_UNPAID" and info.value.status_code == 409
    older = [d.id for d in await _days(world.db, tranche_id=tr[0].id, due_date=dt.date(2026, 11, 4))]
    assert {d.status for d in await _pay(world, [*older, thursday.id], now=ict(11, 5, 10))} == {"PRESUMED_PAID"}


async def test_not_received_needs_the_smes_proof_first(loan):
    world, _, tr = loan
    mine = await _days(world.db, tranche_id=tr[0].id, due_date=dt.date(2026, 11, 4))
    day = mine[-1]

    async def report(now):
        return await repayment.report_missing_day(
            world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="Nothing arrived", file_id=None,
            now=now, params=world.params,
        )

    # Due today and not marked paid: still the SME's to pay, nothing to report. The day stays DUE, so it stays on the
    # SME's payment list and becomes LATE at 18:00.
    with pytest.raises(DirectTransferError) as due:
        await report(ict(11, 4, 10))
    assert (due.value.status_code, due.value.code) == (409, "NOT_MARKED_PAID") and day.status == "DUE"
    assert await repayment.mark_late_days(world.db, now=ict(11, 4, 18), params=world.params) > 0
    with pytest.raises(DirectTransferError) as late:
        await report(ict(11, 4, 19))
    assert late.value.code == "NOT_MARKED_PAID" and day.status == "LATE"
    assert (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).first() is None

    await _pay(world, [d.id for d in mine], now=ict(11, 4, 20))  # the SME's receipt
    assert (await report(ict(11, 4, 21))).status == "REPORTED_MISSED"


async def test_staff_not_paid_decision_makes_the_day_late(loan):
    world, _, tr = loan
    mine = await _days(world.db, tranche_id=tr[0].id, due_date=dt.date(2026, 11, 4))
    await _pay(world, [d.id for d in mine], now=ict(11, 4, 10))
    day = mine[-1]
    await repayment.report_missing_day(
        world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="Nothing arrived", file_id=None,
        now=ict(11, 4, 20), params=world.params,
    )
    dispute = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    await disputes.decide_dispute(
        world.db, dispute_id=dispute.id, staff_id=world.admin.id, outcome="NOT_PAID", reason="No bank proof",
        now=ict(11, 6, 9), params=world.params,
    )
    assert day.status == "LATE" and day.late_marked_at == ict(11, 6, 9) and day.settled_at is None


async def test_late_day_is_not_counted_in_investor_fee_accrual(loan):
    world, _, tr = loan
    # Every investor but the first is paid for November on each due date; the first is never paid, so its days are LATE.
    november = await _days(world.db)
    by_date: dict[dt.date, list] = {}
    for day in november:
        if day.tranche_id != tr[0].id and day.due_date < dt.date(2026, 12, 1):
            by_date.setdefault(day.due_date, []).append(day.id)
    for due_date, ids in by_date.items():
        await _pay(world, ids, now=dt.datetime.combine(due_date, dt.time(10), tzinfo=ICT))
    await repayment.mark_late_days(world.db, now=ict(12, 1), params=world.params)
    await repayment.settle_days(world.db, now=ict(12, 3))
    assert await fees.accrue_investor_fees(world.db, now=ict(12, 3)) == 9  # the month is not held up by late days
    accrued = {a.tranche_id for a in (await world.db.execute(select(InvestorFeeAccrual))).scalars()}
    assert tr[0].id not in accrued and len(accrued) == 9
