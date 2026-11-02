"""Funding windows, investor side (HANDOFF-04 Epic 1, tickets 1.1, 1.3, 1.4 and 1.5).

Edward's services hold the rules (app/direct_transfer); this router adds auth, the investor eligibility gate,
auditing and the HTTP error mapping. Reserving and signing the acceptance are one request here (the investor agrees
once), not the spec's two; see app/market/funding.py.
"""
import datetime as dt
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer.config import DEFAULT_PARAMS
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import FundingWindow, Tranche, Transfer
from app.direct_transfer.receipts import ManualEntryExtractor
from app.direct_transfer.schemas import FundingWindowOut, PaymentOut, ReceiptResultOut
from app.direct_transfer.tranches import list_investor_tranches, payment_instructions, submit_receipt
from app.direct_transfer.windows import get_window_progress
from app.market import funding
from app.market.funding import expire_lapsed_holds, reserve_and_accept
from app.market.schemas import InvestorTrancheOut, ReservationCreate
from app.market.service import _require_eligible_investor
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(prefix="/funding", tags=["funding"])

require_investor = require_roles(Role.INVESTOR)


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


@router.get("/me/tranches", response_model=list[InvestorTrancheOut])
async def read_my_tranches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The caller's tickets, newest first."""
    return [InvestorTrancheOut.model_validate(t) for t in await list_investor_tranches(db, investor_id=current_user.id)]


# The leading bytes of each type a receipt may be. The browser's Content-Type is the uploader's claim; what is
# stored, and later opened by staff, is typed by its content.
_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _sniff(content: bytes) -> str | None:
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return next((kind for magic, kind in _SIGNATURES if content.startswith(magic)), None)


@router.post("/tranches/{tranche_id}/receipt", response_model=ReceiptResultOut, status_code=202)
async def upload_receipt(
    tranche_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """The investor's bank receipt for their transfer to the SME (ticket 1.5): PDF, PNG, JPEG or WEBP, at most
    10 MB. Staff read it in the MVP (check_status AWAITING_FIELDS). A receipt sent after the hold lapsed is still
    taken, for staff to review (LATE_RECEIPT), since the money may already have moved."""
    limit = DEFAULT_PARAMS.max_evidence_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status_code=413, detail="File too large", headers={"X-Error-Code": "FILE_TOO_LARGE"})
    content_type = _sniff(content)
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
    await db.commit()
    return ReceiptResultOut.model_validate(result)
