"""Jobs Google Cloud Scheduler runs on prod-api (no worker is deployed).

Each is a POST that only Cloud Scheduler's service account can call (see
auth.py), safe to call twice: every job records what it sent and skips it the
next time. Kept out of the OpenAPI schema; the web app never calls them.
Spec: docs/specs/scheduled_jobs/cloud-scheduler-jobs.md.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.fee_notice.service import send_pending_fee_invoice_emails
from app.core.database import get_db
from app.scheduled_jobs import reminders, zalo_reminders
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


@router.post("/fee-invoice-emails", response_model=JobResult)
async def run_fee_invoice_emails(db: AsyncSession = Depends(get_db)):
    """Daily: email any SME activation-fee invoice not emailed yet (one whose
    send after Advance failed, or a round closed some other way)."""
    sent = await send_pending_fee_invoice_emails(db)
    return JobResult(job="fee-invoice-emails", sent=sent)
