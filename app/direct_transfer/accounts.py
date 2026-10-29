"""Registered payment accounts (HANDOFF-04 I-15). Spec: transfer-and-evidence.md section 3.

An SME's (or investor's) account is only usable after staff approve it from a bank statement or bank
confirmation: account ownership is otherwise unproven. This is NOT the mock `linked_accounts` table.
"""
from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.banks import require_bank
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import PaymentAccount


async def register_payment_account(
    db: AsyncSession,
    *,
    user_id: UUID,
    holder_role: str,
    holder_type: str,
    bank_bin: str,
    account_number: str,
    account_name: str,
    evidence_file_id: UUID | None,
    now: dt.datetime,
    bank_name: str | None = None,  # ignored: the name always comes from the bank list (banks.py)
) -> PaymentAccount:
    if holder_role not in ("SME", "INVESTOR") or holder_type not in ("INDIVIDUAL", "ENTITY"):
        raise bad_request("Invalid account holder")
    if not (bank_bin.isdigit() and len(bank_bin) == 6):
        raise bad_request("Invalid bank code")
    if not account_number.isalnum() or not (4 <= len(account_number) <= 32):
        raise bad_request("Invalid account number")
    if not account_name.strip():
        raise bad_request("Account name is required")
    bank = require_bank(bank_bin)
    account = PaymentAccount(
        user_id=user_id,
        holder_role=holder_role,
        holder_type=holder_type,
        bank_bin=bank_bin,
        bank_name=bank.short_name,
        account_number=account_number,
        account_name=account_name,
        evidence_file_id=evidence_file_id,
        status="PENDING",
        created_at=now,  # injected clock, so listing order is deterministic (server now() ties within a transaction)
    )
    db.add(account)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict("ACCOUNT_EXISTS", "This account is already registered") from exc
    await log_event(
        db,
        entity_type="PAYMENT_ACCOUNT",
        entity_id=account.id,
        event_type="PAYMENT_ACCOUNT_REGISTERED",
        now=now,
        actor_id=user_id,
        actor_role=holder_role,
        payload={"bank_bin": bank_bin, "bank_code": bank.code, "evidence_file_id": str(evidence_file_id) if evidence_file_id else None},
    )
    return account


async def decide_payment_account(
    db: AsyncSession,
    *,
    account_id: UUID,
    decision: str,
    reason: str,
    staff_id: UUID,
    now: dt.datetime,
) -> PaymentAccount:
    if decision not in ("APPROVED", "REJECTED"):
        raise bad_request("Invalid decision")
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    account = await db.get(PaymentAccount, account_id)
    if account is None:
        raise not_found("Payment account not found")
    if account.status != "PENDING":
        raise conflict("ALREADY_DECIDED", "This account has already been decided")
    account.status = decision
    account.approved_by = staff_id
    account.approved_at = now
    account.rejection_reason = reason if decision == "REJECTED" else None
    await log_event(
        db,
        entity_type="PAYMENT_ACCOUNT",
        entity_id=account.id,
        event_type="PAYMENT_ACCOUNT_DECISION",
        now=now,
        actor_id=staff_id,
        actor_role="ADMIN",
        reason=reason,
        payload={"decision": decision},
    )
    return account


async def get_usable_account(db: AsyncSession, *, account_id: UUID, user_id: UUID, holder_role: str) -> PaymentAccount:
    """Return an APPROVED account owned by `user_id`, else 409 ACCOUNT_NOT_APPROVED (404 if not theirs)."""
    account = (
        await db.execute(
            select(PaymentAccount).where(PaymentAccount.id == account_id, PaymentAccount.user_id == user_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise not_found("Payment account not found")
    if account.holder_role != holder_role or account.status != "APPROVED":
        raise conflict("ACCOUNT_NOT_APPROVED", "The payment account is not approved")
    return account


async def list_payment_accounts(db: AsyncSession, *, user_id: UUID) -> list[PaymentAccount]:
    """The caller's own accounts, oldest first, with their approval status (spec: GET /payment-accounts)."""
    rows = await db.execute(
        select(PaymentAccount).where(PaymentAccount.user_id == user_id).order_by(PaymentAccount.created_at, PaymentAccount.id)
    )
    return list(rows.scalars().all())
