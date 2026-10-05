"""Payout instruction lifecycle: create -> approve -> submit -> settle.

Spec: docs/specs/payments/payout-idempotency.md

Locking rules (spec section 6.2):

1. Creating a disbursement instruction locks the contract row
   (SELECT ... FOR UPDATE), so concurrent creates for one contract run one at
   a time and the second one sees the first. The partial unique index
   uq_payout_instructions_active_disbursement is the backstop for any path
   that skips the lock.
2. Approve and cancel lock the instruction row.
3. The dispatcher claims an instruction with one conditional UPDATE
   (APPROVED -> SUBMITTING, attempt_count + 1) and COMMITS before calling the
   bank. No row lock or open transaction is ever held across a network call.
4. The bank's answer is recorded only against that exact claim
   (status = SUBMITTING and attempt_count = n). An answer that arrives after
   the lease sweeper moved the row to UNKNOWN is routed through the same
   outcome logic a webhook or status inquiry uses.
5. SUCCEEDED posts the ledger in the same transaction as the status change,
   keyed on "payout:<instruction id>", so duplicate outcomes (late response,
   webhook, inquiry) post exactly once.

The bank request id (`client_request_id`) is generated once, when the
instruction is created, and is sent unchanged on every retry and inquiry.
A timeout is never treated as a failure: the instruction goes to UNKNOWN and
the bank is asked, by that id, what happened.
"""
from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.banking.gateway import (
    InquiryOutcome,
    InquiryResult,
    PayoutGateway,
    SubmitOutcome,
    SubmitResult,
    TransferRequest,
)
from app.core.config import settings
from app.ledger.service import LedgerLeg, get_or_create_account, post_transaction, resolve_custodial_account
from app.lending.models import Contract, Holding, Listing
from app.payments.idempotency import busy_conflict, is_lock_timeout, set_money_lock_timeout
from app.payments.models import (
    PAYOUT_RELEASED_STATUSES,
    PAYOUT_TRANSITIONS,
    PayoutBeneficiary,
    PayoutEvent,
    PayoutInstruction,
)
from app.payments.service import resolve_borrower_user_id

logger = logging.getLogger("app.payments.payouts")

SessionFactory = Callable[[], AsyncSession]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CLIENT_REQUEST_ID_PREFIX = "FL"


class PayoutError(Exception):
    """Base class for payout lifecycle errors raised outside HTTP handlers."""


class UnknownPayoutReference(PayoutError):
    """A bank outcome referenced a client_request_id we never issued."""


class PayoutIntegrityError(PayoutError):
    """Settlement found a ledger transaction that does not belong to the
    instruction. Nothing is written; ops must investigate."""


def new_client_request_id() -> str:
    """"FL" + a ULID: 48-bit millisecond timestamp + 80 random bits, in
    Crockford base32. 28 characters of [0-9A-Z] -- inside Circular 64's
    60-character Request-ID limit and safe for any bank's charset. Generated
    once per instruction; the UNIQUE constraint catches the (astronomically
    unlikely) collision."""
    value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return CLIENT_REQUEST_ID_PREFIX + "".join(reversed(chars))


def mask_account_number(account_number: str) -> str:
    return f"****{account_number[-4:]}"


async def _set_actor(db: AsyncSession, actor_id: UUID | None) -> None:
    """Expose the acting user to the status-log trigger for this transaction."""
    await db.execute(
        text("SELECT set_config('fundlok.actor_id', :actor, true)"),
        {"actor": str(actor_id) if actor_id else ""},
    )


async def _lock_instruction(db: AsyncSession, **where: Any) -> PayoutInstruction | None:
    stmt = select(PayoutInstruction).with_for_update().execution_options(populate_existing=True)
    for column, value in where.items():
        stmt = stmt.where(getattr(PayoutInstruction, column) == value)
    try:
        return (await db.execute(stmt)).scalar_one_or_none()
    except DBAPIError as exc:
        if is_lock_timeout(exc):
            raise busy_conflict() from exc
        raise


def _transition(instruction: PayoutInstruction, target: str) -> None:
    if target not in PAYOUT_TRANSITIONS[instruction.status]:
        raise HTTPException(
            status_code=409,
            detail=f"Invalid state transition: {instruction.status} → {target}",
        )
    instruction.status = target


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Maker / checker (HTTP-facing; raise HTTPException)
# --------------------------------------------------------------------------- #


async def create_disbursement_instruction(
    db: AsyncSession, *, contract_id: UUID, beneficiary_id: UUID, maker_id: UUID
) -> PayoutInstruction:
    """Create the single disbursement instruction for a fully funded contract.

    The amount is derived here, never taken from the request: it is the
    contract's target, and only when the contract, the listing and the
    holdings all agree on it. The destination comes from the verified
    beneficiary registry, never from free text."""
    await set_money_lock_timeout(db)
    await _set_actor(db, maker_id)
    try:
        contract = (
            await db.execute(select(Contract).where(Contract.id == contract_id).with_for_update())
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if is_lock_timeout(exc):
            raise busy_conflict() from exc
        raise
    if contract is None:
        raise HTTPException(status_code=404, detail="Contract not found")
    if contract.status != "ACTIVE_FUNDED":
        raise HTTPException(status_code=409, detail="Contract must be ACTIVE_FUNDED before disbursement")

    listing = (await db.execute(select(Listing).where(Listing.contract_id == contract_id))).scalar_one_or_none()
    if listing is None or listing.status != "FUNDED":
        raise HTTPException(status_code=409, detail="Listing must be FUNDED before disbursement")

    amount = Decimal(contract.target_amount)
    holdings_total = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(Holding.principal), 0)).where(Holding.contract_id == contract_id)
            )
        ).scalar_one()
    )
    if holdings_total != amount or Decimal(listing.funded_amount) != amount:
        raise HTTPException(
            status_code=409,
            detail="Funding records do not reconcile with the contract target; disbursement is blocked until they do",
        )

    existing = (
        await db.execute(
            select(PayoutInstruction.status).where(
                PayoutInstruction.contract_id == contract_id,
                PayoutInstruction.purpose == "SME_DISBURSEMENT",
                PayoutInstruction.status.notin_(PAYOUT_RELEASED_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Contract already has a disbursement instruction in status {existing}",
        )

    borrower_user_id = await resolve_borrower_user_id(db, contract)
    beneficiary = (
        await db.execute(select(PayoutBeneficiary).where(PayoutBeneficiary.id == beneficiary_id))
    ).scalar_one_or_none()
    if beneficiary is None or beneficiary.owner_user_id != borrower_user_id or beneficiary.status != "VERIFIED":
        raise HTTPException(status_code=422, detail="Beneficiary is not a verified account of the borrower")

    custodial_account = await resolve_custodial_account(db, contract_id=contract_id)
    client_request_id = new_client_request_id()
    instruction = PayoutInstruction(
        client_request_id=client_request_id,
        purpose="SME_DISBURSEMENT",
        contract_id=contract_id,
        custodial_account_id=custodial_account.id,
        beneficiary_id=beneficiary.id,
        beneficiary_bank_bin=beneficiary.bank_bin,
        beneficiary_account_number=beneficiary.account_number,
        beneficiary_account_name=beneficiary.account_name,
        amount=amount,
        currency="VND",
        description=f"FUNDLOK {client_request_id}",
        status="PENDING_APPROVAL",
        created_by=maker_id,
        attempt_count=0,
    )
    db.add(instruction)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Only reachable if something created a live instruction without
        # taking the contract lock; the partial unique index refused it.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Contract already has a disbursement instruction in progress"
        ) from exc
    return instruction


async def approve_instruction(db: AsyncSession, *, instruction_id: UUID, checker_id: UUID) -> PayoutInstruction:
    await set_money_lock_timeout(db)
    await _set_actor(db, checker_id)
    instruction = await _lock_instruction(db, id=instruction_id)
    if instruction is None:
        raise HTTPException(status_code=404, detail="Payout instruction not found")
    if instruction.created_by == checker_id:
        raise HTTPException(status_code=403, detail="The approver must be a different person from the requester")
    if instruction.status != "PENDING_APPROVAL":
        raise HTTPException(
            status_code=409, detail=f"Invalid state transition: {instruction.status} → APPROVED"
        )
    _transition(instruction, "APPROVED")
    instruction.approved_by = checker_id
    instruction.approved_at = _now()
    await db.flush()
    return instruction


async def cancel_instruction(db: AsyncSession, *, instruction_id: UUID, actor_id: UUID) -> PayoutInstruction:
    await set_money_lock_timeout(db)
    await _set_actor(db, actor_id)
    instruction = await _lock_instruction(db, id=instruction_id)
    if instruction is None:
        raise HTTPException(status_code=404, detail="Payout instruction not found")
    if instruction.status not in ("PENDING_APPROVAL", "APPROVED"):
        raise HTTPException(
            status_code=409, detail=f"Invalid state transition: {instruction.status} → CANCELLED"
        )
    _transition(instruction, "CANCELLED")
    instruction.cancelled_by = actor_id
    await db.flush()
    return instruction


# --------------------------------------------------------------------------- #
# Dispatcher (background; never raises HTTPException to a caller)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SubmissionTicket:
    instruction_id: UUID
    attempt_no: int
    request: TransferRequest


async def claim_for_submission(db: AsyncSession, instruction_id: UUID) -> SubmissionTicket | None:
    """APPROVED -> SUBMITTING in one conditional UPDATE. Of any number of
    concurrent claimers exactly one gets a row back: the others wait on the
    row lock, re-check `status = 'APPROVED'` against the new row version, and
    match nothing. The caller must COMMIT before calling the bank."""
    row = (
        await db.execute(
            update(PayoutInstruction)
            .where(PayoutInstruction.id == instruction_id, PayoutInstruction.status == "APPROVED")
            .values(
                status="SUBMITTING",
                attempt_count=PayoutInstruction.attempt_count + 1,
                last_submitted_at=func.now(),
                lease_expires_at=func.now() + timedelta(seconds=settings.PAYOUT_SUBMIT_LEASE_SECONDS),
                next_check_at=None,
            )
            .returning(
                PayoutInstruction.id,
                PayoutInstruction.attempt_count,
                PayoutInstruction.client_request_id,
                PayoutInstruction.amount,
                PayoutInstruction.beneficiary_bank_bin,
                PayoutInstruction.beneficiary_account_number,
                PayoutInstruction.beneficiary_account_name,
                PayoutInstruction.description,
            )
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if row is None:
        return None
    return SubmissionTicket(
        instruction_id=row.id,
        attempt_no=row.attempt_count,
        request=TransferRequest(
            client_request_id=row.client_request_id,
            amount=int(row.amount),
            beneficiary_bank_bin=row.beneficiary_bank_bin,
            beneficiary_account_number=row.beneficiary_account_number,
            beneficiary_account_name=row.beneficiary_account_name,
            description=row.description,
        ),
    )


_SUBMIT_TARGET = {
    SubmitOutcome.ACCEPTED: "SUBMITTED",
    SubmitOutcome.SUCCEEDED: "SUCCEEDED",
    SubmitOutcome.REJECTED: "FAILED",
    SubmitOutcome.NOT_SENT: "APPROVED",
    SubmitOutcome.UNKNOWN: "UNKNOWN",
}


def _event_detail(message: str | None) -> dict[str, Any] | None:
    # Bank messages can echo request fields; keep them short and never store
    # account numbers or credentials here.
    return {"message": message[:500]} if message else None


async def record_submit_result(db: AsyncSession, ticket: SubmissionTicket, result: SubmitResult) -> str:
    instruction = await _lock_instruction(db, id=ticket.instruction_id)
    assert instruction is not None
    db.add(
        PayoutEvent(
            instruction_id=instruction.id,
            event_type="SUBMIT_RESULT",
            attempt_no=ticket.attempt_no,
            outcome=result.outcome.value,
            bank_code=result.bank_code,
            detail=_event_detail(result.message),
        )
    )
    if instruction.status != "SUBMITTING" or instruction.attempt_count != ticket.attempt_no:
        # The lease lapsed and the sweeper (or a webhook) already moved it on.
        # Final answers still count; anything else is left to the inquiry loop.
        if result.outcome in (SubmitOutcome.SUCCEEDED, SubmitOutcome.REJECTED):
            await _apply_outcome_locked(
                db,
                instruction,
                "SUCCEEDED" if result.outcome is SubmitOutcome.SUCCEEDED else "FAILED",
                bank_transaction_ref=result.bank_transaction_ref,
                bank_code=result.bank_code,
                message=result.message,
                source="LATE_SUBMIT_RESPONSE",
            )
        await db.flush()
        return instruction.status

    target = _SUBMIT_TARGET[result.outcome]
    if target in ("SUCCEEDED", "FAILED"):
        await _apply_final(db, instruction, target, result.bank_transaction_ref, result.bank_code, result.message)
    else:
        _transition(instruction, target)
        instruction.lease_expires_at = None
        if result.bank_transaction_ref and not instruction.bank_transaction_ref:
            instruction.bank_transaction_ref = result.bank_transaction_ref
        # SUBMITTED/UNKNOWN: ask the bank again later. APPROVED (NOT_SENT):
        # back off before the dispatcher picks it up again.
        instruction.next_check_at = _now() + timedelta(seconds=settings.PAYOUT_INQUIRY_INTERVAL_SECONDS)
    await db.flush()
    return instruction.status


async def submit_instruction(session_factory: SessionFactory, gateway: PayoutGateway, instruction_id: UUID) -> str:
    """Claim -> COMMIT -> call the bank -> record, in that order."""
    async with session_factory() as db:
        ticket = await claim_for_submission(db, instruction_id)
        if ticket is None:
            return "SKIPPED"
        await db.commit()

    try:
        result = await gateway.submit_transfer(ticket.request)
    except Exception as exc:  # noqa: BLE001 -- anything unexpected may have reached the bank
        logger.exception("payout %s: submit raised; treating as UNKNOWN", instruction_id)
        result = SubmitResult(outcome=SubmitOutcome.UNKNOWN, message=type(exc).__name__)

    async with session_factory() as db:
        status = await record_submit_result(db, ticket, result)
        await db.commit()
    return status


async def _settle(db: AsyncSession, instruction: PayoutInstruction) -> UUID:
    contract = (await db.execute(select(Contract).where(Contract.id == instruction.contract_id))).scalar_one()
    omnibus_cash = await get_or_create_account(
        db, account_type="OMNIBUS_CASH", custodial_account_id=instruction.custodial_account_id
    )
    borrower_account = await get_or_create_account(
        db,
        account_type="BORROWER",
        custodial_account_id=instruction.custodial_account_id,
        owner_user_id=await resolve_borrower_user_id(db, contract),
        contract_id=instruction.contract_id,
    )
    transaction = await post_transaction(
        db,
        event_type="DISBURSEMENT",
        legs=[
            LedgerLeg(
                debit_account_id=omnibus_cash.id,
                credit_account_id=borrower_account.id,
                amount=Decimal(instruction.amount),
                type="DISBURSEMENT",
                contract_id=instruction.contract_id,
                reference=instruction.client_request_id,
            )
        ],
        idempotency_key=f"payout:{instruction.id}",
        reference=instruction.client_request_id,
    )
    if transaction.event_type != "DISBURSEMENT" or transaction.contract_id != instruction.contract_id:
        raise PayoutIntegrityError(
            f"ledger key payout:{instruction.id} is held by an unrelated transaction {transaction.id}"
        )
    return transaction.id


async def _apply_final(
    db: AsyncSession,
    instruction: PayoutInstruction,
    target: str,
    bank_transaction_ref: str | None,
    bank_code: str | None,
    message: str | None,
) -> None:
    if target not in PAYOUT_TRANSITIONS[instruction.status]:
        raise PayoutError(f"invalid transition {instruction.status} -> {target} for {instruction.id}")
    if bank_transaction_ref and not instruction.bank_transaction_ref:
        instruction.bank_transaction_ref = bank_transaction_ref
    if target == "SUCCEEDED":
        instruction.ledger_transaction_id = await _settle(db, instruction)
        instruction.settled_at = _now()
    else:
        instruction.failure_code = bank_code
        instruction.failure_reason = (message or "")[:500] or None
    instruction.status = target
    instruction.lease_expires_at = None
    instruction.next_check_at = None


async def _apply_outcome_locked(
    db: AsyncSession,
    instruction: PayoutInstruction,
    outcome: str,
    *,
    bank_transaction_ref: str | None = None,
    bank_code: str | None = None,
    message: str | None = None,
    source: str,
) -> PayoutInstruction:
    """Apply a bank-reported outcome (SUCCEEDED / FAILED / PENDING) to an
    instruction whose row the caller has locked. Safe to call any number of
    times with the same outcome."""
    detail: dict[str, Any] = {"source": source}
    if message:
        detail["message"] = message[:500]

    def _event(**extra: Any) -> None:
        db.add(
            PayoutEvent(
                instruction_id=instruction.id,
                event_type="OUTCOME_APPLIED",
                outcome=outcome,
                bank_code=bank_code,
                detail={**detail, **extra},
            )
        )

    if outcome == "PENDING":
        if instruction.status == "UNKNOWN":
            instruction.status = "SUBMITTED"
        if instruction.status in ("SUBMITTED",):
            instruction.next_check_at = _now() + timedelta(seconds=settings.PAYOUT_INQUIRY_INTERVAL_SECONDS)
        _event()
        return instruction

    if instruction.status == outcome:
        _event(duplicate=True)
        return instruction
    if outcome not in PAYOUT_TRANSITIONS[instruction.status]:
        # SUCCEEDED after FAILED (or the reverse), or an outcome for an
        # instruction that was never sent. Never overwrite: alert and keep
        # the evidence.
        logger.critical(
            "payout %s: bank reported %s while instruction is %s (source=%s); manual investigation required",
            instruction.id,
            outcome,
            instruction.status,
            source,
        )
        _event(conflict=True, status_at_receipt=instruction.status)
        return instruction

    await _apply_final(db, instruction, outcome, bank_transaction_ref, bank_code, message)
    _event()
    return instruction


async def apply_bank_outcome(
    db: AsyncSession,
    *,
    client_request_id: str,
    outcome: str,
    bank_transaction_ref: str | None = None,
    bank_code: str | None = None,
    message: str | None = None,
    source: str,
) -> PayoutInstruction:
    """Entry point for asynchronous outcomes (bank webhook, statement match,
    manual resolution). Idempotent; the caller commits."""
    if outcome not in ("SUCCEEDED", "FAILED", "PENDING"):
        raise ValueError(f"unsupported outcome {outcome!r}")
    instruction = await _lock_instruction(db, client_request_id=client_request_id)
    if instruction is None:
        raise UnknownPayoutReference(client_request_id)
    await _apply_outcome_locked(
        db,
        instruction,
        outcome,
        bank_transaction_ref=bank_transaction_ref,
        bank_code=bank_code,
        message=message,
        source=source,
    )
    await db.flush()
    return instruction


async def expire_stale_submissions(db: AsyncSession) -> int:
    """SUBMITTING rows whose dispatcher lease lapsed (worker crashed or hung
    after the claim) become UNKNOWN -- never APPROVED, because the request
    may already have reached the bank."""
    rows = (
        await db.execute(
            update(PayoutInstruction)
            .where(PayoutInstruction.status == "SUBMITTING", PayoutInstruction.lease_expires_at < func.now())
            .values(status="UNKNOWN", lease_expires_at=None, next_check_at=func.now())
            .returning(PayoutInstruction.id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    for row in rows:
        logger.warning("payout %s: dispatcher lease expired; marked UNKNOWN", row.id)
    return len(rows)


async def inquire_instruction(session_factory: SessionFactory, gateway: PayoutGateway, instruction_id: UUID) -> str:
    """Ask the bank what happened to a SUBMITTED or UNKNOWN instruction."""
    async with session_factory() as db:
        # Push next_check_at forward first, so two reconcilers never inquire
        # the same instruction at once.
        row = (
            await db.execute(
                update(PayoutInstruction)
                .where(
                    PayoutInstruction.id == instruction_id,
                    PayoutInstruction.status.in_(("SUBMITTED", "UNKNOWN")),
                    (PayoutInstruction.next_check_at.is_(None)) | (PayoutInstruction.next_check_at <= func.now()),
                )
                .values(next_check_at=func.now() + timedelta(seconds=settings.PAYOUT_INQUIRY_INTERVAL_SECONDS))
                .returning(PayoutInstruction.client_request_id)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        await db.commit()
    if row is None:
        return "SKIPPED"

    try:
        result = await gateway.inquire_transfer(row.client_request_id)
    except Exception:  # noqa: BLE001
        logger.exception("payout %s: inquiry raised", instruction_id)
        result = InquiryResult(outcome=InquiryOutcome.ERROR)

    async with session_factory() as db:
        instruction = await _lock_instruction(db, id=instruction_id)
        assert instruction is not None
        age_seconds = float(
            (
                await db.execute(
                    select(func.extract("epoch", func.now() - PayoutInstruction.last_submitted_at)).where(
                        PayoutInstruction.id == instruction_id
                    )
                )
            ).scalar_one()
            or 0
        )
        db.add(
            PayoutEvent(
                instruction_id=instruction.id,
                event_type="INQUIRY_RESULT",
                attempt_no=instruction.attempt_count,
                outcome=result.outcome.value,
                bank_code=result.bank_code,
                detail=_event_detail(result.message),
            )
        )
        if result.outcome in (InquiryOutcome.SUCCEEDED, InquiryOutcome.FAILED, InquiryOutcome.PENDING):
            await _apply_outcome_locked(
                db,
                instruction,
                result.outcome.value,
                bank_transaction_ref=result.bank_transaction_ref,
                bank_code=result.bank_code,
                message=result.message,
                source="INQUIRY",
            )
        elif result.outcome is InquiryOutcome.NOT_FOUND and instruction.status == "SUBMITTED":
            # The bank acknowledged this instruction earlier; "not found" now
            # is an anomaly, not permission to resend.
            instruction.status = "MANUAL_REVIEW"
            instruction.next_check_at = None
        elif result.outcome is InquiryOutcome.NOT_FOUND and instruction.status == "UNKNOWN":
            if (
                settings.PAYOUT_RESEND_ON_NOT_FOUND
                and age_seconds >= settings.PAYOUT_NOT_FOUND_RESEND_AFTER_SECONDS
            ):
                # Same client_request_id goes out again; only enabled once the
                # bank guarantees it rejects duplicates of that id.
                instruction.status = "APPROVED"
                instruction.next_check_at = None
            elif age_seconds >= settings.PAYOUT_UNKNOWN_ESCALATE_AFTER_SECONDS:
                instruction.status = "MANUAL_REVIEW"
                instruction.next_check_at = None
        elif (
            result.outcome is InquiryOutcome.ERROR
            and instruction.status == "UNKNOWN"
            and age_seconds >= settings.PAYOUT_UNKNOWN_ESCALATE_AFTER_SECONDS
        ):
            instruction.status = "MANUAL_REVIEW"
            instruction.next_check_at = None
        await db.flush()
        status = instruction.status
        await db.commit()
    return status


async def dispatch_approved(session_factory: SessionFactory, gateway: PayoutGateway, *, limit: int = 20) -> list[str]:
    async with session_factory() as db:
        ids = (
            await db.execute(
                select(PayoutInstruction.id)
                .where(
                    PayoutInstruction.status == "APPROVED",
                    (PayoutInstruction.next_check_at.is_(None)) | (PayoutInstruction.next_check_at <= func.now()),
                )
                .order_by(PayoutInstruction.approved_at)
                .limit(limit)
            )
        ).scalars().all()
    return [await submit_instruction(session_factory, gateway, instruction_id) for instruction_id in ids]


async def reconcile_open_instructions(
    session_factory: SessionFactory, gateway: PayoutGateway, *, limit: int = 50
) -> list[str]:
    async with session_factory() as db:
        await expire_stale_submissions(db)
        await db.commit()
        ids = (
            await db.execute(
                select(PayoutInstruction.id)
                .where(
                    PayoutInstruction.status.in_(("SUBMITTED", "UNKNOWN")),
                    (PayoutInstruction.next_check_at.is_(None)) | (PayoutInstruction.next_check_at <= func.now()),
                )
                .order_by(PayoutInstruction.next_check_at.asc().nulls_first())
                .limit(limit)
            )
        ).scalars().all()
    return [await inquire_instruction(session_factory, gateway, instruction_id) for instruction_id in ids]
