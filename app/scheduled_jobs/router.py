"""Jobs Google Cloud Scheduler runs on prod-api (no worker is deployed).

Each is a POST that only Cloud Scheduler's service account can call (see
auth.py), safe to call twice: every job records what it sent and skips it the
next time. Kept out of the OpenAPI schema; the web app never calls them.
Spec: docs/specs/scheduled_jobs/cloud-scheduler-jobs.md.
"""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.fee_notice.service import send_pending_fee_invoice_emails
from app.core.database import get_db
from app.scheduled_jobs import day14, reminders, zalo_reminders
from app.scheduled_jobs.auth import require_scheduler

router = APIRouter(
    prefix="/internal/jobs",
    tags=["internal-jobs"],
    include_in_schema=False,
    dependencies=[Depends(require_scheduler)],
)


class ChannelResult(BaseModel):
    sent: int
    failed: int = 0
    # Why nothing ran (NOT_CONFIGURED, NOT_A_WORKING_DAY), when nothing did.
    skipped: str | None = None


class JobResult(BaseModel):
    job: str
    sent: int
    failed: int = 0
    # Why nothing ran (NOT_A_WORKING_DAY), when nothing did.
    skipped: str | None = None
    # repayment-reminders only: the Zalo messages the same run sent.
    zalo: ChannelResult | None = None


@router.post("/repayment-reminders", response_model=JobResult)
async def run_repayment_reminders(db: AsyncSession = Depends(get_db)):
    """Daily, 15:00 ICT: email each SME what is due today and what earlier
    days are not yet marked paid, then send it a Zalo message for each
    investor ticket due today it has not marked paid (when the template is
    configured). Nothing on a non-working day. `sent`/`failed` are the emails."""
    result = await reminders.send_repayment_reminders(db)
    zalo = await zalo_reminders.send_zalo_reminders(db)
    return JobResult(job="repayment-reminders", **result, zalo=ChannelResult(**zalo))


class Day14CutoffResult(BaseModel):
    job: str
    locked: int  # lenders newly locked by this run
    pledges: int  # late-money top-up pledges recorded by this run
    released: int = 0  # lenders unlocked: their receipt proved them in time
    failed: int = 0  # rounds that failed this run (rolled back, retried by the next run)


class Day14ReminderIn(BaseModel):
    kind: Literal["DAY14_MORNING", "DAY14_AFTERNOON"]


class Day14ReminderResult(BaseModel):
    job: str
    kind: str
    in_app: ChannelResult
    email: ChannelResult
    zalo: ChannelResult


@router.post("/day14-cutoff", response_model=Day14CutoffResult)
async def run_day14_cutoff(db: AsyncSession = Depends(get_db)):
    """Daily, 15:00 and 15:35 ICT: on rounds paying out today, lock lenders who sent nothing and those whose money
    came late, record the late money as their top-up, and tell them in the app. Idempotent per ticket
    (docs/specs/direct_transfer/day14-transfer-rule.md rule 9)."""
    result = await day14.run_day14_cutoff(db)
    return Day14CutoffResult(job="day14-cutoff", **result)


@router.post("/day14-transfer-reminders", response_model=Day14ReminderResult)
async def run_day14_transfer_reminders(body: Day14ReminderIn, db: AsyncSession = Depends(get_db)):
    """Daily, 08:00 (DAY14_MORNING) and 14:00 (DAY14_AFTERNOON) ICT: "sign and pay by 14:45" to every lender whose
    ticket on a round paying out today is unpaid, in the app, by email and on Zalo (rule 10)."""
    counts = await day14.send_day14_reminders(db, kind=body.kind)
    return Day14ReminderResult(
        job="day14-transfer-reminders", kind=body.kind,
        **{channel: ChannelResult(**value) for channel, value in counts.items()},
    )


@router.post("/fee-invoice-emails", response_model=JobResult)
async def run_fee_invoice_emails(db: AsyncSession = Depends(get_db)):
    """Daily: email any SME activation-fee invoice not emailed yet (one whose
    send after Advance failed, or a round closed some other way)."""
    sent = await send_pending_fee_invoice_emails(db)
    return JobResult(job="fee-invoice-emails", sent=sent)
