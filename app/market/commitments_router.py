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

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer import commit_flow
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.schemas import CommitmentOut, FundingWindowOut
from app.direct_transfer.models import FundingWindow, WindowLoanContract
from app.direct_transfer.windows import get_window_progress
from app.loans import guarantor_otp
from app.market import day13_contracts, funding
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
            # Money flow FINAL v1.0: accepting means signing every lender's loan contract.
            contracts_signed=await day13_contracts.all_sme_signed(db, window_id),
        )
        await day13_contracts.void_for_window(db, window=window, now=now)
        progress = await get_window_progress(db, listing_id=window.listing_id, now=now)
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    await db.commit()
    return FundingWindowOut.model_validate(progress)


# --------------------------------------------------------------------------- #
# Day 13: each lender's loan contract, signed by the SME and the guarantor
# --------------------------------------------------------------------------- #
class WindowLoanContractOut(BaseModel):
    id: UUID
    contract_no: str
    lender_name: str  # masked
    amount_vnd: int
    status: Literal["GENERATED", "SME_SIGNED", "INVESTOR_SIGNED", "VOID"]
    generated_at: dt.datetime
    sme_signed_at: dt.datetime | None = None
    investor_signed_at: dt.datetime | None = None


class WindowLoanContractsOut(BaseModel):
    """Where Day-13 signing stands on the SME's round. `signing_cutoff_at`: 12:00 on Day 13 when the pledges are
    below 50% of target (signing must be finished by then); null otherwise. `all_signed`: every contract carries the
    SME's verified signature, so ACCEPT is open. The guarantor's signature is not a condition."""

    window_id: UUID
    status: str
    pledged_vnd: int
    target_vnd: int
    sme_deadline_at: dt.datetime | None
    signing_cutoff_at: dt.datetime | None
    all_signed: bool
    guarantor_signed_at: dt.datetime | None
    guarantor_phone: str | None
    contracts: list[WindowLoanContractOut]


class GuarantorCodeOut(BaseModel):
    phone: str
    channel: str
    expires_in: int
    resend_in: int


class GuarantorSignIn(BaseModel):
    code: str


async def _sme_window(db: AsyncSession, window_id: UUID, user: User) -> FundingWindow:
    window = await db.get(FundingWindow, window_id)
    if window is None or window.sme_user_id != user.id:
        raise HTTPException(status_code=404, detail="Window not found")
    return window


async def _contracts_out(db: AsyncSession, window: FundingWindow, user: User) -> WindowLoanContractsOut:
    view = await day13_contracts.view_for_sme(db, window=window, sme=user)
    return WindowLoanContractsOut(
        window_id=view.window_id, status=view.status, pledged_vnd=view.pledged_vnd, target_vnd=view.target_vnd,
        sme_deadline_at=view.sme_deadline_at, signing_cutoff_at=view.signing_cutoff_at, all_signed=view.all_signed,
        guarantor_signed_at=view.guarantor_signed_at, guarantor_phone=view.guarantor_phone,
        contracts=[
            WindowLoanContractOut(
                id=r.id, contract_no=r.contract_no, lender_name=view.lender_names.get(r.investor_id, ""),
                amount_vnd=r.amount_vnd, status=r.status, generated_at=r.generated_at, sme_signed_at=r.sme_signed_at,
                investor_signed_at=r.investor_signed_at,
            )
            for r in view.contracts
        ],
    )


@router.get("/windows/{window_id}/loan-contracts", response_model=WindowLoanContractsOut)
async def sme_loan_contracts(
    window_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Each lender's loan contract on the SME's round (generated at 00:00 on Day 13), and where signing stands."""
    window = await _sme_window(db, window_id, current_user)
    return await _contracts_out(db, window, current_user)


async def _own_contract(db: AsyncSession, window: FundingWindow, contract_id: UUID) -> WindowLoanContract:
    row = await db.get(WindowLoanContract, contract_id)
    if row is None or row.window_id != window.id:
        raise HTTPException(status_code=404, detail="Contract not found")
    return row


@router.get(
    "/windows/{window_id}/loan-contracts/{contract_id}/document",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def sme_loan_contract_document(
    window_id: UUID,
    contract_id: UUID,
    signed: bool = Query(False, description="true: the SME's signed copy on file"),
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """One lender's contract as generated (to sign), or the signed copy on file."""
    window = await _sme_window(db, window_id, current_user)
    row = await _own_contract(db, window, contract_id)
    key = row.sme_signed_file_key if signed else row.file_key
    if key is None:
        raise HTTPException(status_code=404, detail="No signed copy on file")
    content = await funding.evidence_storage().get(key, max_bytes=day13_contracts.MAX_BYTES)
    name = row.contract_no.split("/")[0]
    filename = f"FundLok-hop-dong-cho-vay-{name}{'-da-ky' if signed else ''}.pdf"
    disposition = "attachment" if download else "inline"
    return Response(
        content=content, media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/windows/{window_id}/loan-contracts/{contract_id}/signed", response_model=WindowLoanContractsOut)
async def sme_upload_signed_loan_contract(
    window_id: UUID,
    contract_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The SME's signed copy of one lender's contract (enterprise digital signature), checked with NEAC."""
    window = await _sme_window(db, window_id, current_user)
    row = await _own_contract(db, window, contract_id)
    content = await file.read(day13_contracts.MAX_BYTES + 1)
    try:
        await day13_contracts.record_sme_signed(
            db, window=window, row=row, content=content, file_name=file.filename, storage=funding.evidence_storage(),
            now=_now(), actor=current_user, ip_address=request.client.host if request.client else None,
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    await db.commit()
    return await _contracts_out(db, window, current_user)


@router.post("/windows/{window_id}/loan-contracts/guarantor-code", response_model=GuarantorCodeOut)
async def sme_guarantor_code(
    window_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Send the Personal Guarantor the Zalo code that signs every lender's contract on the round."""
    window = await _sme_window(db, window_id, current_user)
    row = await day13_contracts.send_guarantor_code(db, window=window, user=current_user, now=_now())
    out = GuarantorCodeOut(
        phone=guarantor_otp.masked_phone(row.phone), channel=row.channel,
        expires_in=int(guarantor_otp.CODE_TTL.total_seconds()),
        resend_in=int(guarantor_otp.RESEND_COOLDOWN.total_seconds()),
    )
    await db.commit()
    return out


@router.post("/windows/{window_id}/loan-contracts/guarantor-signature", response_model=WindowLoanContractsOut)
async def sme_guarantor_sign(
    window_id: UUID,
    body: GuarantorSignIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The Personal Guarantor signs every lender's contract with the Zalo code (not a condition of acceptance)."""
    window = await _sme_window(db, window_id, current_user)
    try:
        await day13_contracts.sign_as_guarantor(
            db, window=window, user=current_user, code=body.code, now=_now(),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except guarantor_otp.GuarantorCodeError as exc:
        if exc.persist:
            await db.commit()
        raise
    await db.commit()
    return await _contracts_out(db, window, current_user)
