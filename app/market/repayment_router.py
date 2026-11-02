"""Daily repayment (HANDOFF-04 Epic 2, tickets 2.1 and 2.2): the business pays and marks days paid; the investor
sees their schedule and reports a day that never arrived.

Edward's services hold the rules (app/direct_transfer/repayment.py); this router adds auth, auditing and the HTTP
error mapping. The business pays each investor directly every working day, one VietQR per investor; FundLok never
holds the money (ADR-009). Spec: docs/specs/direct_transfer/repayment-collection.md section 4.
"""
import datetime as dt
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.repayment import mark_days_paid, report_missing_day, todays_payments, tranche_schedule
from app.direct_transfer.schemas import RepaymentRowOut, TodaysPaymentsOut
from app.market.schemas import MarkDaysPaidCreate, ReportMissingCreate
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(prefix="/repayment", tags=["repayment"])

require_sme = require_roles(Role.SME)
require_investor = require_roles(Role.INVESTOR)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _raise_mapped(exc: DirectTransferError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code}) from exc


@router.get("/today", response_model=TodaysPaymentsOut)
async def read_todays_payments(
    on_date: dt.date | None = Query(default=None, alias="date"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """What the business owes each investor today, and any earlier day not yet settled: one row per investor and
    day, each with the investor's account and a VietQR for the exact amount. `date` defaults to today (ICT)."""
    return TodaysPaymentsOut.model_validate(
        await todays_payments(db, sme_user_id=current_user.id, on_date=on_date or ict_date(_now()))
    )


@router.post("/days/mark-paid", response_model=list[RepaymentRowOut])
async def mark_paid(
    body: MarkDaysPaidCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """The business says it paid these days (due today or earlier): DUE -> PRESUMED_PAID. Each investor then has
    two working days to report a day that did not arrive. Returns the rows as they now stand."""
    now = _now()
    try:
        days = await mark_days_paid(
            db,
            sme_user_id=current_user.id,
            repayment_day_ids=body.repayment_day_ids,
            receipt_file_id=body.receipt_file_id,
            idempotency_key=idempotency_key or "",
            now=now,
        )
    except DirectTransferError as exc:
        _raise_mapped(exc)
    append_audit(
        db,
        entity_type="REPAYMENT_DAY",
        entity_id=None,
        action="REPAYMENT_DAYS_MARKED_PAID",
        actor_id=current_user.id,
        after_state={
            "repayment_day_ids": [str(day.id) for day in days],
            "net_vnd": sum(day.net_vnd for day in days),
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    marked = {day.id for day in days}
    today = await todays_payments(db, sme_user_id=current_user.id, on_date=ict_date(now))
    return [RepaymentRowOut.model_validate(row) for row in today["rows"] if row["repayment_day_id"] in marked]


@router.get("/tranches/{tranche_id}/schedule", response_model=list[RepaymentRowOut])
async def read_schedule(
    tranche_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every repayment day of one ticket, for its investor or the business that owes it."""
    try:
        rows = await tranche_schedule(db, tranche_id=tranche_id, user_id=current_user.id)
    except DirectTransferError as exc:
        _raise_mapped(exc)
    return [RepaymentRowOut.model_validate(row) for row in rows]


@router.post("/days/{day_id}/report-missing", response_model=RepaymentRowOut)
async def report_missing(
    day_id: UUID,
    body: ReportMissingCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The investor says a due day never reached their account. Before the report deadline the day stops counting
    as paid and the business has to prove it paid (MISSED_DAY); a few working days late it still goes to staff
    (LATE_REPORT) without changing the day; after that it is refused (REPORT_WINDOW_CLOSED)."""
    statement = (body.statement or "").strip() or None
    try:
        day = await report_missing_day(
            db, day_id=day_id, investor_id=current_user.id, statement=statement, file_id=None, now=_now()
        )
    except DirectTransferError as exc:
        # A refused late report is itself recorded (evidence log); keep that row.
        if exc.persist:
            await db.commit()
        _raise_mapped(exc)
    append_audit(
        db,
        entity_type="REPAYMENT_DAY",
        entity_id=day.id,
        action="REPAYMENT_DAY_REPORTED_MISSING",
        actor_id=current_user.id,
        after_state={"status": day.status, "due_date": day.due_date.isoformat(), "statement": statement},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    rows = await tranche_schedule(db, tranche_id=day.tranche_id, user_id=current_user.id)
    return RepaymentRowOut.model_validate(next(row for row in rows if row["repayment_day_id"] == day.id))
