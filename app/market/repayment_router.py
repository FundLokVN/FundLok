"""Daily repayment (HANDOFF-04 Epic 2, tickets 2.1 and 2.2): the business pays and marks days paid; the investor
sees their schedule, confirms a repayment that arrived (its receipt) and reports a day that never arrived.

Edward's services hold the rules (app/direct_transfer/repayment.py); this router adds auth, auditing and the HTTP
error mapping. The business pays each investor directly every working day, one VietQR per investor; FundLok never
holds the money (ADR-009). Spec: docs/specs/direct_transfer/repayment-collection.md section 4.
"""
import datetime as dt
from typing import Literal
from uuid import UUID

from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.config import DEFAULT_PARAMS
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.evidence import store_evidence_file
from app.direct_transfer.models import FundingWindow, PaymentAccount, RepaymentDay, Tranche, Transfer
from app.direct_transfer.repayment import mark_days_paid, report_missing_day, todays_payments, tranche_schedule
from app.direct_transfer.schemas import RepaymentRowOut, TodaysPaymentsOut
from app.direct_transfer.tranches import mask_name
from app.direct_transfer.vietqr import build_vietqr_payload
from app.lending.models import AuditLog
from app.market import funding, repayment_receipts
from app.market.projection import project_ticket
from app.market.repayment_receipt_check import (
    RECEIPT_MISMATCH,
    RepaymentCheck,
    check_repayment_receipt,
    record_kept,
    record_refused,
)
from app.market.schemas import (
    InvestmentDueOut,
    MarkDaysPaidCreate,
    ProjectedScheduleOut,
    RepaymentReceiptOut,
    RepaymentRejectionOut,
    ReportMissingCreate,
)
from app.notifications import realtime
from app.uploads.receipt_ocr import ReceiptOcr, receipt_ocr
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(prefix="/repayment", tags=["repayment"])

# D-047: a suspended account may still repay and confirm receipts (only new offers and investments stop).
require_sme = require_roles(Role.SME, allow_suspended=True)
require_investor = require_roles(Role.INVESTOR, allow_suspended=True)


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
    24 hours, from now, to report a day that did not arrive. A receipt (`receipt_file_id`) is required: without proof
    a day becomes LATE at 18:00 on its due date. Returns the rows as they now stand."""
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


async def _business_tranche(db: AsyncSession, tranche_id: UUID, sme_user_id: UUID) -> Tranche:
    """One investment in the business's own round; 404 for anyone else's (its investor included)."""
    tranche = await db.get(Tranche, tranche_id)
    window = await db.get(FundingWindow, tranche.window_id) if tranche else None
    if window is None or window.sme_user_id != sme_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return tranche


@router.get("/tranches/{tranche_id}/due", response_model=InvestmentDueOut)
async def read_investment_due(
    tranche_id: UUID,
    on_date: dt.date | None = Query(default=None, alias="date"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """What the business owes this investor now: the days due today or earlier still DUE, added up into one
    transfer with one VietQR, to the investor's account and under the investment's memo. A day already marked
    paid, or reported missing, is not asked for again. `date` defaults to today (ICT)."""
    tranche = await _business_tranche(db, tranche_id, current_user.id)
    today = on_date or ict_date(_now())
    rows = await tranche_schedule(db, tranche_id=tranche_id, user_id=current_user.id)
    due = [row for row in rows if row["status"] in ("DUE", "LATE") and row["due_date"] <= today]
    net = sum(row["net_vnd"] for row in due)
    account = await db.get(PaymentAccount, tranche.investor_account_id) if tranche.investor_account_id else None
    qr = None
    if account is not None and account.bank_bin and net > 0:
        qr = build_vietqr_payload(
            bank_bin=account.bank_bin, account_number=account.account_number, amount_vnd=net,
            memo=tranche.reference_code,
        )
    investor = await db.get(User, tranche.investor_id)
    # A day due again because staff denied its receipt (before investors confirmed repayments): say why, from the
    # decision's audit entry.
    denied = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "TRANCHE",
                AuditLog.entity_id == tranche.id,
                AuditLog.action == "REPAYMENT_RECEIPT_DENIED",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    due_ids = {str(row["repayment_day_id"]) for row in due}
    rejection = None
    if denied is not None and due_ids & set((denied.after_state or {}).get("repayment_day_ids") or []):
        rejection = RepaymentRejectionOut(reason=denied.after_state.get("reason") or "", decided_at=denied.created_at)
    return InvestmentDueOut(
        date=today,
        tranche_id=tranche.id,
        reference_code=tranche.reference_code,
        investor_display=mask_name(investor.full_name or investor.email),
        days=[RepaymentRowOut.model_validate(row) for row in due],
        gross_vnd=sum(row["gross_vnd"] for row in due),
        tax_withheld_vnd=sum(row["tax_withheld_vnd"] for row in due),
        net_vnd=net,
        payee=rows[0]["payee"] if rows else None,
        vietqr_payload=qr,
        rejection=rejection,
    )


@router.post("/tranches/{tranche_id}/pay", response_model=list[RepaymentRowOut])
async def pay_with_receipt(
    tranche_id: UUID,
    request: Request,
    repayment_day_ids: list[UUID] = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ocr: ReceiptOcr | None = Depends(receipt_ocr),
):
    """The business paid this investor and uploads its bank receipt (PDF, PNG, JPEG or WEBP, at most 10 MB). In one
    step the receipt is kept as evidence (a REPAYMENT transfer for each day, for the investor to confirm) and the
    days are marked paid, so a request that fails part way leaves nothing half done. The investor's bell rings. The same receipt cannot be used twice
    (409 DUPLICATE_FILE); replaying the same Idempotency-Key stores and marks nothing twice.

    With RECEIPT_OCR_ENABLED the receipt is read on our own server and compared with this repayment (receipt-ocr.md
    section 10). A confident read that does not match (amount, memo, the investor's account, bank or name, the
    business as payer, the time, a bank transaction already used) is refused when RECEIPT_AUTO_VERDICT_ENABLED is on:
    422 RECEIPT_MISMATCH with ``details.failure_codes``, nothing stored or marked paid. Anything else goes through as
    before, and the investor sees the system's check while confirming; a match never confirms on its own."""
    if not idempotency_key:
        raise HTTPException(
            status_code=400, detail="Idempotency-Key is required", headers={"X-Error-Code": "IDEMPOTENCY_KEY_REQUIRED"}
        )
    tranche = await _business_tranche(db, tranche_id, current_user.id)
    day_ids = list(dict.fromkeys(repayment_day_ids))
    days = (await db.execute(select(RepaymentDay).where(RepaymentDay.id.in_(day_ids)))).scalars().all()
    if len(days) != len(day_ids) or any(day.tranche_id != tranche_id for day in days):
        raise HTTPException(status_code=404, detail="Repayment day not found")

    # A replay: the receipt was stored and the days marked under this key already.
    receipt_id = (
        await db.execute(
            select(Transfer.evidence_file_id).where(
                Transfer.idempotency_key.in_([f"{idempotency_key}:{day_id}" for day_id in day_ids])
            )
        )
    ).scalars().first()
    replay = receipt_id is not None
    now = _now()
    check: RepaymentCheck | None = None
    if not replay:
        limit = DEFAULT_PARAMS.max_evidence_bytes
        content = await file.read(limit + 1)
        if len(content) > limit:
            raise HTTPException(status_code=413, detail="File too large", headers={"X-Error-Code": "FILE_TOO_LARGE"})
        content_type = funding.sniff_receipt_type(content)
        if content_type is None:
            raise HTTPException(
                status_code=415, detail="Upload a PDF, PNG, JPEG or WEBP", headers={"X-Error-Code": "UNSUPPORTED_TYPE"}
            )
        # OCR (receipt-ocr.md section 10) runs before mark_days_paid locks the days, so a slow read holds nothing.
        # Not on a replay: that receipt was read the first time.
        if ocr is not None:
            reading = await ocr.reader.read(content=content, content_type=content_type)
            window = await db.get(FundingWindow, tranche.window_id)
            check = await check_repayment_receipt(
                db, tranche=tranche, window=window, days=days, reading=reading, auto_verdict=ocr.auto_verdict, now=now
            )
            if check.refused:
                # Clearly not this repayment: nothing is stored or marked paid, the days stay due, the attempt is
                # recorded for staff, and the business is told what did not match.
                await record_refused(db, check, content=content, tranche=tranche, days=days, now=now)
                await db.commit()
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "This receipt does not match the repayment ("
                        + ", ".join(check.failure_codes)
                        + "). Upload the receipt of the transfer made with this repayment's QR.",
                        "details": {"failure_codes": check.failure_codes},
                    },
                    headers={"X-Error-Code": RECEIPT_MISMATCH},
                )
    try:
        if not replay:
            stored = await store_evidence_file(
                db,
                funding.evidence_storage(),
                owner_user_id=current_user.id,
                purpose="RECEIPT",
                content=content,
                content_type=content_type,
                filename=(file.filename or "")[:255] or None,
            )
            receipt_id = stored.id
        marked = await mark_days_paid(
            db,
            sme_user_id=current_user.id,
            repayment_day_ids=day_ids,
            receipt_file_id=receipt_id,
            idempotency_key=idempotency_key,
            now=now,
        )
    except DirectTransferError as exc:
        _raise_mapped(exc)
    notify = None
    if not replay:
        append_audit(
            db,
            entity_type="TRANCHE",
            entity_id=tranche_id,
            action="REPAYMENT_DAYS_MARKED_PAID",
            actor_id=current_user.id,
            after_state={
                "repayment_day_ids": [str(day.id) for day in marked],
                "net_vnd": sum(day.net_vnd for day in marked),
                "receipt_file_id": str(receipt_id),
            },
            ip_address=request.client.host if request.client else None,
        )
        if check is not None:
            # What the system read, for the investor to see while confirming; nothing waits on it.
            await record_kept(db, check, receipt_id=receipt_id, tranche=tranche, days=marked, now=now)
        # The investor checks their account and confirms it arrived.
        notify = await repayment_receipts.notify_investor_of_receipt(
            db, tranche=tranche, receipt_id=receipt_id, net_vnd=sum(day.net_vnd for day in marked)
        )
    await db.commit()
    if notify is not None:
        await realtime.publish([notify])
    ids = {day.id for day in marked}
    rows = await tranche_schedule(db, tranche_id=tranche_id, user_id=current_user.id)
    return [RepaymentRowOut.model_validate(row) for row in rows if row["repayment_day_id"] in ids]


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


@router.get("/tranches/{tranche_id}/projection", response_model=ProjectedScheduleOut)
async def read_projection(
    tranche_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """A confirmed ticket, before its round forms the loan, for its investor or the business that will owe it: what
    its repayment days would be if the round closed on its last day (the latest it can). Projected, not guaranteed;
    once the loan forms, the real days are at /schedule. 409 NO_PROJECTION when the ticket is not confirmed or the
    loan already formed."""
    tranche = await db.get(Tranche, tranche_id)
    window = await db.get(FundingWindow, tranche.window_id) if tranche else None
    if window is None or current_user.id not in (tranche.investor_id, window.sme_user_id):
        raise HTTPException(status_code=404, detail="Ticket not found")
    if tranche.status != "CONFIRMED" or window.status not in ("OPEN", "FULL", "CLOSING"):
        raise HTTPException(
            status_code=409,
            detail="Only a confirmed ticket whose round is still closing has a projection",
            headers={"X-Error-Code": "NO_PROJECTION"},
        )
    # The latest the round can close: its last day while open, the end of the close wait once closing.
    close_at = window.close_wait_until if window.status == "CLOSING" and window.close_wait_until else window.window_end
    # The stub runs from when the money arrived, as at close (close._receipt_date).
    transfer = (
        await db.execute(
            select(Transfer)
            .where(Transfer.tranche_id == tranche.id, Transfer.kind == "DISBURSEMENT")
            .order_by(Transfer.created_at)
        )
    ).scalars().first()
    received_at = (transfer.transferred_at if transfer else None) or tranche.attested_at or tranche.confirmed_at
    return ProjectedScheduleOut.model_validate(
        project_ticket(
            amount_vnd=tranche.amount_vnd,
            loan_rate_pct=Decimal(window.annual_rate_pct),
            term_months=window.term_months,
            individual=tranche.investor_type == "INDIVIDUAL",
            confirmed_on=ict_date(received_at),
            close_on=ict_date(close_at),
        )
    )


@router.get("/receipts", response_model=list[RepaymentReceiptOut])
async def list_repayment_receipts(
    status: Literal["AWAITING", "ALL"] = "AWAITING",
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The receipts businesses uploaded for repaying this investor, each with the days it pays and what should have
    reached their account, newest first. AWAITING (default): the ones the investor has not confirmed yet; ALL adds
    the ones already settled, after those."""
    return await repayment_receipts.list_receipts(db, investor_id=current_user.id, status=status, limit=limit)


@router.post("/receipts/{receipt_id}/confirm", response_model=RepaymentReceiptOut)
async def confirm_repayment_receipt(
    receipt_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The investor received this repayment: each day it pays is matched and settled, and a day they had reported
    missing has that report closed as paid. Only the investor it pays (anyone else: 404); final (409
    NOTHING_TO_CONFIRM once done), audited, and the business is told."""
    return await repayment_receipts.confirm_receipt(
        db, receipt_id, investor=current_user, ip_address=request.client.host if request.client else None
    )
