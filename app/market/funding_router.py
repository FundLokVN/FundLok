"""Funding windows, investor side (HANDOFF-04 Epic 1, tickets 1.1, 1.3, 1.4 and 1.5).

Edward's services hold the rules (app/direct_transfer); this router adds auth, the investor eligibility gate,
auditing and the HTTP error mapping. Reserving and signing the acceptance are one request here (the investor agrees
once), not the spec's two; see app/market/funding.py.
"""
import datetime as dt
from uuid import UUID

from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer.close import request_early_close
from app.direct_transfer.config import DEFAULT_PARAMS
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import FundingWindow, RepaymentDay, Tranche, Transfer
from app.direct_transfer.receipts import ManualEntryExtractor
from app.direct_transfer.schemas import FundingWindowOut, PaymentOut, ReceiptResultOut
from app.direct_transfer.tranches import (
    list_investor_tranches,
    list_window_tranches,
    mask_name,
    payment_instructions,
    sme_dispute_tranche,
    submit_receipt,
)
from app.direct_transfer.windows import get_window_progress
from app.lending.models import Project
from app.market import funding
from app.market.emails import receipt_notice, send_investment_receipt_email
from app.market.funding import expire_lapsed_holds, reserve_and_accept
from app.market.portfolio import business_analytics, business_transactions, investor_analytics, investor_transactions
from app.market.projection import project_ticket, sum_by_day
from app.market.schemas import (
    BusinessAnalyticsOut,
    InvestorAnalyticsOut,
    InvestorTrancheOut,
    ReportMissingCreate,
    ReservationCreate,
    SmeRepaymentPlanOut,
    SmeRoundOut,
    SmeTicketOut,
    TransactionOut,
)
from app.market.service import _require_eligible_investor
from app.notifications import realtime
from app.notifications.models import EVENT_INVESTMENT_RECEIPT_SUBMITTED
from app.notifications.service import create_notification
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(prefix="/funding", tags=["funding"])

require_investor = require_roles(Role.INVESTOR)
require_sme = require_roles(Role.SME)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _raise_mapped(db: AsyncSession, exc: DirectTransferError) -> None:
    # A service that wrote an evidence row before failing marks the error `persist`; that row must survive.
    if exc.persist:
        await db.commit()
    raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code}) from exc


@router.get("/listings/{listing_id}/window", response_model=FundingWindowOut)
async def read_window(
    listing_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Progress of the listing's funding window: totals only, never another investor's name or amount."""
    now = _now()
    # Lapsed holds are released first, so the capacity shown is what a reservation would get.
    window_id = (
        await db.execute(select(FundingWindow.id).where(FundingWindow.listing_id == listing_id))
    ).scalar_one_or_none()
    if window_id is not None and await expire_lapsed_holds(db, window_id=window_id, now=now):
        await db.commit()
    try:
        progress = await get_window_progress(db, listing_id=listing_id, now=now)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    return FundingWindowOut.model_validate(progress)


@router.post("/windows/{window_id}/reservations", response_model=InvestorTrancheOut, status_code=201)
async def reserve(
    window_id: UUID,
    body: ReservationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Hold an amount on the window and sign the acceptance of the investing terms. The tranche comes back SIGNED;
    GET /funding/tranches/{id}/payment then gives the SME's account to pay into."""
    # Verified email, approved KYC and an approved bank account: the same gate as the legacy order path.
    await _require_eligible_investor(db, current_user.id)
    replay = idempotency_key is not None and (
        await db.execute(select(Tranche.id).where(Tranche.idempotency_key == idempotency_key))
    ).first() is not None
    try:
        tranche = await reserve_and_accept(
            db,
            window_id=window_id,
            investor_id=current_user.id,
            amount_vnd=body.amount_vnd,
            ack_risk_disclosure=body.ack_risk_disclosure,
            terms_version=body.terms_version,
            idempotency_key=idempotency_key or "",
            now=_now(),
            payment_account_id=body.payment_account_id,
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    if not replay:
        append_audit(
            db,
            entity_type="TRANCHE",
            entity_id=tranche.id,
            action="INVESTMENT_RESERVED",
            actor_id=current_user.id,
            after_state={
                "window_id": str(tranche.window_id),
                "amount_vnd": tranche.amount_vnd,
                "reference_code": tranche.reference_code,
                "terms_version": body.terms_version,
                "status": tranche.status,
            },
            ip_address=request.client.host if request.client else None,
        )
    await db.commit()
    return InvestorTrancheOut.model_validate(tranche)


@router.get("/tranches/{tranche_id}/payment", response_model=PaymentOut)
async def read_payment(
    tranche_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """Where and how to pay: the SME's account, the exact amount, the memo, and a VietQR carrying all three."""
    try:
        instructions = await payment_instructions(db, tranche_id=tranche_id, investor_id=current_user.id, now=_now())
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    return PaymentOut.model_validate(instructions)


@router.get("/me/transactions", response_model=list[TransactionOut])
async def read_my_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The investor's money in and out, newest first: each investment transferred, and each day the business repaid
    them (what reached them, after the tax the business keeps back)."""
    return [TransactionOut.model_validate(row) for row in await investor_transactions(db, investor_id=current_user.id)]


@router.get("/me/analytics", response_model=InvestorAnalyticsOut)
async def read_my_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The investor's portfolio: monthly capital invested and returned (capital and earnings after tax apart), by
    industry, and where the capital stands."""
    return InvestorAnalyticsOut.model_validate(
        await investor_analytics(db, investor_id=current_user.id, today=ict_date(_now()))
    )


@router.get("/me/business-transactions", response_model=list[TransactionOut])
async def read_business_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's money in and out, newest first: each investment received, each day it repaid an investor
    (what it transferred), and FundLok's fee invoices. Investors are masked."""
    return [TransactionOut.model_validate(row) for row in await business_transactions(db, sme_user_id=current_user.id)]


@router.get("/me/business-analytics", response_model=BusinessAnalyticsOut)
async def read_business_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's loan: what it owes and has repaid, its daily amount, month by month against its verified
    revenue, and the four groups of its business score."""
    return BusinessAnalyticsOut.model_validate(
        await business_analytics(db, sme_user_id=current_user.id, today=ict_date(_now()))
    )


@router.get("/me/tranches", response_model=list[InvestorTrancheOut])
async def read_my_tranches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The caller's tickets, newest first, each with the project it is in."""
    tranches = await list_investor_tranches(db, investor_id=current_user.id)
    windows = {
        window_id: (project, term_months)
        for window_id, project, term_months in (
            await db.execute(
                select(FundingWindow.id, Project, FundingWindow.term_months)
                .join(Project, Project.id == FundingWindow.project_id)
                .where(FundingWindow.id.in_({t.window_id for t in tranches}))
            )
        ).all()
    } if tranches else {}
    out = []
    for tranche in tranches:
        project, term_months = windows.get(tranche.window_id, (None, None))
        out.append(
            InvestorTrancheOut.model_validate(tranche).model_copy(
                update={
                    "project_id": project.id if project else None,
                    "project_name": project.legal_name if project else None,
                    "term_months": term_months,
                }
            )
        )
    return out


@router.post("/tranches/{tranche_id}/receipt", response_model=ReceiptResultOut, status_code=202)
async def upload_receipt(
    tranche_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """The investor's bank receipt for their transfer to the SME (ticket 1.5): PDF, PNG, JPEG or WEBP, at most
    10 MB. Staff read it in the MVP (check_status AWAITING_FIELDS). A receipt sent after the hold lapsed is still
    taken, for staff to review (LATE_RECEIPT), since the money may already have moved. The business is emailed to
    look for the money under the memo (once, not on a replay); its bell rings when staff approve the transfer."""
    limit = DEFAULT_PARAMS.max_evidence_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail="File too large", headers={"X-Error-Code": "FILE_TOO_LARGE"})
    content_type = funding.sniff_receipt_type(content)
    if content_type is None:
        raise HTTPException(
            status_code=415, detail="Upload a PDF, PNG, JPEG or WEBP", headers={"X-Error-Code": "UNSUPPORTED_TYPE"}
        )
    replay = idempotency_key is not None and (
        await db.execute(select(Transfer.id).where(Transfer.idempotency_key == idempotency_key))
    ).first() is not None
    try:
        result = await submit_receipt(
            db,
            tranche_id=tranche_id,
            investor_id=current_user.id,
            content=content,
            content_type=content_type,
            filename=(file.filename or "")[:255] or None,
            extractor=ManualEntryExtractor(),
            storage=funding.evidence_storage(),
            idempotency_key=idempotency_key,
            now=_now(),
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    notice = await receipt_notice(db, tranche_id=tranche_id) if not replay else None
    if not replay:
        append_audit(
            db,
            entity_type="TRANCHE",
            entity_id=tranche_id,
            action="INVESTMENT_RECEIPT_UPLOADED",
            actor_id=current_user.id,
            after_state={
                "transfer_id": str(result.transfer_id),
                "tranche_status": result.tranche_status,
                "check_status": result.check_status,
            },
            ip_address=request.client.host if request.client else None,
        )
        if notice is not None:
            create_notification(
                db,
                user_id=notice.sme_user_id,
                event=EVENT_INVESTMENT_RECEIPT_SUBMITTED,
                entity_type="TRANCHE",
                entity_id=tranche_id,
                data=notice.notification_data(result.tranche_status),
            )
    await db.commit()
    if notice is not None:
        await realtime.publish([notice.sme_user_id])
        background_tasks.add_task(send_investment_receipt_email, **notice.email)
    return ReceiptResultOut.model_validate(result)


# --------------------------------------------------------------------------- #
# the business's side (HANDOFF-04 tickets 1.6 and 1.7)
# --------------------------------------------------------------------------- #
@router.post("/tranches/{tranche_id}/report-missing", response_model=SmeTicketOut)
async def report_missing_transfer(
    tranche_id: UUID,
    request: Request,
    body: ReportMissingCreate | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business reports that an investor's transfer has not reached its account after a receipt was submitted.
    Moves the ticket to UNDER_REVIEW (SME_DISPUTE) and opens a dispute for FundLok staff to review."""
    statement = ((body.statement if body else None) or "").strip() or (
        "The business reported that the transfer was not received."
    )
    try:
        tranche = await sme_dispute_tranche(
            db,
            tranche_id=tranche_id,
            sme_user_id=current_user.id,
            statement=statement,
            file_id=None,
            now=_now(),
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche.id,
        action="INVESTMENT_REPORTED_MISSING",
        actor_id=current_user.id,
        after_state={
            "tranche_status": tranche.status,
            "review_cause": tranche.review_cause,
            "reason": statement,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    investor = await db.get(User, tranche.investor_id)
    return SmeTicketOut(
        id=tranche.id,
        amount_vnd=tranche.amount_vnd,
        status=tranche.status,
        reference_code=tranche.reference_code,
        investor_display=mask_name((investor.full_name or investor.email) if investor else ""),
        receipt_submitted_at=tranche.receipt_submitted_at,
        attested_at=tranche.attested_at,
        confirm_deadline=tranche.confirm_deadline,
        review_cause=tranche.review_cause,
    )
@router.get("/me/rounds", response_model=list[SmeRoundOut])
async def read_my_rounds(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's funding rounds, newest first: progress, and every investor's ticket (investor masked)."""
    now = _now()
    windows = (
        await db.execute(
            select(FundingWindow)
            .where(FundingWindow.sme_user_id == current_user.id)
            .order_by(FundingWindow.window_start.desc())
        )
    ).scalars().all()
    # Lapsed holds are released first, as on the investor's read.
    released = 0
    for window in windows:
        if window.status in ("OPEN", "FULL"):
            released += await expire_lapsed_holds(db, window_id=window.id, now=now)
    if released:
        await db.commit()
    rounds = []
    for window in windows:
        progress = await get_window_progress(db, listing_id=window.listing_id, now=now)
        project = await db.get(Project, window.project_id)
        tickets = await list_window_tranches(db, window_id=window.id, sme_user_id=current_user.id)
        rounds.append(
            SmeRoundOut(
                **FundingWindowOut.model_validate(progress).model_dump(),
                project_id=window.project_id,
                project_name=project.legal_name if project else "",
                tickets=[SmeTicketOut.model_validate(ticket) for ticket in tickets],
            )
        )
    return rounds


@router.get("/me/rounds/{window_id}/repayment-plan", response_model=SmeRepaymentPlanOut)
async def read_repayment_plan(
    window_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business's daily repayments, one row per working day summed over its investors. Once the loan has formed,
    the real days with how many were marked paid or reported missing; before, a projection from the investments
    confirmed so far, if the round closed on its last day (the latest it can): projected, not guaranteed. What goes
    to investors only: FundLok's fee is invoiced separately."""
    window = await db.get(FundingWindow, window_id)
    if window is None or window.sme_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Round not found")

    days = (
        await db.execute(
            select(RepaymentDay)
            .join(Tranche, Tranche.id == RepaymentDay.tranche_id)
            .where(Tranche.window_id == window_id)
            .order_by(RepaymentDay.due_date)
        )
    ).scalars().all()
    # Real days as soon as any investment repays (with repay_from_approval, while the round is still open).
    if days:
        rows = sum_by_day(
            [
                {"due_date": d.due_date, "principal_vnd": d.principal_vnd, "interest_vnd": d.interest_vnd,
                 "gross_vnd": d.gross_vnd, "tax_withheld_vnd": d.tax_withheld_vnd, "net_vnd": d.net_vnd}
                for d in days
            ]
        )
        counts: dict[dt.date, list[int]] = {}
        for d in days:
            paid, missing = counts.setdefault(d.due_date, [0, 0])
            counts[d.due_date] = [
                paid + (d.status in ("PRESUMED_PAID", "MATCHED")),
                missing + (d.status == "REPORTED_MISSED"),
            ]
        for row in rows:
            row["paid"], row["missing"] = counts[row["due_date"]]
        kind, assumed, start = "ACTUAL", None, min(d.due_date for d in days)
    elif window.status in ("OPEN", "FULL", "CLOSING"):
        close_at = (
            window.close_wait_until if window.status == "CLOSING" and window.close_wait_until else window.window_end
        )
        tickets = (
            await db.execute(select(Tranche).where(Tranche.window_id == window_id, Tranche.status == "CONFIRMED"))
        ).scalars().all()
        projected: list[dict] = []
        start = None
        for ticket in tickets:
            plan = project_ticket(
                amount_vnd=ticket.amount_vnd,
                loan_rate_pct=Decimal(window.annual_rate_pct),
                term_months=window.term_months,
                individual=ticket.investor_type == "INDIVIDUAL",
                confirmed_on=ict_date(ticket.attested_at or ticket.confirmed_at),
                close_on=ict_date(close_at),
            )
            projected.extend(plan["days"])
            start = plan["start_date"]
        rows = sum_by_day(projected)
        kind, assumed = "PROJECTED", ict_date(close_at)
    else:
        rows, kind, assumed, start = [], "ACTUAL", None, None

    return SmeRepaymentPlanOut(
        kind=kind,
        assumed_close_date=assumed,
        start_date=start,
        days=rows,
        totals={
            "gross_vnd": sum(r["gross_vnd"] for r in rows),
            "tax_withheld_vnd": sum(r["tax_withheld_vnd"] for r in rows),
            "net_vnd": sum(r["net_vnd"] for r in rows),
        },
    )


@router.post("/windows/{window_id}/close-early", response_model=FundingWindowOut)
async def close_round_early(
    window_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The business closes its round before day 14, once at least the minimum raise is confirmed. The round goes
    CLOSING; staff then form the loan (Advance round) once nothing is pending."""
    now = _now()
    try:
        window = await request_early_close(db, window_id=window_id, sme_user_id=current_user.id, now=now)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    append_audit(
        db,
        entity_type="FUNDING_WINDOW",
        entity_id=window_id,
        action="FUNDING_ROUND_CLOSE_REQUESTED",
        actor_id=current_user.id,
        after_state={"status": window.status, "close_reason": window.close_reason},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return FundingWindowOut.model_validate(await get_window_progress(db, listing_id=window.listing_id, now=now))
