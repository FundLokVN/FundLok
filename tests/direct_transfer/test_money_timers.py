"""The ARQ money timers run through Cloud Scheduler (HANDOFF-07 E1, docs/specs/scheduled_jobs/cloud-scheduler-jobs.md).

`app.scheduled_jobs.money_timers` is what `/internal/jobs/repayment-day` and `/internal/jobs/invoices` call, with the
clock injected. The `loan` fixture: 10 investors x 10M, repayment from Wed 4 Nov 2026.
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.core.timezone import ICT
from app.direct_transfer import repayment
from app.direct_transfer.models import FeeInvoice, RepaymentDay
from app.scheduled_jobs import money_timers

from .helpers import pay_days_through, receipt_file


def ict(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ICT)


async def _wednesday(db):
    return (await db.execute(select(RepaymentDay).where(RepaymentDay.due_date == dt.date(2026, 11, 4)))).scalars().all()


async def test_repayment_day_marks_an_unpaid_day_late_after_18h_and_not_before(loan):
    world, _, _ = loan
    before = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 17, 59))
    assert before == {"late": 0, "settled": 0}
    after = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 18, 10))
    assert after == {"late": 20, "settled": 0}  # each investor's stub and instalment
    assert {d.status for d in await _wednesday(world.db)} == {"LATE"}


async def test_repayment_day_settles_a_paid_day_after_the_investor_s_24_hours(loan):
    world, _, _ = loan
    ids = [d.id for d in await _wednesday(world.db)]
    await repayment.mark_days_paid(
        world.db, sme_user_id=world.sme.id, repayment_day_ids=ids, receipt_file_id=(await receipt_file(world)).id,
        idempotency_key=f"pay-{uuid.uuid4().hex}", now=ict(2026, 11, 4, 10), params=world.params,
    )
    early = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 5, 9, 59))
    assert early == {"late": 0, "settled": 0}
    settled = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 5, 10, 10))
    assert settled == {"late": 0, "settled": 20}


async def test_repayment_day_called_twice_changes_nothing_the_second_time(loan):
    world, _, _ = loan
    assert (await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 18, 10)))["late"] == 20
    assert await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 18, 10)) == {"late": 0, "settled": 0}
    assert await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 5, 0, 10)) == {"late": 0, "settled": 0}


async def test_invoices_issues_an_investor_invoice_once(loan):
    world, _, tr = loan
    now = ict(2026, 12, 3)
    await pay_days_through(world, now)
    await repayment.settle_days(world.db, now=now)
    first = await money_timers.run_invoices(world.db, now=now)
    assert first["accrued"] == 10 and first["invoiced"] == 10
    issued = (await world.db.execute(select(FeeInvoice).where(FeeInvoice.kind == "INVESTOR_SERVICE"))).scalars().all()
    assert {i.payer_user_id for i in issued} == {t.investor_id for t in tr}
    again = await money_timers.run_invoices(world.db, now=now)
    assert again["accrued"] == 0 and again["invoiced"] == 0


async def test_invoices_marks_an_unpaid_invoice_overdue(loan):
    world, _, _ = loan
    inv = (await world.db.execute(select(FeeInvoice).where(FeeInvoice.kind == "SME_ACTIVATION"))).scalar_one()
    assert (await money_timers.run_invoices(world.db, now=inv.due_at - dt.timedelta(seconds=1)))["overdue"] == 0
    assert (await money_timers.run_invoices(world.db, now=inv.due_at + dt.timedelta(days=1)))["overdue"] == 1
    assert inv.status == "OVERDUE"
