"""Funding windows, investor side (HANDOFF-04 Epic 1, tickets 1.1, 1.3 and 1.4).

Edward's services hold the rules (app/direct_transfer); this router adds auth, the investor eligibility gate,
auditing and the HTTP error mapping. Reserving and signing the acceptance are one request here (the investor agrees
once), not the spec's two; see app/market/funding.py.
"""
import datetime as dt
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import Tranche
from app.direct_transfer.schemas import FundingWindowOut, PaymentOut, TrancheOut
from app.direct_transfer.tranches import list_investor_tranches, payment_instructions
from app.direct_transfer.windows import get_window_progress
from app.market.funding import reserve_and_accept
from app.market.schemas import ReservationCreate
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
    try:
        progress = await get_window_progress(db, listing_id=listing_id, now=_now())
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    return FundingWindowOut.model_validate(progress)


@router.post("/windows/{window_id}/reservations", response_model=TrancheOut, status_code=201)
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
    return TrancheOut.model_validate(tranche)


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


@router.get("/me/tranches", response_model=list[TrancheOut])
async def read_my_tranches(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The caller's tickets, newest first."""
    return [TrancheOut.model_validate(t) for t in await list_investor_tranches(db, investor_id=current_user.id)]
