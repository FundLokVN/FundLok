"""The money timers run through Cloud Scheduler (HANDOFF-07 E1, docs/specs/scheduled_jobs/cloud-scheduler-jobs.md).

`app.scheduled_jobs.money_timers` is what `/internal/jobs/repayment-day` and `/internal/jobs/invoices` call, with the
clock injected. The `loan` fixture: 10 investors x 10M, repayment from Wed 4 Nov 2026.
"""
import ast
import datetime as dt
import pathlib
import uuid

from sqlalchemy import func, select

import app.direct_transfer
from app.core.timezone import ICT
from app.direct_transfer import repayment
from app.direct_transfer.models import Dispute, FeeInvoice, RepaymentDay
from app.repayment_schedule.models import Facility
from app.scheduled_jobs import money_timers

from .helpers import at, close_now, fund_window, make_world, pay_days_through, receipt_file


def ict(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ICT)


async def _wednesday(db):
    return (await db.execute(select(RepaymentDay).where(RepaymentDay.due_date == dt.date(2026, 11, 4)))).scalars().all()


async def test_repayment_day_marks_an_unpaid_day_late_after_18h_and_not_before(loan):
    world, _, _ = loan
    before = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 17, 59))
    assert before == {"late": 0, "settled": 0, "disputes": 0, "failed": 0}
    after = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 18, 10))
    assert after == {"late": 20, "settled": 0, "disputes": 0, "failed": 0}  # each investor's stub and instalment
    assert {d.status for d in await _wednesday(world.db)} == {"LATE"}


async def test_repayment_day_marks_every_missed_day_late_in_one_catch_up_run(loan):
    world, _, _ = loan
    out = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 9, 0, 10))
    assert out == {"late": 40, "settled": 0, "disputes": 0, "failed": 0}  # Wed (10 instalments + 10 stubs), Thu and Fri
    assert await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 9, 0, 10)) == {"late": 0, "settled": 0, "disputes": 0, "failed": 0}


async def test_repayment_day_settles_a_paid_day_after_the_investor_s_24_hours(loan):
    world, _, _ = loan
    ids = [d.id for d in await _wednesday(world.db)]
    await repayment.mark_days_paid(
        world.db, sme_user_id=world.sme.id, repayment_day_ids=ids, receipt_file_id=(await receipt_file(world)).id,
        idempotency_key=f"pay-{uuid.uuid4().hex}", now=ict(2026, 11, 4, 10), params=world.params,
    )
    early = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 5, 9, 59))
    assert early == {"late": 0, "settled": 0, "disputes": 0, "failed": 0}
    settled = await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 5, 10, 10))
    assert settled == {"late": 0, "settled": 20, "disputes": 0, "failed": 0}


async def test_repayment_day_called_twice_changes_nothing_the_second_time(loan):
    world, _, _ = loan
    assert (await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 18, 10)))["late"] == 20
    assert await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 4, 18, 10)) == {"late": 0, "settled": 0, "disputes": 0, "failed": 0}
    assert await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 5, 0, 10)) == {"late": 0, "settled": 0, "disputes": 0, "failed": 0}


async def test_repayment_day_commits_healthy_rounds_when_another_settlement_fails(db_session, monkeypatch):
    now = ict(2027, 6, 1)
    broken = await make_world(db_session, target=10_000_000, term=1)
    await fund_window(broken, [10_000_000])
    broken_window = await close_now(broken, now=at(days=1))
    await pay_days_through(broken, now)
    broken_facility = await db_session.get(Facility, broken_window.facility_id)
    broken_facility.collected_to_date_vnd = 1
    broken_day_id = (await db_session.execute(
        select(RepaymentDay.id).where(RepaymentDay.facility_id == broken_facility.id).limit(1)
    )).scalar_one()

    healthy = await make_world(db_session, target=10_000_000, term=1)
    await fund_window(healthy, [10_000_000])
    healthy_window = await close_now(healthy, now=at(days=1))
    days = (await db_session.execute(
        select(RepaymentDay).where(RepaymentDay.facility_id == healthy_window.facility_id)
        .order_by(RepaymentDay.due_date)
    )).scalars().all()
    paid_ids = [day.id for day in days if day.due_date == days[0].due_date]
    unpaid_id = days[-1].id
    await repayment.mark_days_paid(
        db_session, sme_user_id=healthy.sme.id, repayment_day_ids=paid_ids,
        receipt_file_id=(await receipt_file(healthy)).id, idempotency_key="healthy", now=ict(2026, 11, 4, 10),
    )
    await db_session.commit()

    broken_window_id = broken_window.id
    due_windows = repayment.due_settlement_windows

    async def broken_first(db, *, now):
        ids = await due_windows(db, now=now)
        return sorted(ids, key=lambda window_id: window_id != broken_window_id)

    monkeypatch.setattr(repayment, "due_settlement_windows", broken_first)
    result = await money_timers.run_repayment_day(db_session, now=now)
    assert result["failed"] == 1 and result["settled"] == len(paid_ids)
    for day_id in paid_ids:
        assert (await db_session.get(RepaymentDay, day_id, populate_existing=True)).settled_at == now
    assert (await db_session.get(RepaymentDay, unpaid_id, populate_existing=True)).status == "LATE"
    assert (await db_session.get(RepaymentDay, broken_day_id, populate_existing=True)).settled_at is None
    retry = await money_timers.run_repayment_day(db_session, now=now)
    assert retry["failed"] == 1 and retry["settled"] == 0


async def test_repayment_day_hands_an_expired_dispute_turn_to_staff(loan):
    """disputes.md: a required party that has not answered by its deadline hands the turn to staff, never a decision.
    No worker runs in production, so repayment-day is what moves it."""
    world, _, tr = loan
    day = (await world.db.execute(select(RepaymentDay).where(
        RepaymentDay.tranche_id == tr[0].id, RepaymentDay.day_index == 1, RepaymentDay.kind == "INSTALMENT",
    ))).scalar_one()
    same_day_ids = (await world.db.execute(select(RepaymentDay.id).where(
        RepaymentDay.tranche_id == day.tranche_id, RepaymentDay.due_date == day.due_date,
    ))).scalars().all()
    await repayment.mark_days_paid(
        world.db, sme_user_id=world.sme.id, repayment_day_ids=same_day_ids,
        receipt_file_id=(await receipt_file(world)).id,
        idempotency_key="dispute-repayment-proof", now=ict(2026, 11, 5, 9), params=world.params,
    )
    await repayment.report_missing_day(
        world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="not received", file_id=None,
        now=ict(2026, 11, 5, 10), params=world.params,
    )
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    deadline = d.response_deadline
    assert d.required_next_party == "SME" and deadline == ict(2026, 11, 7, 10)

    before = await money_timers.run_repayment_day(world.db, now=deadline - dt.timedelta(seconds=1))
    assert before["disputes"] == 0 and d.required_next_party == "SME"
    after = await money_timers.run_repayment_day(world.db, now=deadline)
    assert after["disputes"] == 1
    assert (d.required_next_party, d.response_deadline, d.status, d.outcome) == ("STAFF", None, "OPEN", None)
    assert (await money_timers.run_repayment_day(world.db, now=deadline))["disputes"] == 0


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


async def test_invoices_marks_overdue_accrues_and_invoices_in_one_run(loan):
    world, _, _ = loan
    await pay_days_through(world, ict(2026, 12, 3))  # November paid with proof, so it settles and accrues
    await money_timers.run_repayment_day(world.db, now=ict(2026, 12, 3))
    out = await money_timers.run_invoices(world.db, now=ict(2026, 12, 3))
    assert out == {"overdue": 1, "accrued": 10, "invoiced": 10}  # the SME invoice (issued 3 Nov, due 10 Nov) is overdue
    assert await money_timers.run_invoices(world.db, now=ict(2026, 12, 3)) == {"overdue": 0, "accrued": 0, "invoiced": 0}
    assert (await world.db.execute(select(func.count()).select_from(FeeInvoice))).scalar_one() == 11


async def test_invoices_marks_an_unpaid_invoice_overdue(loan):
    world, _, _ = loan
    inv = (await world.db.execute(select(FeeInvoice).where(FeeInvoice.kind == "SME_ACTIVATION"))).scalar_one()
    assert (await money_timers.run_invoices(world.db, now=inv.due_at - dt.timedelta(seconds=1)))["overdue"] == 0
    assert (await money_timers.run_invoices(world.db, now=inv.due_at + dt.timedelta(days=1)))["overdue"] == 1
    assert inv.status == "OVERDUE"


async def test_cic_daily_balance_logs_the_previous_business_day_once(loan):
    world, _, _ = loan
    await money_timers.run_repayment_day(world.db, now=ict(2026, 11, 6, 0, 10))
    assert await money_timers.run_cic_daily_balance(world.db, now=ict(2026, 11, 6, 0, 30)) == {"events": 10}  # Thu 5 Nov
    assert await money_timers.run_cic_daily_balance(world.db, now=ict(2026, 11, 6, 0, 30)) == {"events": 0}
    assert await money_timers.run_cic_daily_balance(world.db, now=ict(2026, 11, 8, 0, 30)) == {"events": 0}  # Sat


def test_money_timers_never_use_background_tasks_or_sleep():
    """CLAUDE.md: never BackgroundTasks for financial operations."""
    tree = ast.parse(pathlib.Path(money_timers.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert not any("BackgroundTasks" in a.name for a in node.names)
        if isinstance(node, ast.Attribute):
            assert node.attr != "sleep"


def test_no_direct_transfer_module_reads_the_wall_clock():
    """Time is injected everywhere: the only wall-clock read is the default in app/scheduled_jobs/money_timers.py."""
    root = pathlib.Path(app.direct_transfer.__file__).parent
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today") and isinstance(node.value, ast.Attribute):
                assert node.value.attr != "datetime", (path.name, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("now", "utcnow", "today"):
                base = node.func.value
                assert not (isinstance(base, ast.Name) and base.id in ("datetime", "date")), (path.name, node.lineno)
