"""Registered payment accounts (HANDOFF-04 I-15). Spec: transfer-and-evidence.md section 3.

Who verifies an account (Edward and Loc, 2026-10-30; PROVISIONAL(Q-D18)). The standard is FundLok name = KYC/KYB
name = BANK account holder name, for investors and SMEs alike:
- Every saved account starts PENDING ("waiting verification"). The account name the user typed is self-declared,
  so it never approves anything; `name_match` / `expected_name` are only a hint for staff.
- Staff verify it: they look up the holder name at the bank, compare it with the KYC name (investor) or KYB
  business name (SME), and approve or reject, with a written reason either way. When approving they record the
  holder name they saw (`verified_holder_name`):
    * INVESTOR: it must equal the KYC name, or the account cannot be approved (reject it). Money disbursed to
      an investor, refunds and disputes must trace back to the KYC-verified person (AML).
    * SME: a difference (an abbreviation, a trade name) can be accepted only with an explicit
      `accept_difference` and the reason saying why.
- A `BankNameLookup` (name_lookup.py) can later do the lookup automatically: an account is auto-approved only if
  the name the BANK returns equals the verified name. No provider is connected yet.
An auto-approved account has `approved_by` NULL; one a person decided carries their id.
This is NOT the mock `linked_accounts` table.
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
from app.direct_transfer.name_lookup import BankNameLookup
from app.direct_transfer.models import PaymentAccount


async def register_payment_account(
    db: AsyncSession,
    *,
    user_id: UUID,
    holder_role: str,
    holder_type: str,
    bank_bin: str | None,
    account_number: str,
    account_name: str,
    evidence_file_id: UUID | None,
    now: dt.datetime,
    bank_name: str | None = None,  # used only when bank_bin is None; for a listed bank it comes from banks.py
    lookup: BankNameLookup | None = None,  # None: no automatic verification, every account waits for staff
) -> PaymentAccount:
    """`bank_bin` None means a bank outside the NAPAS list: the account is saved under `bank_name` as typed."""
    if holder_role not in ("SME", "INVESTOR") or holder_type not in ("INDIVIDUAL", "ENTITY"):
        raise bad_request("Invalid account holder")
    if bank_bin is not None and not (bank_bin.isdigit() and len(bank_bin) == 6):
        raise bad_request("Invalid bank code")
    if not account_number.isalnum() or not (4 <= len(account_number) <= 32):
        raise bad_request("Invalid account number")
    if not account_name.strip():
        raise bad_request("Account name is required")
    if bank_bin is None:
        typed_name = " ".join((bank_name or "").split())
        if not typed_name or len(typed_name) > 100:
            raise bad_request("Bank name is required", "BANK_NAME_REQUIRED")
        resolved_name, bank_code = typed_name, None
    else:
        bank = require_bank(bank_bin)
        resolved_name, bank_code = bank.short_name, bank.code
    account = PaymentAccount(
        user_id=user_id,
        holder_role=holder_role,
        holder_type=holder_type,
        bank_bin=bank_bin,
        bank_name=resolved_name,
        account_number=account_number,
        account_name=account_name,
        evidence_file_id=evidence_file_id,
        status="PENDING",
        created_at=now,  # injected clock, so listing order is deterministic (server now() ties within a transaction)
    )
    expected = await expected_holder_name(db, user_id=user_id, holder_role=holder_role)
    matched = names_match(account_name, expected)  # a hint for staff: the typed name is self-declared
    bank_name_returned: str | None = None
    if lookup is not None and bank_bin is not None:
        bank_name_returned = await lookup.lookup(bank_bin=bank_bin, account_number=account_number)
    auto_approved = names_match(bank_name_returned or "", expected) is True
    if auto_approved:
        account.status = "APPROVED"
        account.approved_at = now
    account.name_match, account.expected_name = matched, expected
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
        payload={
            "bank_bin": bank_bin,
            "bank_code": bank_code,
            "evidence_file_id": str(evidence_file_id) if evidence_file_id else None,
            "name_match": matched,
            "auto_approved": auto_approved,
            "verified_holder_name": bank_name_returned if auto_approved else None,
            "verified_by": "BANK_LOOKUP" if auto_approved else None,
        },
    )
    if matched is False:
        # A hint for the staff reviewing it (the typed name differs from the KYC/KYB name).
        await log_event(
            db,
            entity_type="PAYMENT_ACCOUNT",
            entity_id=account.id,
            event_type="PAYMENT_ACCOUNT_NAME_MISMATCH",
            now=now,
            actor_id=user_id,
            actor_role=holder_role,
            payload={"holder_role": holder_role, "auto_approved": auto_approved},
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
    verified_holder_name: str | None = None,
    accept_difference: bool = False,
) -> PaymentAccount:
    """Staff decision on a PENDING account. A reason is required either way. To APPROVE, staff record the account
    holder name they saw at the bank (`verified_holder_name`); see the module docstring for what must match."""
    if decision not in ("APPROVED", "REJECTED"):
        raise bad_request("Invalid decision")
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    account = await db.get(PaymentAccount, account_id)
    if account is None:
        raise not_found("Payment account not found")
    if account.status != "PENDING":
        raise conflict("ALREADY_DECIDED", "This account has already been decided")
    payload: dict = {"decision": decision}
    if decision == "APPROVED":
        expected = await expected_holder_name(db, user_id=account.user_id, holder_role=account.holder_role)
        if not (verified_holder_name or "").strip():
            raise DirectTransferError(422, "Record the account holder name as the bank shows it", "HOLDER_NAME_REQUIRED")
        same = names_match(verified_holder_name, expected)
        if account.holder_role == "INVESTOR":
            if same is None:
                raise conflict("KYC_NAME_MISSING", "This investor has no verified KYC name to check the account against")
            if same is False:
                raise conflict(
                    "HOLDER_NAME_MISMATCH",
                    "The bank's holder name must equal the investor's KYC name. Reject this account instead",
                )
        elif same is not True and not accept_difference:
            raise conflict(
                "HOLDER_NAME_MISMATCH",
                "The bank's holder name differs from the company's verified name (or there is none). "
                "Approve only if you have confirmed the difference, with accept_difference and the reason",
            )
        payload.update(
            verified_holder_name=verified_holder_name.strip(),
            expected_name=expected,
            holder_name_matches=same,
            accept_difference=bool(accept_difference and same is not True),
            verified_by="STAFF_LOOKUP",
        )
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
        payload=payload,
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
    if account.bank_bin is None:
        # A bank outside the NAPAS list: no VietQR can pay into it, so it cannot fund or receive a tranche.
        raise conflict("ACCOUNT_NOT_NAPAS", "This account's bank cannot be paid by VietQR")
    return account


async def list_payment_accounts(db: AsyncSession, *, user_id: UUID) -> list[PaymentAccount]:
    """The caller's own accounts, oldest first, with their approval status (spec: GET /payment-accounts).
    Each carries `name_match` / `expected_name` (see annotate_name_match)."""
    rows = await db.execute(
        select(PaymentAccount).where(PaymentAccount.user_id == user_id).order_by(PaymentAccount.created_at, PaymentAccount.id)
    )
    accounts = list(rows.scalars().all())
    await annotate_name_match(db, accounts)
    return accounts


async def expected_holder_name(db: AsyncSession, *, user_id: UUID, holder_role: str) -> str | None:
    """The verified name an account should be held in, or None when there is none to compare against.

    INVESTOR: the KYC name (the same name the receipt check uses). SME: the business name from the newest
    eKYB attempt, and only if that attempt was APPROVED: the project's own `legal_name` is self-declared, so it
    is not used here."""
    from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification

    if holder_role == "INVESTOR":
        from app.direct_transfer.tranches import resolve_kyc_name

        return (await resolve_kyc_name(db, user_id)) or None
    row = (
        await db.execute(
            select(GVerifyKybVerification.status, GVerifyKybVerification.business_name)
            .where(GVerifyKybVerification.user_id == user_id)
            .order_by(GVerifyKybVerification.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None or row.status != STATUS_APPROVED or not (row.business_name or "").strip():
        return None
    return row.business_name


def names_match(account_name: str, expected: str | None) -> bool | None:
    """True/False on the receipt check's normalization (case, accents, punctuation, spacing ignored); None when
    there is no verified name to compare with."""
    from app.direct_transfer.receipts import normalize_name

    if not expected:
        return None
    return normalize_name(account_name) == normalize_name(expected)


async def annotate_name_match(db: AsyncSession, accounts: list[PaymentAccount]) -> None:
    """Set the non-persisted `name_match` and `expected_name` on each account (for the owner's screen and the
    staff queue). Computed on read, so it follows the owner's current KYC/KYB; the decision at save time is in the
    evidence log."""
    cache: dict[tuple[UUID, str], str | None] = {}
    for account in accounts:
        key = (account.user_id, account.holder_role)
        if key not in cache:
            cache[key] = await expected_holder_name(db, user_id=account.user_id, holder_role=account.holder_role)
        account.expected_name = cache[key]
        account.name_match = names_match(account.account_name, cache[key])
