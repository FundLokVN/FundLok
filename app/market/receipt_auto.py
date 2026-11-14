"""The system's own check of an investor's receipt: the OCR reading, the receipt checks, and the automatic verdict
(docs/specs/direct_transfer/receipt-ocr.md).

  * ACCEPTED: a confident read that passes every check (check_receipt: amount, reference in the memo, the SME's
    account number, payer name = KYC name, time) and whose receiver is confirmed in full: the receiving bank and the
    account holder's name are the SME's verified account's too (`check_receiver`). The ticket is CONFIRMED at once (it counts toward the round and the
    business is told), but its loan does not form: that waits for staff to approve it in review, or for the round
    to be advanced or closed. Until then staff can still deny it (CONFIRMED -> VOID).
  * DENIED: a confident read that fails a check, including a receiver that is shown and wrong (another account
    number, another bank, another holder). The receipt is rejected the way staff reject one: the ticket waits
    for a receipt again, so the investor can upload the right one while the hold lasts. Staff can still approve it.
  * NEEDS_REVIEW: anything else (unsure or failed read, a receiving bank or holder name the receipt does not show
    or that cannot be identified, a late receipt, automatic decisions switched off). The receipt waits for staff
    exactly as it did before OCR existed.

Everything is appended to the evidence log against the transfer, with a NULL actor and role SYSTEM: the reading
(RECEIPT_OCR_READ), the fields and checks when it decided (FIELDS_ENTERED, CHECKS_RUN), and the verdict
(SYSTEM_VERDICT). Staff's later review is SYSTEM_VERDICT_REVIEWED. There is no schema change: the admin screens read
the verdict back from the log.

Only `app/direct_transfer/`'s public functions are used (check_receipt, transition_tranche, log_event,
resolve_kyc_name); its rules are unchanged.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.banks import Bank, list_banks
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import EvidenceLog, FundingWindow, PaymentAccount, Tranche, Transfer
from app.direct_transfer.receipts import ExpectedTransfer, ReceiptFields, check_receipt, normalize_name
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.tranches import resolve_kyc_name
from app.uploads.parsers.bank_receipt import fold
from app.uploads.receipt_ocr import ReceiptReading
from app.utils.audit import append_audit

SYSTEM_ROLE = "SYSTEM"

EVENT_OCR_READ = "RECEIPT_OCR_READ"
EVENT_SYSTEM_VERDICT = "SYSTEM_VERDICT"
EVENT_SYSTEM_VERDICT_REVIEWED = "SYSTEM_VERDICT_REVIEWED"

ACCEPTED = "ACCEPTED"
DENIED = "DENIED"
NEEDS_REVIEW = "NEEDS_REVIEW"

# Why a receipt was left for staff (besides the reading's own notes).
LATE_RECEIPT = "LATE_RECEIPT"
AUTO_VERDICT_OFF = "AUTO_VERDICT_OFF"
NOT_AWAITING = "NOT_AWAITING"

DUPLICATE_BANK_TXN = "DUPLICATE_BANK_TXN"

# The receiver's banking details against the SME's verified account (the account number itself is check_receipt's
# PAYEE_MISMATCH). Shown and wrong denies the receipt; not shown, or not identifiable, leaves it for staff, so an
# ACCEPTED receipt always had its receiving bank, account number and holder name confirmed.
PAYEE_BANK_MISMATCH = "PAYEE_BANK_MISMATCH"
PAYEE_NAME_MISMATCH = "PAYEE_NAME_MISMATCH"
PAYEE_BANK_NOT_SHOWN = "PAYEE_BANK_NOT_SHOWN"
PAYEE_BANK_UNRECOGNIZED = "PAYEE_BANK_UNRECOGNIZED"
PAYEE_NAME_NOT_SHOWN = "PAYEE_NAME_NOT_SHOWN"
PAYEE_NAME_UNVERIFIED = "PAYEE_NAME_UNVERIFIED"  # the SME's account has no holder name verified at the bank

# Audit-log actions, so the Transactions tab's activity log shows the system's decisions with staff's.
AUDIT_ACTIONS = {
    ACCEPTED: "TRANSFER_SYSTEM_ACCEPTED",
    DENIED: "TRANSFER_SYSTEM_DENIED",
    NEEDS_REVIEW: "TRANSFER_SYSTEM_NEEDS_REVIEW",
}


@dataclasses.dataclass(frozen=True)
class SystemCheck:
    verdict: str  # ACCEPTED | DENIED | NEEDS_REVIEW
    applied: bool  # False: only a suggestion (NEEDS_REVIEW, or automatic decisions off)
    suggested: str | None  # what a confident read would decide (ACCEPTED/DENIED), applied or not
    failure_codes: list[str]
    review_reasons: list[str]
    receiver: dict[str, Any] | None  # the receiver as read and as expected (see check_receiver)


# --------------------------------------------------------------------------- #
# the receiver
# --------------------------------------------------------------------------- #
def identify_bank(text: str | None) -> Bank | None:
    """The bank a receipt names ("Techcombank", "TCB", "Ngân hàng TMCP Kỹ thương Việt Nam", "MB Bank"), or None
    when it names none of the NAPAS list or two equally well."""
    if not text:
        return None
    folded = fold(text)
    squashed = folded.replace(" ", "")
    scores: dict[str, tuple[int, Bank]] = {}
    for bank in list_banks(include_wallets=True):
        score = 0
        for name in (bank.name, bank.short_name):
            candidate = fold(name).replace(" ", "")
            if len(candidate) >= 4 and candidate in squashed:
                score = max(score, len(candidate))
        code = fold(bank.code)
        if code and re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", folded):
            score = max(score, len(code.replace(" ", "")))
        if score:
            scores[bank.bin] = (score, bank)
    if not scores:
        return None
    ranked = sorted(scores.values(), key=lambda pair: pair[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


# Long forms banks shorten in a holder's name (company types and common words), folded.
_ABBREVIATIONS = (
    ("TRACH NHIEM HUU HAN", "TNHH"), ("MOT THANH VIEN", "MTV"), ("HAI THANH VIEN", "HTV"),
    ("DOANH NGHIEP TU NHAN", "DNTN"), ("CONG TY", "CTY"), ("CO PHAN", "CP"), ("THUONG MAI", "TM"),
    ("DICH VU", "DV"), ("SAN XUAT", "SX"), ("XUAT NHAP KHAU", "XNK"), ("XAY DUNG", "XD"), ("CONG NGHE", "CN"),
    ("PHAT TRIEN", "PT"), ("DAU TU", "DT"), ("VIET NAM", "VN"),
)
_TRUNCATION_MIN = 18  # a shorter name that is only a prefix of the other is not taken as a bank's truncation


def _company_key(name: str) -> str:
    key = f" {normalize_name(name)} "
    for long, short in _ABBREVIATIONS:
        key = key.replace(f" {long} ", f" {short} ")
    return key.strip()


def receiver_names_match(read: str, holder: str) -> bool:
    """The holder name on a receipt against the name the bank showed staff when they verified the account: equal
    once accents, case and punctuation are ignored and the usual abbreviations are expanded alike ("CTY CP" =
    "CÔNG TY CỔ PHẦN"), or a bank's truncation of it (a long prefix)."""
    for a, b in ((normalize_name(read), normalize_name(holder)), (_company_key(read), _company_key(holder))):
        if a == b:
            return True
        short, long = sorted((a, b), key=len)
        if len(short) >= _TRUNCATION_MIN and long.startswith(short):
            return True
    return False


async def verified_holder_name(db: AsyncSession, account_id: UUID) -> str | None:
    """The holder name the bank showed for this account when it was verified (staff lookup or bank lookup, D-031),
    from the evidence log; None if it was never verified that way."""
    rows = (
        await db.execute(
            select(EvidenceLog.payload)
            .where(EvidenceLog.entity_type == "PAYMENT_ACCOUNT", EvidenceLog.entity_id == account_id)
            .order_by(EvidenceLog.occurred_at.desc(), EvidenceLog.created_at.desc())
        )
    ).scalars().all()
    return next((p["verified_holder_name"] for p in rows if (p or {}).get("verified_holder_name")), None)


async def check_receiver(
    db: AsyncSession, reading: ReceiptReading, payee: PaymentAccount
) -> tuple[list[str], list[str], dict[str, Any]]:
    """(failures, unknowns, what was compared) for the receiving bank and holder name. The account number is
    check_receipt's (PAYEE_MISMATCH)."""
    failures: list[str] = []
    unknowns: list[str] = []
    bank = identify_bank(reading.payee_bank)
    holder = await verified_holder_name(db, payee.id)
    read_name = reading.fields.payee_name if reading.fields else None
    if reading.payee_bank is None:
        unknowns.append(PAYEE_BANK_NOT_SHOWN)
    elif bank is None:
        unknowns.append(PAYEE_BANK_UNRECOGNIZED)
    elif bank.bin != payee.bank_bin:
        failures.append(PAYEE_BANK_MISMATCH)
    if not read_name:
        unknowns.append(PAYEE_NAME_NOT_SHOWN)
    elif not holder:
        unknowns.append(PAYEE_NAME_UNVERIFIED)
    elif not receiver_names_match(read_name, holder):
        failures.append(PAYEE_NAME_MISMATCH)
    info = {
        "bank_read": reading.payee_bank,
        "bank_identified": {"bin": bank.bin, "short_name": bank.short_name} if bank else None,
        "expected_bank": {"bin": payee.bank_bin, "short_name": payee.bank_name},
        "account_read": reading.fields.payee_account if reading.fields else None,
        "expected_account": payee.account_number,
        "holder_read": read_name,
        "expected_holder": holder,
    }
    return failures, unknowns, info


def _summary(verdict: str, failures: list[str], reasons: list[str], suggested: str | None, confident: bool) -> str:
    if verdict == ACCEPTED:
        return "Read automatically: it matches the payment FundLok issued and the business's verified account"
    if verdict == DENIED:
        return "Read automatically: it does not match (" + ", ".join(failures) + ")"
    left = "; left for staff (" + ", ".join(reasons) + ")"
    if suggested == ACCEPTED:
        return "Read automatically: it matches" + left
    if suggested == DENIED:
        return "Read automatically: it does not match (" + ", ".join(failures) + ")" + left
    if confident:
        return "Read automatically, but the receiving bank or holder could not be confirmed" + left
    return "The receipt could not be read with confidence" + left


async def _expected(db: AsyncSession, tranche: Tranche, window: FundingWindow) -> tuple[ExpectedTransfer, PaymentAccount]:
    payee = await db.get(PaymentAccount, window.sme_payment_account_id)
    expected = ExpectedTransfer(
        amount_vnd=tranche.amount_vnd,
        reference_code=tranche.reference_code,
        payee_account_number=payee.account_number,
        payer_kyc_name=await resolve_kyc_name(db, tranche.investor_id),
        reserved_at=tranche.reserved_at,
    )
    return expected, payee


async def bank_txn_used_elsewhere(db: AsyncSession, bank_txn_id: str | None, transfer_id: UUID) -> bool:
    if not bank_txn_id:
        return False
    row = (
        await db.execute(
            select(Transfer.id).where(
                Transfer.bank_txn_id == bank_txn_id,
                Transfer.kind.in_(("DISBURSEMENT", "REFUND")),
                Transfer.id != transfer_id,
            )
        )
    ).first()
    return row is not None


async def run_system_check(
    db: AsyncSession,
    *,
    transfer_id: UUID,
    reading: ReceiptReading,
    auto_verdict: bool,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> SystemCheck:
    """Record the reading, run the checks and, when allowed and confident, decide the receipt. Runs in the upload's
    transaction, after submit_receipt (which holds the ticket and round locks)."""
    p = resolve(params)
    transfer = await db.get(Transfer, transfer_id)
    tranche = await db.get(Tranche, transfer.tranche_id)
    window = await db.get(FundingWindow, tranche.window_id)

    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type=EVENT_OCR_READ, now=now,
        actor_role=SYSTEM_ROLE, payload=reading.to_json(),
    )

    failures: list[str] = []
    unknowns: list[str] = []
    receiver: dict[str, Any] | None = None
    fields = reading.fields
    if fields is not None and reading.confident:
        expected, payee = await _expected(db, tranche, window)
        failures = check_receipt(
            fields, expected, now=now, future_skew=dt.timedelta(minutes=p.receipt_future_skew_minutes)
        )
        if await bank_txn_used_elsewhere(db, fields.bank_txn_id, transfer.id):
            failures.append(DUPLICATE_BANK_TXN)
        receiver_failures, unknowns, receiver = await check_receiver(db, reading, payee)
        failures.extend(receiver_failures)

    # A wrong detail decides it (DENIED) even when another detail is missing; an all-correct read is ACCEPTED only
    # when nothing about the receiver is unknown.
    suggested = None
    if fields is not None and reading.confident:
        suggested = DENIED if failures else (ACCEPTED if not unknowns else None)
    reasons: list[str] = []
    if not reading.confident:
        reasons.extend(reading.notes)
    if not failures:
        reasons.extend(unknowns)
    if tranche.status == "UNDER_REVIEW" and tranche.review_cause == "LATE_RECEIPT":
        reasons.append(LATE_RECEIPT)
    elif tranche.status != "SIGNED" or transfer.check_status != "AWAITING_FIELDS":
        reasons.append(NOT_AWAITING)
    if suggested is not None and not auto_verdict:
        reasons.append(AUTO_VERDICT_OFF)

    verdict = suggested if suggested is not None and not reasons else NEEDS_REVIEW
    applied = verdict in (ACCEPTED, DENIED)
    summary = _summary(verdict, failures, reasons, suggested, reading.confident and fields is not None)

    if applied:
        assert fields is not None
        await _record_fields(db, transfer, fields, failures, payee_bank=reading.payee_bank, now=now, reason=summary)
        if verdict == ACCEPTED:
            await _accept(db, transfer, tranche, fields, now=now, reason=summary)
        else:
            _deny(transfer, tranche, failures)
        await db.flush()

    payload: dict[str, Any] = {
        "verdict": verdict,
        "applied": applied,
        "suggested": suggested,
        "failure_codes": failures,
        "review_reasons": reasons,
        "receiver": receiver,
        "engine": reading.engine,
        "confidence": reading.confidence,
    }
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type=EVENT_SYSTEM_VERDICT, now=now,
        actor_role=SYSTEM_ROLE, reason=summary, payload=payload,
    )
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche.id,
        action=AUDIT_ACTIONS[verdict],
        actor_id=None,
        after_state={
            "transfer_id": str(transfer.id),
            "check_status": transfer.check_status,
            "tranche_status": tranche.status,
            "failure_codes": failures,
            "reason": summary,
        },
    )
    return SystemCheck(
        verdict=verdict, applied=applied, suggested=suggested, failure_codes=failures, review_reasons=reasons,
        receiver=receiver,
    )


async def _record_fields(
    db: AsyncSession, transfer: Transfer, fields: ReceiptFields, failures: list[str], *, payee_bank: str | None,
    now: dt.datetime, reason: str,
) -> None:
    transfer.parsed_fields = fields.to_json() | {"payee_bank": payee_bank}
    transfer.transferred_at = fields.transferred_at
    transfer.failure_codes = failures
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type="FIELDS_ENTERED", now=now,
        actor_role=SYSTEM_ROLE, reason=reason, payload={"fields": transfer.parsed_fields, "source": "OCR"},
    )
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id, event_type="CHECKS_RUN", now=now,
        actor_role=SYSTEM_ROLE, payload={"failure_codes": failures, "matched": not failures},
    )


async def _accept(
    db: AsyncSession, transfer: Transfer, tranche: Tranche, fields: ReceiptFields, *, now: dt.datetime, reason: str
) -> None:
    """Received: CONFIRMED now (SIGNED -> ATTESTED -> CONFIRMED, as a staff approval), no loan yet."""
    transfer.bank_txn_id = fields.bank_txn_id  # unique: one bank transaction funds one ticket
    transfer.check_status = "MATCHED"
    transfer.confirmation_level = "CONFIRMED"
    await transition_tranche(
        db, tranche, "ATTESTED", now=now, actor_role=SYSTEM_ROLE, reason=reason, confirm_deadline=now,
    )
    await transition_tranche(db, tranche, "CONFIRMED", now=now, actor_role=SYSTEM_ROLE, reason=reason)


def _deny(transfer: Transfer, tranche: Tranche, failures: list[str]) -> None:
    """Rejected as staff reject a receipt: the ticket waits for one again while its hold lasts. The bank
    transaction id is not claimed, so a correct receipt of the same transfer can still be uploaded."""
    transfer.check_status = "REJECTED"
    transfer.failure_codes = failures
    tranche.receipt_submitted_at = None


# --------------------------------------------------------------------------- #
# reading the verdict back (admin screens, staff override)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class SystemCheckState:
    verdict: str
    applied: bool
    suggested: str | None
    failure_codes: list[str]
    review_reasons: list[str]
    receiver: dict[str, Any] | None
    summary: str | None
    decided_at: dt.datetime
    reading: dict[str, Any] | None
    reviewed: EvidenceLog | None  # staff's review of an applied verdict, if any
    payer: dict[str, Any] | None = None  # a repayment receipt's payer as read and as expected


async def system_check_state(
    db: AsyncSession, transfer_id: UUID, *, entity_type: str = "TRANSFER"
) -> SystemCheckState | None:
    """The latest reading and verdict recorded against an investor's transfer, or (entity_type EVIDENCE_FILE, the
    receipt's id) against a business's repayment receipt (app/market/repayment_receipt_check.py)."""
    rows = (
        await db.execute(
            select(EvidenceLog)
            .where(
                EvidenceLog.entity_type == entity_type,
                EvidenceLog.entity_id == transfer_id,
                EvidenceLog.event_type.in_((EVENT_OCR_READ, EVENT_SYSTEM_VERDICT, EVENT_SYSTEM_VERDICT_REVIEWED)),
            )
            .order_by(EvidenceLog.occurred_at, EvidenceLog.created_at)
        )
    ).scalars().all()
    verdict = next((r for r in reversed(rows) if r.event_type == EVENT_SYSTEM_VERDICT), None)
    if verdict is None:
        return None
    reading = next((r for r in reversed(rows) if r.event_type == EVENT_OCR_READ), None)
    reviewed = next((r for r in reversed(rows) if r.event_type == EVENT_SYSTEM_VERDICT_REVIEWED), None)
    payload = verdict.payload or {}
    return SystemCheckState(
        verdict=payload.get("verdict", NEEDS_REVIEW),
        applied=bool(payload.get("applied")),
        suggested=payload.get("suggested"),
        failure_codes=list(payload.get("failure_codes") or []),
        review_reasons=list(payload.get("review_reasons") or []),
        receiver=payload.get("receiver"),
        summary=verdict.reason,
        decided_at=verdict.occurred_at,
        reading=reading.payload if reading else None,
        reviewed=reviewed,
        payer=payload.get("payer"),
    )


async def record_staff_review(
    db: AsyncSession, *, transfer_id: UUID, state: SystemCheckState, outcome: str, reason: str, staff_id: UUID,
    now: dt.datetime,
) -> None:
    """Staff looked at a receipt the system decided: APPROVED or DENIED, and whether that overrides the system."""
    overrides = (state.verdict == ACCEPTED and outcome == "DENIED") or (state.verdict == DENIED and outcome == "APPROVED")
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer_id, event_type=EVENT_SYSTEM_VERDICT_REVIEWED, now=now,
        actor_id=staff_id, actor_role="ADMIN", reason=reason,
        payload={"system_verdict": state.verdict, "outcome": outcome, "overrides_system": overrides},
    )
