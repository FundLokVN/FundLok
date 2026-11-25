"""Tranche flow: sign, pay instructions, receipt, SME confirm/dispute, lapses (HANDOFF-04 B6/B7).

Spec: docs/specs/direct_transfer/funding-window.md (rules 8-10) and transfer-and-evidence.md (rules 3-12).
Every step appends to the append-only evidence log; every transition goes through `transition_tranche`.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import day14
from app.direct_transfer.calendar_utils import calendar_deadline
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.day14_times import window_cutoffs
from app.direct_transfer.disputes import open_tranche_dispute
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event, store_evidence_file
from app.direct_transfer.models import Dispute, FundingWindow, PaymentAccount, Tranche, Transfer
from app.direct_transfer.refunds import create_refund_transfer
from app.direct_transfer.receipts import (
    AMOUNT_MISMATCH,
    REFERENCE_MISSING,
    ExpectedTransfer,
    ReceiptExtractor,
    ReceiptFields,
    _normalize_memo,
    check_receipt,
)
from app.direct_transfer.signing import Signer, SignerIdentity
from app.direct_transfer.storage import EvidenceStorage
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.vietqr import build_vietqr_payload
from app.direct_transfer.windows import lock_window, refresh_window_fullness
from app.users.models import User

PAYMENT_WARNING = (
    "Pay from an account in your own name. Money sent above the ticket is not part of the loan "
    "and is not FundLok's liability. FundLok never holds this money."
)


# --------------------------------------------------------------------------- #
# lookups
# --------------------------------------------------------------------------- #
async def _investor_tranche(db: AsyncSession, tranche_id: UUID, investor_id: UUID, *, lock: bool = False) -> Tranche:
    stmt = select(Tranche).where(Tranche.id == tranche_id, Tranche.investor_id == investor_id)
    if lock:
        stmt = stmt.with_for_update()
    tranche = (await db.execute(stmt)).scalar_one_or_none()
    if tranche is None:
        raise not_found("Ticket not found")
    return tranche


async def _sme_tranche(db: AsyncSession, tranche_id: UUID, sme_user_id: UUID, *, lock: bool = False):
    stmt = select(Tranche).where(Tranche.id == tranche_id)
    if lock:
        stmt = stmt.with_for_update()
    tranche = (await db.execute(stmt)).scalar_one_or_none()
    if tranche is None:
        raise not_found("Ticket not found")
    window = await db.get(FundingWindow, tranche.window_id)
    if window.sme_user_id != sme_user_id:
        raise not_found("Ticket not found")
    return tranche, window


async def resolve_kyc_name(db: AsyncSession, user_id: UUID) -> str:
    """The investor's KYC name: latest APPROVED eKYC full name, else the account's full name."""
    name = await approved_kyc_name(db, user_id)
    if name:
        return name
    user = await db.get(User, user_id)
    return (user.full_name or "") if user else ""


async def approved_kyc_name(db: AsyncSession, user_id: UUID) -> str | None:
    """The latest APPROVED eKYC full name, or None. Unlike resolve_kyc_name, never the self-editable full_name."""
    from app.gverify.models import STATUS_APPROVED, GVerifyVerification

    row = (
        await db.execute(
            select(GVerifyVerification.full_name)
            .where(
                GVerifyVerification.user_id == user_id,
                GVerifyVerification.verification_type == "KYC",
                GVerifyVerification.status == STATUS_APPROVED,
                GVerifyVerification.full_name.is_not(None),
            )
            .order_by(GVerifyVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or None


# --------------------------------------------------------------------------- #
# sign
# --------------------------------------------------------------------------- #
async def accept_tranche(
    db: AsyncSession,
    *,
    tranche_id: UUID,
    investor_id: UUID,
    document: bytes | None,
    signer: Signer,
    storage: EvidenceStorage,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Tranche:
    """RESERVED -> SIGNED. The acceptance is signed with `Signer`; the contract takes effect when funds are confirmed."""
    p = resolve(params)
    tranche = await _investor_tranche(db, tranche_id, investor_id, lock=True)
    if tranche.status == "SIGNED":
        return tranche  # idempotent replay
    if tranche.status != "RESERVED":
        raise DirectTransferError(400, f"Invalid state transition: {tranche.status} → SIGNED", "INVALID_TRANSITION")
    if now > tranche.hold_expires_at:
        raise conflict("HOLD_EXPIRED", "The reservation hold has expired")
    window = await db.get(FundingWindow, tranche.window_id)
    cutoffs = window_cutoffs(window, p)
    if cutoffs and now >= cutoffs.pay_deadline_at:
        raise conflict("PAY_DEADLINE_PASSED", "Signing for this round closed at 14:45 on disbursement day")
    if window.status not in ("OPEN", "FULL") or now >= window.window_end:
        raise conflict("WINDOW_NOT_OPEN", "The window is not open")
    investor = await db.get(User, investor_id)
    body = document or (
        f"ACCEPTANCE window={window.id} tranche={tranche.id} amount_vnd={tranche.amount_vnd} ref={tranche.reference_code}"
    ).encode("utf-8")
    signature = await signer.sign(
        document=body,
        signer=SignerIdentity(user_id=investor_id, display_name=(investor.full_name or investor.email), role="INVESTOR"),
        now=now,
    )
    stored = await store_evidence_file(
        db, storage, owner_user_id=investor_id, purpose="SIGNED_DOCUMENT", content=signature.signed_content,
        content_type="application/pdf", filename=f"acceptance-{tranche.id}.pdf", params=p,
    )
    tranche.acceptance_file_key = stored.storage_key
    tranche.acceptance_sha256 = signature.sha256
    tranche.acceptance_certificate = signature.certificate
    await transition_tranche(db, tranche, "SIGNED", now=now, actor_id=investor_id, actor_role="INVESTOR")
    return tranche


@dataclasses.dataclass(frozen=True)
class PaymentInstructions:
    bank_name: str
    bank_bin: str
    account_number: str
    account_name: str
    amount_vnd: int
    reference_code: str
    vietqr_payload: str
    hold_expires_at: dt.datetime
    warning: str
    # Day-14 transfer rule: the QR's own code (also the memo) and its validity, and the round's cut-offs.
    qr_code: str | None = None
    qr_issued_at: dt.datetime | None = None
    qr_expires_at: dt.datetime | None = None
    pay_deadline_at: dt.datetime | None = None
    receipt_deadline_at: dt.datetime | None = None
    # The server's clock: the app counts the QR down on it, not on the device's.
    server_time: dt.datetime | None = None


async def payment_instructions(
    db: AsyncSession, *, tranche_id: UUID, investor_id: UUID, now: dt.datetime, params: DirectTransferParams | None = None
) -> PaymentInstructions:
    """Where and how to pay, with the current QR. The first call after signing issues the first QR (writes a
    payment_qrs row: the caller commits). Later calls return the latest QR, expired or not."""
    tranche = await _investor_tranche(db, tranche_id, investor_id)
    window = await db.get(FundingWindow, tranche.window_id)
    if tranche.status == "RESERVED" and window.flow == "COMMIT_THEN_ACCEPT":
        # Day 14 (Money flow FINAL v1.0): the VietQR appears only once the lender signed its loan contract.
        raise conflict("CONTRACT_REQUIRED", "Sign your loan contract first")
    if tranche.status != "SIGNED":
        raise conflict("TRANCHE_NOT_SIGNED", "Sign the acceptance first")
    cutoffs = window_cutoffs(window, params)
    if cutoffs and now >= cutoffs.pay_deadline_at:
        raise conflict("PAY_DEADLINE_PASSED", "Payments for this round closed at 14:45 on disbursement day")
    qr = await day14.current_qr(db, tranche=tranche, window=window, now=now, params=params)
    account = await db.get(PaymentAccount, window.sme_payment_account_id)
    payload = build_vietqr_payload(
        bank_bin=account.bank_bin, account_number=account.account_number, amount_vnd=tranche.amount_vnd,
        memo=qr.code if qr else tranche.reference_code,
    )
    return PaymentInstructions(
        bank_name=account.bank_name,
        bank_bin=account.bank_bin,
        account_number=account.account_number,
        account_name=account.account_name,
        amount_vnd=tranche.amount_vnd,
        reference_code=tranche.reference_code,
        vietqr_payload=payload,
        hold_expires_at=tranche.hold_expires_at,
        warning=PAYMENT_WARNING,
        qr_code=qr.code if qr else None,
        qr_issued_at=qr.issued_at if qr else None,
        qr_expires_at=qr.expires_at if qr else None,
        pay_deadline_at=cutoffs.pay_deadline_at if cutoffs else None,
        receipt_deadline_at=cutoffs.receipt_deadline_at if cutoffs else None,
        server_time=now,
    )


# --------------------------------------------------------------------------- #
# receipt
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class ReceiptResult:
    transfer_id: UUID
    tranche_status: str
    check_status: str
    failure_codes: list[str]
    confirmation_level: str


def _result(transfer: Transfer, tranche: Tranche) -> ReceiptResult:
    return ReceiptResult(
        transfer_id=transfer.id,
        tranche_status=tranche.status,
        check_status=transfer.check_status,
        failure_codes=list(transfer.failure_codes or []),
        confirmation_level=transfer.confirmation_level,
    )


async def _expire_now(db: AsyncSession, tranche: Tranche, now: dt.datetime) -> None:
    await transition_tranche(db, tranche, "EXPIRED", now=now, reason="HOLD_LAPSED")


async def submit_receipt(
    db: AsyncSession,
    *,
    tranche_id: UUID,
    investor_id: UUID,
    content: bytes,
    content_type: str,
    filename: str | None,
    extractor: ReceiptExtractor,
    storage: EvidenceStorage,
    idempotency_key: str | None,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> ReceiptResult:
    p = resolve(params)
    if idempotency_key:
        prior = (await db.execute(select(Transfer).where(Transfer.idempotency_key == idempotency_key))).scalar_one_or_none()
        if prior is not None:
            tranche = await _investor_tranche(db, prior.tranche_id, investor_id) if prior.tranche_id else None
            if tranche is None or prior.tranche_id != tranche_id:
                raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
            return _result(prior, tranche)

    tranche = await _investor_tranche(db, tranche_id, investor_id, lock=True)
    window = await lock_window(db, tranche.window_id)
    cutoffs = window_cutoffs(window, p)
    if cutoffs and now > cutoffs.receipt_deadline_at:
        raise conflict("RECEIPT_DEADLINE_PASSED", "Receipts for this round could be uploaded until 15:30")

    # A hold that has lapsed but whose job has not yet run is treated as lapsed (lazy expiry): the money may have
    # moved, so the receipt is accepted as a LATE_RECEIPT for staff rather than refused. Not on a round with Day-14
    # cut-offs: there the hold ends at 14:45 only to close signing and new QRs. Receipts are taken until 15:30, the
    # Day-14 timing check below tells on-time money from late money, and the 15:00 / 15:35 step handles lenders who
    # sent nothing (day14-transfer-rule.md rules 5, 6 and 8).
    if (
        cutoffs is None
        and tranche.status in ("RESERVED", "SIGNED")
        and tranche.receipt_submitted_at is None
        and now > tranche.hold_expires_at
    ):
        await _expire_now(db, tranche, now)
    late = tranche.status == "EXPIRED"
    if late and window.status not in ("OPEN", "FULL", "CLOSING", "AWAITING_PAYMENT"):
        raise conflict("TRANCHE_NOT_SIGNED", "The window has closed for this ticket")
    if not late and tranche.status != "SIGNED":
        raise conflict("TRANCHE_NOT_SIGNED", "The ticket is not awaiting a receipt")
    if tranche.receipt_submitted_at is not None and not late:
        raise conflict("RECEIPT_ALREADY_SUBMITTED", "A receipt was already submitted for this ticket")

    try:
        evidence = await store_evidence_file(
            db, storage, owner_user_id=investor_id, purpose="RECEIPT", content=content, content_type=content_type,
            filename=filename, params=p,
        )
    except DirectTransferError as exc:
        if exc.code == "DUPLICATE_FILE":
            await log_event(
                db, entity_type="TRANCHE", entity_id=tranche.id, event_type="DUPLICATE_REJECTED", now=now,
                actor_id=investor_id, actor_role="INVESTOR", payload={"reason": "DUPLICATE_FILE"},
            )
            exc.persist = True
        raise

    transfer = Transfer(
        kind="DISBURSEMENT",
        tranche_id=tranche.id,
        from_account_id=tranche.investor_account_id,
        to_account_id=window.sme_payment_account_id,
        amount_vnd=tranche.amount_vnd,
        reference=tranche.reference_code,
        rail="VIETQR",
        confirmation_level="REPORTED",
        check_status="AWAITING_FIELDS",
        evidence_file_id=evidence.id,
        reported_by=investor_id,
        idempotency_key=idempotency_key,
    )
    db.add(transfer)
    tranche.receipt_submitted_at = now
    await db.flush()
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type="RECEIPT_UPLOADED", now=now, actor_id=investor_id,
        actor_role="INVESTOR", file_sha256=evidence.sha256,
        payload={"tranche_id": str(tranche.id), "file_id": str(evidence.id), "late": late},
    )

    if late:
        # I-8 / PROVISIONAL(Q-D3): money may have moved after the hold lapsed; staff decide (revive only if capacity).
        await transition_tranche(
            db, tranche, "UNDER_REVIEW", now=now, actor_id=investor_id, actor_role="INVESTOR", review_cause="LATE_RECEIPT"
        )
        await open_tranche_dispute(db, tranche=tranche, cause="LATE_RECEIPT", now=now, raised_by=investor_id, params=p)
        return _result(transfer, tranche)

    fields = await extractor.extract(content=content, content_type=content_type)
    if fields is None:
        return _result(transfer, tranche)  # AWAITING_FIELDS: staff manual entry (MVP extractor)
    await apply_receipt_fields(
        db, tranche=tranche, window=window, transfer=transfer, fields=fields, now=now,
        actor_id=investor_id, actor_role="INVESTOR", params=p,
    )
    return _result(transfer, tranche)


async def apply_receipt_fields(
    db: AsyncSession,
    *,
    tranche: Tranche,
    window: FundingWindow,
    transfer: Transfer,
    fields: ReceiptFields,
    now: dt.datetime,
    actor_id: UUID | None,
    actor_role: str,
    params: DirectTransferParams,
    reason: str | None = None,
) -> None:
    """Record the fields, reject duplicates, run the checks, and attest (match) or send to review (mismatch)."""
    # Duplicate bank transaction id: reject (one bank txn funds at most one tranche).
    if fields.bank_txn_id:
        dup = (
            await db.execute(
                select(Transfer.id).where(
                    Transfer.bank_txn_id == fields.bank_txn_id,
                    Transfer.kind.in_(("DISBURSEMENT", "REFUND")),
                    Transfer.id != transfer.id,
                )
            )
        ).first()
        if dup is not None:
            transfer.check_status = "REJECTED"
            transfer.failure_codes = ["DUPLICATE_BANK_TXN"]
            tranche.receipt_submitted_at = None  # the hold clock may apply again; the file cannot be reused
            await log_event(
                db, entity_type="TRANSFER", entity_id=transfer.id, event_type="DUPLICATE_REJECTED", now=now,
                actor_id=actor_id, actor_role=actor_role, reason=reason, payload={"bank_txn_id": fields.bank_txn_id},
            )
            err = conflict("DUPLICATE_BANK_TXN", "This bank transaction was already used")
            err.persist = True
            raise err
        transfer.bank_txn_id = fields.bank_txn_id
    transfer.parsed_fields = fields.to_json()
    transfer.transferred_at = fields.transferred_at

    payee = await db.get(PaymentAccount, window.sme_payment_account_id)
    expected = ExpectedTransfer(
        amount_vnd=tranche.amount_vnd,
        reference_code=tranche.reference_code,
        payee_account_number=payee.account_number,
        payer_kyc_name=await resolve_kyc_name(db, tranche.investor_id),
        reserved_at=tranche.reserved_at,
    )
    failures = check_receipt(
        fields, expected, now=now, future_skew=dt.timedelta(minutes=params.receipt_future_skew_minutes)
    )
    timing = await day14.record_payment_timing(
        db, tranche=tranche, window=window, transfer=transfer, memo=fields.memo, transferred_at=fields.transferred_at,
        params=params,
    )
    late_money = day14.is_late_money(window, timing, params)
    transfer.failure_codes = failures
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type="FIELDS_ENTERED", now=now, actor_id=actor_id,
        actor_role=actor_role, reason=reason, payload={"fields": fields.to_json()},
    )
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type="CHECKS_RUN", now=now, actor_id=actor_id,
        actor_role=actor_role, payload={"failure_codes": failures, "matched": not failures},
    )
    if tranche.status == "UNDER_REVIEW":
        # Late receipt: record the checks only; staff decide the outcome.
        transfer.check_status = "MATCHED" if not failures else "MISMATCH"
        return
    if not failures and late_money:
        # Day-14 late money: the receipt is right but outside its QR's 15 minutes (or after 14:45). It does not join
        # the loan; the 15:00 step turns it into the lender's top-up (day14.process_day14_no_shows).
        transfer.check_status = "MATCHED"
        await transition_tranche(
            db, tranche, "UNDER_REVIEW", now=now, actor_id=actor_id, actor_role=actor_role, review_cause="LATE_MONEY",
            extra={"late_reason": timing.late_reason},
        )
    elif not failures:
        transfer.check_status = "MATCHED"
        transfer.confirmation_level = "ATTESTED"
        await transition_tranche(
            db, tranche, "ATTESTED", now=now, actor_id=actor_id, actor_role=actor_role,
            confirm_deadline=calendar_deadline(now, hours=params.sme_confirm_hours),
        )
    else:
        transfer.check_status = "MISMATCH"
        await transition_tranche(
            db, tranche, "UNDER_REVIEW", now=now, actor_id=actor_id, actor_role=actor_role, review_cause="RECEIPT_MISMATCH",
            extra={"failure_codes": failures},
        )
        await open_tranche_dispute(db, tranche=tranche, cause="RECEIPT_MISMATCH", now=now, params=params)


async def staff_enter_receipt_fields(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    fields: ReceiptFields,
    reason: str,
    staff_id: UUID,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> ReceiptResult:
    """Staff manual entry of the parsed fields (the MVP extractor). Runs the same checks as an automatic extractor."""
    p = resolve(params)
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    transfer = (await db.execute(select(Transfer).where(Transfer.id == transfer_id).with_for_update())).scalar_one_or_none()
    if transfer is None or transfer.kind not in ("DISBURSEMENT", "REFUND"):
        raise not_found("Transfer not found")
    if transfer.check_status not in ("AWAITING_FIELDS", "DUE"):
        raise bad_request("The fields of this transfer were already entered", "FIELDS_ALREADY_ENTERED")
    tranche = (await db.execute(select(Tranche).where(Tranche.id == transfer.tranche_id).with_for_update())).scalar_one()
    window = await lock_window(db, tranche.window_id)
    if transfer.kind == "REFUND":
        await _apply_refund_fields(db, tranche=tranche, window=window, transfer=transfer, fields=fields, now=now, actor_id=staff_id, reason=reason)
        return _result(transfer, tranche)
    await apply_receipt_fields(
        db, tranche=tranche, window=window, transfer=transfer, fields=fields, now=now, actor_id=staff_id,
        actor_role="ADMIN", params=p, reason=reason,
    )
    return _result(transfer, tranche)


async def _apply_refund_fields(
    db: AsyncSession, *, tranche: Tranche, window: FundingWindow, transfer: Transfer, fields: ReceiptFields,
    now: dt.datetime, actor_id: UUID | None, reason: str | None,
) -> None:
    """Refund receipt (SME pays the investor back): payer = SME's registered account, payee = the investor's account."""
    payer = await db.get(PaymentAccount, window.sme_payment_account_id)
    payee = await db.get(PaymentAccount, tranche.investor_account_id)
    if fields.bank_txn_id:
        dup = (
            await db.execute(
                select(Transfer.id).where(
                    Transfer.bank_txn_id == fields.bank_txn_id, Transfer.kind.in_(("DISBURSEMENT", "REFUND")), Transfer.id != transfer.id
                )
            )
        ).first()
        if dup is not None:
            err = conflict("DUPLICATE_BANK_TXN", "This bank transaction was already used")
            err.persist = True
            raise err
        transfer.bank_txn_id = fields.bank_txn_id
    expected = ExpectedTransfer(
        amount_vnd=tranche.amount_vnd, reference_code=tranche.reference_code, payee_account_number=payee.account_number,
        payer_kyc_name=payer.account_name, reserved_at=transfer.created_at or now,
    )
    failures = check_receipt(fields, expected, now=now)
    transfer.parsed_fields = fields.to_json()
    transfer.transferred_at = fields.transferred_at
    transfer.failure_codes = failures
    transfer.check_status = "MATCHED" if not failures else "MISMATCH"
    if not failures:
        transfer.confirmation_level = "ATTESTED"
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type="CHECKS_RUN", now=now, actor_id=actor_id,
        reason=reason, payload={"kind": "REFUND", "failure_codes": failures, "matched": not failures},
    )


async def submit_refund_receipt(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    sme_user_id: UUID,
    content: bytes,
    content_type: str,
    filename: str | None,
    extractor: ReceiptExtractor,
    storage: EvidenceStorage,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Transfer:
    """The SME uploads the receipt of a refund it paid; same file/hash rules as any receipt."""
    p = resolve(params)
    transfer = (await db.execute(select(Transfer).where(Transfer.id == transfer_id, Transfer.kind == "REFUND").with_for_update())).scalar_one_or_none()
    if transfer is None:
        raise not_found("Refund not found")
    tranche = await db.get(Tranche, transfer.tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)
    if window.sme_user_id != sme_user_id:
        raise not_found("Refund not found")
    if transfer.evidence_file_id is not None:
        raise conflict("RECEIPT_ALREADY_SUBMITTED", "A receipt was already submitted for this refund")
    evidence = await store_evidence_file(
        db, storage, owner_user_id=sme_user_id, purpose="RECEIPT", content=content, content_type=content_type,
        filename=filename, params=p,
    )
    transfer.evidence_file_id = evidence.id
    transfer.reported_by = sme_user_id
    transfer.check_status = "AWAITING_FIELDS"
    await db.flush()
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type="RECEIPT_UPLOADED", now=now, actor_id=sme_user_id,
        actor_role="SME", file_sha256=evidence.sha256, payload={"kind": "REFUND", "file_id": str(evidence.id)},
    )
    fields = await extractor.extract(content=content, content_type=content_type)
    if fields is not None:
        await _apply_refund_fields(db, tranche=tranche, window=window, transfer=transfer, fields=fields, now=now, actor_id=sme_user_id, reason=None)
    return transfer


# --------------------------------------------------------------------------- #
# SME confirm / dispute, lapses
# --------------------------------------------------------------------------- #
async def sme_confirm_tranche(
    db: AsyncSession, *, tranche_id: UUID, sme_user_id: UUID, now: dt.datetime, params: DirectTransferParams | None = None
) -> Tranche:
    tranche, window = await _sme_tranche(db, tranche_id, sme_user_id, lock=True)
    transfer = (
        await db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id, Transfer.kind == "DISBURSEMENT"))
    ).scalars().first()
    await transition_tranche(db, tranche, "CONFIRMED", now=now, actor_id=sme_user_id, actor_role="SME")
    if transfer is not None:
        transfer.confirmation_level = "CONFIRMED"
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="SME_CONFIRMED", now=now, actor_id=sme_user_id, actor_role="SME"
    )
    await _void_if_excluded(db, tranche, window, now=now, params=resolve(params))
    return tranche


async def sme_dispute_tranche(
    db: AsyncSession,
    *,
    tranche_id: UUID,
    sme_user_id: UUID,
    statement: str | None,
    file_id: UUID | None,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Tranche:
    p = resolve(params)
    tranche, window = await _sme_tranche(db, tranche_id, sme_user_id, lock=True)
    if tranche.status != "ATTESTED" and not (tranche.status == "SIGNED" and tranche.receipt_submitted_at is not None):
        raise DirectTransferError(400, f"Invalid state transition: {tranche.status} → UNDER_REVIEW", "INVALID_TRANSITION")
    if tranche.confirm_deadline is not None and now >= tranche.confirm_deadline:
        raise conflict("DISPUTE_WINDOW_CLOSED", "The dispute window has closed")
    if not (statement and statement.strip()) and file_id is None:
        raise bad_request("A statement or a file is required", "EVIDENCE_REQUIRED")
    await transition_tranche(
        db, tranche, "UNDER_REVIEW", now=now, actor_id=sme_user_id, actor_role="SME", review_cause="SME_DISPUTE"
    )
    await open_tranche_dispute(
        db, tranche=tranche, cause="SME_DISPUTE", now=now, raised_by=sme_user_id, claim_statement=statement,
        claim_file_id=file_id, params=p,
    )
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="SME_DISPUTED", now=now, actor_id=sme_user_id, actor_role="SME"
    )
    return tranche


BANK_TXN_MISMATCH = "BANK_TXN_MISMATCH"


async def sme_receipt_check(
    db: AsyncSession,
    *,
    tranche_id: UUID,
    sme_user_id: UUID,
    content: bytes,
    content_type: str,
    filename: str | None,
    fields: ReceiptFields | None,
    storage: EvidenceStorage,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Tranche:
    """The SME's own screenshot of the money arriving, within its 24 hours (spec 4.6, rule 8). Amount, reference and
    bank transaction id equal to the lender's: CONFIRMED. A difference: UNDER_REVIEW with a RECEIPT_MISMATCH dispute.
    `fields` is what the system read off the screenshot; None (OCR off or unsure) confirms on the SME's word, the
    same as pressing Received."""
    p = resolve(params)
    tranche, _ = await _sme_tranche(db, tranche_id, sme_user_id, lock=True)
    if tranche.status != "ATTESTED":
        raise DirectTransferError(400, f"Invalid state transition: {tranche.status} → CONFIRMED", "INVALID_TRANSITION")
    if now >= tranche.confirm_deadline:
        raise conflict("DISPUTE_WINDOW_CLOSED", "The 24 hours have passed: silence counted as received")
    evidence = await store_evidence_file(
        db, storage, owner_user_id=sme_user_id, purpose="RECEIPT", content=content, content_type=content_type,
        filename=filename, params=p,
    )
    failures: list[str] = []
    if fields is not None:
        lender = (
            await db.execute(
                select(Transfer).where(
                    Transfer.tranche_id == tranche.id, Transfer.kind == "DISBURSEMENT", Transfer.check_status == "MATCHED"
                ).order_by(Transfer.created_at.desc())
            )
        ).scalars().first()
        if fields.amount_vnd != tranche.amount_vnd:
            failures.append(AMOUNT_MISMATCH)
        if _normalize_memo(tranche.reference_code) not in _normalize_memo(fields.memo or ""):
            failures.append(REFERENCE_MISSING)
        if not (lender and lender.bank_txn_id and (fields.bank_txn_id or "").strip().upper() == lender.bank_txn_id.strip().upper()):
            failures.append(BANK_TXN_MISMATCH)
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="SME_RECEIPT_UPLOADED", now=now,
        actor_id=sme_user_id, actor_role="SME", file_sha256=evidence.sha256,
        payload={"file_id": str(evidence.id), "fields": fields.to_json() if fields else None, "failure_codes": failures},
    )
    if not failures:
        return await sme_confirm_tranche(db, tranche_id=tranche.id, sme_user_id=sme_user_id, now=now, params=p)
    await transition_tranche(
        db, tranche, "UNDER_REVIEW", now=now, actor_id=sme_user_id, actor_role="SME", review_cause="RECEIPT_MISMATCH",
        extra={"failure_codes": failures},
    )
    await open_tranche_dispute(
        db, tranche=tranche, cause="RECEIPT_MISMATCH", now=now, raised_by=sme_user_id, claim_file_id=evidence.id,
        params=p,
    )
    return tranche


async def sme_dispute_after_confirmation(
    db: AsyncSession,
    *,
    tranche_id: UUID,
    sme_user_id: UUID,
    statement: str,
    content: bytes,
    content_type: str,
    filename: str | None,
    storage: EvidenceStorage,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> Dispute:
    """A confirmation by silence stays open to dispute for 30 days, with a bank statement (spec 4.6, rule 8, A5).
    The ticket keeps its state (it may already be in the loan); staff decide the dispute."""
    p = resolve(params)
    tranche, _ = await _sme_tranche(db, tranche_id, sme_user_id, lock=True)
    if tranche.dispute_until is None or now >= tranche.dispute_until:
        raise conflict("DISPUTE_WINDOW_CLOSED", "The dispute window has closed")
    if not (statement and statement.strip()):
        raise bad_request("Say what is wrong", "EVIDENCE_REQUIRED")
    statement_file = await store_evidence_file(
        db, storage, owner_user_id=sme_user_id, purpose="BANK_STATEMENT", content=content, content_type=content_type,
        filename=filename, params=p,
    )
    dispute = await open_tranche_dispute(
        db, tranche=tranche, cause="SME_DISPUTE", now=now, raised_by=sme_user_id, claim_statement=statement.strip(),
        claim_file_id=statement_file.id, params=p,
    )
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type="SME_DISPUTED_AFTER_CONFIRMATION", now=now,
        actor_id=sme_user_id, actor_role="SME", file_sha256=statement_file.sha256,
        payload={"dispute_id": str(dispute.id), "dispute_until": tranche.dispute_until.isoformat()},
    )
    return dispute


async def _void_if_excluded(
    db: AsyncSession, tranche: Tranche, window: FundingWindow, *, now: dt.datetime, params: DirectTransferParams
) -> None:
    """A tranche confirmed after the loan formed (or flagged excluded at close) never joins it: PROVISIONAL(Q-D7).
    It is voided and the SME refunds it (tracked)."""
    if tranche.excluded_at_close or window.status in ("REPAYING", "FAILED", "REPAID", "CANCELLED"):
        await transition_tranche(db, tranche, "VOID", now=now, reason="CONFIRMED_AFTER_CLOSE", void_reason="EXCLUDED_AT_CLOSE")
        await create_refund_transfer(db, tranche=tranche, window=window, now=now, reason="EXCLUDED_AT_CLOSE", params=params)


async def confirm_lapsed_tranches(
    db: AsyncSession, *, now: dt.datetime, window_id: UUID | None = None, params: DirectTransferParams | None = None
) -> int:
    """Silence counts as received: ATTESTED tranches past their dispute deadline become CONFIRMED."""
    p = resolve(params)
    stmt = select(Tranche).where(
        Tranche.status == "ATTESTED", Tranche.confirm_deadline.is_not(None), Tranche.confirm_deadline <= now
    )
    if window_id is not None:
        stmt = stmt.where(Tranche.window_id == window_id)
    rows = (await db.execute(stmt.with_for_update(skip_locked=True))).scalars().all()
    for tranche in rows:
        window = await db.get(FundingWindow, tranche.window_id)
        transfer = (
            await db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id, Transfer.kind == "DISBURSEMENT"))
        ).scalars().first()
        tranche.dispute_until = calendar_deadline(now, days=p.confirmed_dispute_days)
        await transition_tranche(db, tranche, "CONFIRMED", now=now, reason="DISPUTE_WINDOW_LAPSED")
        if transfer is not None:
            transfer.confirmation_level = "CONFIRMED"
        await _void_if_excluded(db, tranche, window, now=now, params=p)
    return len(rows)


async def expire_holds(db: AsyncSession, *, now: dt.datetime) -> int:
    """RESERVED and SIGNED tranches whose hold has lapsed and that have no submitted receipt -> EXPIRED. Not on
    windows with Day-14 cut-offs, whose unpaid tickets are the Day-14 step's (see `submit_receipt`)."""
    candidates = (
        await db.execute(
            select(Tranche.window_id)
            .join(FundingWindow, FundingWindow.id == Tranche.window_id)
            .where(
                Tranche.status.in_(("RESERVED", "SIGNED")), Tranche.receipt_submitted_at.is_(None), Tranche.hold_expires_at <= now,
                FundingWindow.pay_deadline_at.is_(None),
            )
            .distinct()
        )
    ).scalars().all()
    expired = 0
    for window_id in candidates:
        window = await lock_window(db, window_id)
        rows = (
            await db.execute(
                select(Tranche).where(
                    Tranche.window_id == window_id,
                    Tranche.status.in_(("RESERVED", "SIGNED")),
                    Tranche.receipt_submitted_at.is_(None),
                    Tranche.hold_expires_at <= now,
                )
            )
        ).scalars().all()
        for tranche in rows:
            await transition_tranche(db, tranche, "EXPIRED", now=now, reason="HOLD_LAPSED")
            expired += 1
        await refresh_window_fullness(db, window, now)
    return expired


async def list_window_tranches(db: AsyncSession, *, window_id: UUID, sme_user_id: UUID) -> list[dict]:
    window = await db.get(FundingWindow, window_id)
    if window is None or window.sme_user_id != sme_user_id:
        raise not_found("Window not found")
    tranches = (await db.execute(select(Tranche).where(Tranche.window_id == window_id).order_by(Tranche.reserved_at))).scalars().all()
    out = []
    for t in tranches:
        investor = await db.get(User, t.investor_id)
        out.append(
            {
                "id": t.id,
                "amount_vnd": t.amount_vnd,
                "status": t.status,
                "reference_code": t.reference_code,
                "investor_display": mask_name(investor.full_name or investor.email),
                "receipt_submitted_at": t.receipt_submitted_at,
                "attested_at": t.attested_at,
                "confirm_deadline": t.confirm_deadline,
                "review_cause": t.review_cause,
                "dispute_until": t.dispute_until,
            }
        )
    return out


def mask_name(name: str) -> str:
    """'Nguyen Van A' -> 'N. A***' style display: initial plus the masked last token."""
    parts = [x for x in name.split() if x]
    if not parts:
        return "***"
    if len(parts) == 1:
        return parts[0][0] + "***"
    return f"{parts[0][0]}. {parts[-1][0]}***"


async def list_investor_tranches(db: AsyncSession, *, investor_id: UUID) -> list[Tranche]:
    return list((await db.execute(select(Tranche).where(Tranche.investor_id == investor_id).order_by(Tranche.reserved_at.desc()))).scalars())
