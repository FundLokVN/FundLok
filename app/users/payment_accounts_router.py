"""Payment accounts: save the bank account a user pays from or is paid into (HANDOFF-04 Epic 0).

The contract is docs/handoffs/HANDOFF-04-phat-tickets.md section 0, minus the proof upload: accounts
are saved without a bank statement (Phat's call; #72 had staff approve from one). The rules live in
Edward's app/direct_transfer services; this router adds auth, auditing and the HTTP error mapping.
An account is saved PENDING; an admin approves or rejects it (POST /admin/payment-accounts/{id}/decision), and
only an APPROVED account lists an SME's project or lets an investor invest.
"""
import datetime as dt
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.direct_transfer import banks, vietqr
from app.direct_transfer.accounts import list_payment_accounts, register_payment_account
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import PaymentAccount
from app.direct_transfer.schemas import BankOut, PaymentAccountOut
from app.users.models import Role, User
from app.users.schemas import (
    PaymentAccountCreate,
    VietQrOut,
    VietQrParseOut,
    VietQrParseRequest,
)
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(tags=["payment-accounts"])

authorized = require_roles(Role.SME, Role.INVESTOR)


def _http_error(exc: DirectTransferError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code})


async def _raise_mapped(db: AsyncSession, exc: DirectTransferError) -> None:
    # A service that wrote an audit row before failing marks the error `persist`;
    # that row must survive the error response.
    if exc.persist:
        await db.commit()
    raise _http_error(exc) from exc


@router.get("/direct-transfer/banks", response_model=list[BankOut])
async def read_banks(
    include_wallets: bool = False,
    current_user: User = Depends(get_current_user),
):
    return [BankOut.model_validate(bank) for bank in banks.list_banks(include_wallets=include_wallets)]


@router.post("/payment-accounts", response_model=PaymentAccountOut, status_code=201)
async def create_payment_account(
    body: PaymentAccountCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authorized),
):
    now = dt.datetime.now(dt.timezone.utc)
    try:
        account = await register_payment_account(
            db,
            user_id=current_user.id,
            holder_role=current_user.role,
            holder_type=body.holder_type,
            bank_bin=body.bank_bin,
            bank_name=body.other_bank_name,
            account_number=body.account_number,
            account_name=body.account_name,
            evidence_file_id=None,
            now=now,
        )
    except DirectTransferError as exc:
        await _raise_mapped(db, exc)
    append_audit(
        db,
        entity_type="PAYMENT_ACCOUNT",
        entity_id=account.id,
        action="PAYOUT_ACCOUNT_ADDED",
        actor_id=current_user.id,
        after_state={
            "bank_name": account.bank_name,
            "bank_bin": account.bank_bin,
            "account_last4": account.account_number[-4:],
            "holder_type": account.holder_type,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return PaymentAccountOut.model_validate(account)


@router.get("/payment-accounts", response_model=list[PaymentAccountOut])
async def read_payment_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authorized),
):
    accounts = await list_payment_accounts(db, user_id=current_user.id)
    return [PaymentAccountOut.model_validate(account) for account in accounts]


@router.post("/payment-accounts/parse-vietqr", response_model=VietQrParseOut)
async def parse_vietqr(
    body: VietQrParseRequest,
    current_user: User = Depends(authorized),
):
    """Read bank + account number from the QR a user's bank app shows, to fill the form.

    The browser decodes the image; only the decoded text comes here, and nothing is stored.
    """
    try:
        parsed = vietqr.parse_account_vietqr(body.payload)
    except vietqr.VietQrParseError as exc:
        detail = "This QR is not a bank account QR" if exc.code == "NOT_AN_ACCOUNT_QR" else "Not a valid VietQR"
        raise HTTPException(status_code=400, detail=detail, headers={"X-Error-Code": exc.code}) from exc
    try:
        bank = banks.require_bank(parsed.bank_bin)
    except DirectTransferError as exc:
        raise _http_error(exc) from exc
    return VietQrParseOut(
        bank_bin=bank.bin,
        bank_name=bank.short_name,
        account_number=parsed.account_number,
        account_name=parsed.account_name,
    )


@router.get("/payment-accounts/{account_id}/vietqr", response_model=VietQrOut)
async def read_payment_account_vietqr(
    account_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(authorized),
):
    account = (
        await db.execute(
            select(PaymentAccount).where(PaymentAccount.id == account_id, PaymentAccount.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Payment account not found", headers={"X-Error-Code": "NOT_FOUND"})
    bank = banks.get_bank(account.bank_bin)
    try:
        if bank is None or not bank.vietqr:
            raise ValueError("bank does not accept VietQR transfers")
        payload = vietqr.build_account_vietqr_payload(bank_bin=account.bank_bin, account_number=account.account_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="A VietQR cannot be made for this account", headers={"X-Error-Code": "QR_UNSUPPORTED"}
        ) from exc
    return VietQrOut(payload=payload)
