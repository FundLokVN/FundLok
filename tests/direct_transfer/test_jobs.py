"""Spec: HANDOFF-04 B13 (ARQ jobs wrap idempotent, clock-injected service functions) and funding-window.md section 5 timers."""
import ast
import datetime as dt
import inspect
import pathlib

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import engine
from app.core.timezone import ICT
from app.direct_transfer import jobs, worker
from app.direct_transfer.models import CicEvent, FeeInvoice, FundingWindow, InvestorFeeAccrual, RepaymentDay, Tranche

from .helpers import T0, accept, at, attested_tranche, fund_window, make_investor, open_window, reserve

M = 1_000_000
Session = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def ctx_at(moment):
    return {"session_factory": Session, "clock": lambda: moment}


def ict(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ICT)


async def test_expire_holds_job_uses_injected_clock_and_session(world, db_session):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    await db_session.commit()
    assert await jobs.expire_holds_job(ctx_at(at(hours=1))) == 0
    assert await jobs.expire_holds_job(ctx_at(at(hours=2))) == 1
    assert await jobs.expire_holds_job(ctx_at(at(hours=3))) == 0
    async with Session() as s:
        assert (await s.get(Tranche, t.id)).status == "EXPIRED"


async def test_advance_windows_job_lapses_confirms_and_closes(world, db_session):
    await fund_window(world, [60 * M])
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M, now=at(days=14, hours=-1))
    await db_session.commit()
    n = await jobs.advance_windows_job(ctx_at(at(days=14)))
    assert n == 1
    async with Session() as s:
        assert (await s.get(FundingWindow, world.window.id)).status == "CLOSING"
    await jobs.advance_windows_job(ctx_at(ict(2026, 11, 19)))  # SME dispute window lapsed -> confirmed; close finalizes
    async with Session() as s:
        window = await s.get(FundingWindow, world.window.id)
        assert window.status == "REPAYING" and window.confirmed_total_vnd == 70 * M
        assert (await s.get(Tranche, t.id)).status == "ACTIVE"
    assert await jobs.advance_windows_job(ctx_at(ict(2026, 11, 19, 1))) == 0  # nothing live any more


async def test_repayment_day_job_presumes_and_settles(loan, db_session):
    world, window, _ = loan
    await db_session.commit()
    out = await jobs.repayment_day_job(ctx_at(ict(2026, 11, 9, 0, 10)))
    assert out == {"presumed": 40, "settled": 40}  # 4 working days of rows are past; Wed, Thu and Fri passed their 24-hour report deadline
    again = await jobs.repayment_day_job(ctx_at(ict(2026, 11, 9, 0, 10)))
    assert again == {"presumed": 0, "settled": 0}


async def test_invoice_job_marks_overdue_accrues_and_invoices(loan, db_session):
    world, _, _ = loan
    await jobs.repayment_day_job(ctx_at(ict(2026, 12, 3)))
    await db_session.commit()
    await jobs.repayment_day_job(ctx_at(ict(2026, 12, 3)))
    out = await jobs.invoice_job(ctx_at(ict(2026, 12, 3)))
    assert out == {"overdue": 1, "accrued": 10, "invoiced": 10}  # the SME invoice (issued 3 Nov, due 10 Nov) is overdue
    assert await jobs.invoice_job(ctx_at(ict(2026, 12, 3))) == {"overdue": 0, "accrued": 0, "invoiced": 0}
    async with Session() as s:
        assert (await s.execute(select(func.count()).select_from(FeeInvoice))).scalar_one() == 11


async def test_cic_daily_balance_job_logs_previous_business_day_only(loan, db_session):
    world, _, _ = loan
    await db_session.commit()
    await jobs.repayment_day_job(ctx_at(ict(2026, 11, 6, 0, 10)))
    assert await jobs.cic_daily_balance_job(ctx_at(ict(2026, 11, 6, 0, 30))) == 10  # Thursday 5 Nov
    assert await jobs.cic_daily_balance_job(ctx_at(ict(2026, 11, 6, 0, 30))) == 0
    assert await jobs.cic_daily_balance_job(ctx_at(ict(2026, 11, 8, 0, 30))) == 0  # yesterday was Saturday
    async with Session() as s:
        n = (await s.execute(select(func.count()).select_from(CicEvent).where(CicEvent.event_type == "DAILY_BALANCE"))).scalar_one()
        assert n == 10


def test_worker_registers_every_timer_in_ict_without_background_tasks():
    names = {fn.__name__ for fn in worker.DirectTransferWorkerSettings.functions}
    assert names == {"expire_holds_job", "advance_windows_job", "repayment_day_job", "invoice_job", "cic_daily_balance_job"}
    assert worker.DirectTransferWorkerSettings.timezone == ICT
    assert len(worker.DirectTransferWorkerSettings.cron_jobs) == 5
    assert all(inspect.iscoroutinefunction(fn) for fn in worker.DirectTransferWorkerSettings.functions)
    for mod in (jobs, worker):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert not any("BackgroundTasks" in a.name for a in node.names)
            if isinstance(node, ast.Attribute):
                assert node.attr != "sleep"


def test_no_service_module_reads_the_wall_clock():
    """Time is injected everywhere: the only wall-clock read is the job default in jobs._now."""
    root = pathlib.Path(jobs.__file__).parent
    for path in root.glob("*.py"):
        if path.name in ("jobs.py",):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today") and isinstance(node.value, ast.Attribute):
                assert node.value.attr != "datetime", (path.name, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("now", "utcnow", "today"):
                base = node.func.value
                assert not (isinstance(base, ast.Name) and base.id in ("datetime", "date")), (path.name, node.lineno)
