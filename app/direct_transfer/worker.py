"""ARQ worker settings for the direct-transfer timers (HANDOFF-04 B13).

Run with: `arq app.direct_transfer.worker.DirectTransferWorkerSettings`

Kept separate from `app.scheduler.worker.WorkerSettings` on purpose: the scaffold's tests assert its exact job set and
a single cron entry. Merging the two is a one-line change plus a test update for Edward once the scaffold is retired.

Timers (ICT, `timezone=ICT` makes every cron field evaluate in Asia/Ho_Chi_Minh):
    hold expiry          every 5 minutes
    lapse / window close every 10 minutes (an SME dispute window is 2 working days, so minute precision is plenty)
    repayment day        00:10  presume unreported days paid, settle days past the report deadline
    invoices             00:20  overdue + late fee, investor accruals and invoices
    CIC daily balances   00:30  previous business day
"""
from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.timezone import ICT
from app.direct_transfer.jobs import (
    ALL_JOBS,
    advance_windows_job,
    cic_daily_balance_job,
    expire_holds_job,
    invoice_job,
    repayment_day_job,
)


class DirectTransferWorkerSettings:
    functions = ALL_JOBS
    cron_jobs = [
        cron(expire_holds_job, minute=set(range(0, 60, 5)), run_at_startup=False),
        cron(advance_windows_job, minute=set(range(0, 60, 10)), run_at_startup=False),
        cron(repayment_day_job, hour=0, minute=10, run_at_startup=False),
        cron(invoice_job, hour=0, minute=20, run_at_startup=False),
        cron(cic_daily_balance_job, hour=0, minute=30, run_at_startup=False),
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    timezone = ICT
    job_timeout = 300
    max_jobs = 5
