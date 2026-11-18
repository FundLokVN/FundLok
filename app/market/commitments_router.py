"""Commit-then-accept funding flow over HTTP (HANDOFF-06 Phase 2, slice S2).

Thin handlers over Edward's service functions in app/direct_transfer/commit_flow.py: auth, the investor eligibility
gate, the market-side check of the investing-terms version and the HTTP error mapping, in the style of
funding_router. Spec: docs/specs/direct_transfer/commit-then-accept-flow.md sections 4.1 to 4.3, as amended by
Money flow FINAL v1.0: a pledge signs nothing (the Lender Service Agreement is signed with the Day-14 loan
contract) and can be withdrawn until the end of Day 12.
Phat may move or merge this file into funding_router.py.
"""
import datetime as dt
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer import commit_flow
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.schemas import CommitmentOut, FundingWindowOut
from app.direct_transfer.windows import get_window_progress
from app.market import funding
from app.market.funding_router import _raise_mapped
from app.market.service import _require_eligible_investor
from app.users.models import Role, User
from app.utils.rbac import require_roles

router = APIRouter(prefix="/funding", tags=["funding"])

require_investor = require_roles(Role.INVESTOR)
require_sme = require_roles(Role.SME)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class CommitmentCreate(BaseModel):
    amount_vnd: int
    ack_risk_disclosure: bool
    terms_version: str
    payment_account_id: UUID


class DecisionRequest(BaseModel):
    decision: Literal["ACCEPT", "DECLINE"]


@router.post("/windows/{window_id}/commitments", response_model=CommitmentOut, status_code=201)
async def commit(
    window_id: UUID,
    body: CommitmentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Pledge an amount to the window by pressing Confirm. No money moves and nothing is signed; the pledge is kept
    as a record. 409 WINDOW_FULL while the round is fully pledged."""
    await _require_eligible_investor(db, current_user.id)
    try:
        commitment = await commit_flow.commit_to_window(
            db,
            window_id=window_id,
            investor_id=current_user.id,
            amount_vnd=body.amount_vnd,
            ack_risk_disclosure=body.ack_risk_disclosure,
            terms_version=body.terms_version,
            current_terms_version=funding.INVESTOR_TERMS_VERSION,
            payment_account_id=body.payment_account_id,
            idempotency_key=idempotency_key or "",
            now=_now(),
            signer=funding._signer(),
            storage=funding.evidence_storage(),
        )
        view = await commit_flow.commitment_view(db, commitment)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    await db.commit()
    return CommitmentOut.model_validate(view)


@router.post("/commitments/{commitment_id}/withdraw", response_model=CommitmentOut)
async def withdraw(
    commitment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """Withdraw one of the caller's pledges, until the end of Day 12. The amount is free again for other lenders.
    409 WITHDRAW_CLOSED once the pledge list is final."""
    try:
        commitment = await commit_flow.withdraw_commitment(
            db, commitment_id=commitment_id, investor_id=current_user.id, now=_now()
        )
        view = await commit_flow.commitment_view(db, commitment)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    await db.commit()
    return CommitmentOut.model_validate(view)


@router.get("/me/commitments", response_model=list[CommitmentOut])
async def my_commitments(
    window_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_investor),
):
    """The caller's own commitments to the window, newest first."""
    try:
        views = await commit_flow.list_investor_commitments(db, window_id=window_id, investor_id=current_user.id)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    return [CommitmentOut.model_validate(v) for v in views]


@router.post("/windows/{window_id}/decision", response_model=FundingWindowOut)
async def decide(
    window_id: UUID,
    body: DecisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """The business accepts or declines the commitments, before the decision deadline."""
    now = _now()
    try:
        window = await commit_flow.decide_window(
            db,
            window_id=window_id,
            sme_user_id=current_user.id,
            decision=body.decision,
            idempotency_key=idempotency_key or "",
            now=now,
        )
        progress = await get_window_progress(db, listing_id=window.listing_id, now=now)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    await db.commit()
    return FundingWindowOut.model_validate(progress)
