"""The system's own check of a business's repayment receipt (docs/specs/direct_transfer/receipt-ocr.md section 10).

The business pays an investor directly and uploads its bank receipt (app/market/repayment_router.py
`pay_with_receipt`). With RECEIPT_OCR_ENABLED the receipt is read on our own server, the same way investors' receipts
are (app/uploads/receipt_ocr.py), and compared with what the business was asked to pay:

  * the amount: the days' net total, exactly (the VietQR's amount);
  * the memo carries the investment's reference code;
  * the receiver: the investor's account number, and its bank and holder name against the ones the bank showed when
    the account was verified (D-031, `receipt_auto.check_receiver`);
  * the payer: the holder name against the business's verified account, read the same forgiving way (banks shorten
    company names: "CTY CP" = "CÔNG TY CỔ PHẦN");
  * the time: not before the investment was confirmed, not in the future;
  * the bank transaction is not one another repayment receipt (or an investment) already used.

What it decides (Phat, 2026-11-14):

  * ACCEPTED: everything matches and the receiver and payer are confirmed. Nothing moves: the investor still
    confirms the money reached their account, and sees the system's check while deciding.
  * DENIED: a confident read that fails a check. With RECEIPT_AUTO_VERDICT_ENABLED the upload is refused: nothing is
    stored or marked paid, the days stay DUE, and the business is told what did not match. Automatic decisions off:
    the receipt goes through as before and the mismatch is shown to the investor and staff.
  * NEEDS_REVIEW: anything else (an unsure read, a receiver or payer the receipt does not show or that was never
    verified). The receipt goes through as before; the investor decides.

An unsure read can never refuse a receipt. Everything is appended to the evidence log with a NULL actor and role
SYSTEM: against the receipt (entity EVIDENCE_FILE) when it was kept, against the investment (entity TRANCHE, with
the file's SHA-256) when it was refused. Only `app/direct_transfer/`'s public functions are used.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import FundingWindow, PaymentAccount, RepaymentDay, Tranche, Transfer
from app.direct_transfer.receipts import PAYER_NAME_MISMATCH, ExpectedTransfer, check_receipt
from app.market.receipt_auto import (
    ACCEPTED,
    AUTO_VERDICT_OFF,
    DENIED,
    DUPLICATE_BANK_TXN,
    EVENT_OCR_READ,
    EVENT_SYSTEM_VERDICT,
    NEEDS_REVIEW,
    SYSTEM_ROLE,
    bank_txn_used_elsewhere,
    check_receiver,
    receiver_names_match,
    verified_holder_name,
)
from app.uploads.receipt_ocr import ReceiptReading
from app.utils.audit import append_audit

# Why a repayment receipt was left to the investor (besides the reading's own notes and the receiver's).
PAYER_NAME_UNVERIFIED = "PAYER_NAME_UNVERIFIED"  # the business's account has no holder name verified at the bank
PAYEE_ACCOUNT_MISSING = "PAYEE_ACCOUNT_MISSING"  # the investment has no investor account to compare with

# The X-Error-Code of a refused upload.
RECEIPT_MISMATCH = "RECEIPT_MISMATCH"

# Audit-log actions (entity TRANCHE, no actor), so the Transactions tab's activity log shows the system's checks.
AUDIT_ACTIONS = {
    ACCEPTED: "REPAYMENT_RECEIPT_SYSTEM_ACCEPTED",
    DENIED: "REPAYMENT_RECEIPT_SYSTEM_DENIED",
    NEEDS_REVIEW: "REPAYMENT_RECEIPT_SYSTEM_NEEDS_REVIEW",
}

ENTITY_RECEIPT = "EVIDENCE_FILE"


@dataclasses.dataclass(frozen=True)
class RepaymentCheck:
    verdict: str  # ACCEPTED | DENIED | NEEDS_REVIEW
    refused: bool  # DENIED with automatic decisions on: the upload is refused
    suggested: str | None  # what a confident read would decide (ACCEPTED/DENIED), whether or not it is acted on
    failure_codes: list[str]
    review_reasons: list[str]
    receiver: dict[str, Any] | None
    payer: dict[str, Any] | None
    summary: str
    reading: ReceiptReading

    def payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "applied": self.refused,
            "suggested": self.suggested,
            "failure_codes": self.failure_codes,
            "review_reasons": self.review_reasons,
            "receiver": self.receiver,
            "payer": self.payer,
            "engine": self.reading.engine,
            "confidence": self.reading.confidence,
        }


def _summary(verdict: str, failures: list[str], reasons: list[str], suggested: str | None, confident: bool) -> str:
    if verdict == ACCEPTED:
        return "Read automatically: it matches the repayment asked for, to the investor's verified account"
    if verdict == DENIED:
        return "Read automatically: it does not match (" + ", ".join(failures) + "); the upload was refused"
    left = "; left for the investor (" + ", ".join(reasons) + ")"
    if suggested == DENIED:
        return "Read automatically: it does not match (" + ", ".join(failures) + ")" + left
    if confident:
        return "Read automatically, but the receiver or payer could not be confirmed" + left
    return "The receipt could not be read with confidence" + left


async def _txn_used_by_another_repayment(db: AsyncSession, bank_txn_id: str | None) -> bool:
    """The bank transaction is already on a repayment receipt that was kept (refused ones claim nothing)."""
    if not bank_txn_id:
        return False
    row = (
        await db.execute(
            select(Transfer.id).where(
                Transfer.kind == "REPAYMENT",
                Transfer.check_status != "REJECTED",
                Transfer.parsed_fields["bank_txn_id"].astext == bank_txn_id,
            ).limit(1)
        )
    ).first()
    return row is not None


async def _check_payer(db: AsyncSession, payer_name: str | None, account: PaymentAccount | None) -> tuple[list[str], list[str], dict[str, Any]]:
    """(failures, unknowns, what was compared) for the name the money came from."""
    holder = await verified_holder_name(db, account.id) if account is not None else None
    info = {
        "holder_read": payer_name,
        "expected_holder": holder,
        "expected_account": account.account_number if account is not None else None,
    }
    if not payer_name:
        return [], [], info  # a missing payer name is check_receipt's MISSING_FIELD
    if not holder:
        return [], [PAYER_NAME_UNVERIFIED], info
    if not receiver_names_match(payer_name, holder):
        return [PAYER_NAME_MISMATCH], [], info
    return [], [], info


async def check_repayment_receipt(
    db: AsyncSession,
    *,
    tranche: Tranche,
    window: FundingWindow,
    days: Sequence[RepaymentDay],
    reading: ReceiptReading,
    auto_verdict: bool,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> RepaymentCheck:
    """Compare a reading with what the business was asked to pay for these days. Reads only: the caller records
    the result (`record_kept` or `record_refused`)."""
    p = resolve(params)
    fields = reading.fields
    confident = fields is not None and reading.confident
    failures: list[str] = []
    unknowns: list[str] = []
    receiver: dict[str, Any] | None = None
    payer: dict[str, Any] | None = None
    payee = await db.get(PaymentAccount, tranche.investor_account_id) if tranche.investor_account_id else None
    if confident and payee is None:
        unknowns.append(PAYEE_ACCOUNT_MISSING)
    elif confident:
        assert fields is not None
        payer_account = (
            await db.get(PaymentAccount, window.sme_payment_account_id) if window.sme_payment_account_id else None
        )
        expected = ExpectedTransfer(
            amount_vnd=sum(int(day.net_vnd) for day in days),
            reference_code=tranche.reference_code,
            payee_account_number=payee.account_number,
            payer_kyc_name="",  # compared below, the forgiving way
            # A repayment cannot predate the investment it repays.
            reserved_at=tranche.confirmed_at or tranche.reserved_at,
        )
        failures = [
            code
            for code in check_receipt(
                fields, expected, now=now, future_skew=dt.timedelta(minutes=p.receipt_future_skew_minutes)
            )
            if code != PAYER_NAME_MISMATCH
        ]
        payer_failures, payer_unknowns, payer = await _check_payer(db, fields.payer_name, payer_account)
        failures.extend(payer_failures)
        unknowns.extend(payer_unknowns)
        # bank_txn_used_elsewhere looks at investments' transfers; none of them is this receipt's to exclude.
        if await _txn_used_by_another_repayment(db, fields.bank_txn_id) or await bank_txn_used_elsewhere(
            db, fields.bank_txn_id, uuid.UUID(int=0)
        ):
            failures.append(DUPLICATE_BANK_TXN)
        receiver_failures, receiver_unknowns, receiver = await check_receiver(db, reading, payee)
        failures.extend(receiver_failures)
        unknowns.extend(receiver_unknowns)

    # A wrong detail decides it (DENIED) even when another detail is missing; an all-correct read is ACCEPTED only
    # when nothing about the receiver or payer is unknown.
    suggested = (DENIED if failures else (ACCEPTED if not unknowns else None)) if confident else None
    reasons: list[str] = []
    if not reading.confident:
        reasons.extend(reading.notes)
    if not failures:
        reasons.extend(unknowns)
    # Accepting moves nothing, so only a refusal needs automatic decisions switched on.
    if suggested == DENIED and not auto_verdict:
        reasons.append(AUTO_VERDICT_OFF)
    verdict = suggested if suggested is not None and not reasons else NEEDS_REVIEW
    return RepaymentCheck(
        verdict=verdict,
        refused=verdict == DENIED,
        suggested=suggested,
        failure_codes=failures,
        review_reasons=reasons,
        receiver=receiver,
        payer=payer,
        summary=_summary(verdict, failures, reasons, suggested, confident),
        reading=reading,
    )


async def record_kept(
    db: AsyncSession,
    check: RepaymentCheck,
    *,
    receipt_id: UUID,
    tranche: Tranche,
    days: Sequence[RepaymentDay],
    now: dt.datetime,
) -> None:
    """The receipt was kept and the days marked paid: the reading and the check against the receipt, and what was
    read on each of its transfers (a confident read only), for the investor and staff to see. The transfers keep
    waiting for the investor (AWAITING_FIELDS)."""
    await log_event(
        db, entity_type=ENTITY_RECEIPT, entity_id=receipt_id, event_type=EVENT_OCR_READ, now=now,
        actor_role=SYSTEM_ROLE, payload=check.reading.to_json(),
    )
    await log_event(
        db, entity_type=ENTITY_RECEIPT, entity_id=receipt_id, event_type=EVENT_SYSTEM_VERDICT, now=now,
        actor_role=SYSTEM_ROLE, reason=check.summary, payload=check.payload(),
    )
    fields = check.reading.fields
    if fields is not None and check.reading.confident:
        transfers = (
            await db.execute(
                select(Transfer).where(Transfer.kind == "REPAYMENT", Transfer.evidence_file_id == receipt_id)
            )
        ).scalars().all()
        for transfer in transfers:
            transfer.parsed_fields = fields.to_json() | {"payee_bank": check.reading.payee_bank}
            transfer.transferred_at = fields.transferred_at
    _audit(db, check, tranche=tranche, days=days, receipt_id=receipt_id)
    await db.flush()


async def record_refused(
    db: AsyncSession,
    check: RepaymentCheck,
    *,
    content: bytes,
    tranche: Tranche,
    days: Sequence[RepaymentDay],
    now: dt.datetime,
) -> None:
    """The upload was refused: nothing was stored or marked. The reading and the check go against the investment,
    with the file's SHA-256, so staff can see what was tried."""
    digest = hashlib.sha256(content).hexdigest()
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type=EVENT_OCR_READ, now=now,
        actor_role=SYSTEM_ROLE, payload=check.reading.to_json(), file_sha256=digest,
    )
    await log_event(
        db, entity_type="TRANCHE", entity_id=tranche.id, event_type=EVENT_SYSTEM_VERDICT, now=now,
        actor_role=SYSTEM_ROLE, reason=check.summary,
        payload=check.payload() | {"repayment_day_ids": [str(day.id) for day in days]}, file_sha256=digest,
    )
    _audit(db, check, tranche=tranche, days=days, receipt_id=None, sha256=digest)
    await db.flush()


def _audit(
    db: AsyncSession,
    check: RepaymentCheck,
    *,
    tranche: Tranche,
    days: Sequence[RepaymentDay],
    receipt_id: UUID | None,
    sha256: str | None = None,
) -> None:
    after: dict[str, Any] = {
        "repayment_day_ids": [str(day.id) for day in days],
        "net_vnd": sum(int(day.net_vnd) for day in days),
        "failure_codes": check.failure_codes,
        "review_reasons": check.review_reasons,
        "reason": check.summary,
        "refused": check.refused,
    }
    if receipt_id is not None:
        after["receipt_id"] = str(receipt_id)
    if sha256 is not None:
        after["file_sha256"] = sha256
    append_audit(
        db, entity_type="TRANCHE", entity_id=tranche.id, action=AUDIT_ACTIONS[check.verdict], actor_id=None,
        after_state=after,
    )
