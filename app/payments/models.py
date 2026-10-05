"""Payout instructions and API idempotency records (app/payments/).

Spec: docs/specs/payments/payout-idempotency.md (DRAFT)

Three layers stop a retry, a timeout or a double-click from paying twice:

    idempotency_records  -- one row per (actor, endpoint, Idempotency-Key).
                            Written in the same DB transaction as the effect
                            it guards, so "record exists" <=> "effect
                            committed". A replay returns the stored response.
    payout_instructions  -- one row per intended bank transfer. A partial
                            unique index allows at most one live disbursement
                            per contract; `client_request_id` is generated
                            once, here, and reused verbatim on every retry
                            and status inquiry sent to the bank.
    payout_events        -- append-only log of every status change (written
                            by a DB trigger) and every bank response.

The bank's name never appears in a table, column or enum value: the bank is
reached through `custodial_account_id` and the gateway seam in
app/banking/gateway.py.
"""
import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base

PAYOUT_PURPOSES = ("SME_DISBURSEMENT",)

PAYOUT_STATUSES = (
    "PENDING_APPROVAL",  # maker created it; a second person must approve
    "APPROVED",  # checker approved; waiting for the dispatcher
    "SUBMITTING",  # claimed by one dispatcher; the bank call may be in flight
    "SUBMITTED",  # the bank acknowledged it; final status not known yet
    "UNKNOWN",  # the call timed out or broke after it may have reached the bank
    "MANUAL_REVIEW",  # automatic inquiry could not settle it; ops must check
    "SUCCEEDED",  # the bank confirmed the transfer; ledger posted
    "FAILED",  # the bank confirmed it did not execute
    "CANCELLED",  # withdrawn before it was ever sent
)

# Statuses that free the contract's single disbursement slot again. Every
# other status -- including SUCCEEDED -- keeps it taken.
PAYOUT_RELEASED_STATUSES = ("FAILED", "CANCELLED")

# Allowed status transitions. The DB trigger `payout_instruction_guard`
# (migration c5e1a9d4b7f2) enforces the same table, so a buggy code path or a
# hand-written UPDATE cannot move a payout backwards either.
PAYOUT_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
    "APPROVED": frozenset({"SUBMITTING", "CANCELLED"}),
    # -> APPROVED only when the request provably never left (NOT_SENT).
    "SUBMITTING": frozenset({"SUBMITTED", "SUCCEEDED", "FAILED", "UNKNOWN", "APPROVED"}),
    "SUBMITTED": frozenset({"SUCCEEDED", "FAILED", "UNKNOWN", "MANUAL_REVIEW"}),
    # -> APPROVED only when resend-on-not-found is explicitly enabled.
    "UNKNOWN": frozenset({"SUBMITTED", "SUCCEEDED", "FAILED", "APPROVED", "MANUAL_REVIEW"}),
    "MANUAL_REVIEW": frozenset({"SUCCEEDED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}

BENEFICIARY_STATUSES = ("PENDING_VERIFICATION", "VERIFIED", "REVOKED")

PAYOUT_EVENT_TYPES = ("STATUS_CHANGED", "SUBMIT_RESULT", "INQUIRY_RESULT", "OUTCOME_APPLIED")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class IdempotencyRecord(Base):
    """One Idempotency-Key claim. Inserted at the start of the request and
    completed with the response before COMMIT, inside the same transaction
    as the effect. A concurrent request with the same key blocks on the
    unique index until this one commits (then replays) or rolls back (then
    proceeds) -- no separate IN_PROGRESS state is ever visible."""

    __tablename__ = "idempotency_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    scope = Column(Text, nullable=False)
    idempotency_key = Column(Text, nullable=False)
    request_hash = Column(Text, nullable=False)
    response_status = Column(Integer, nullable=True)
    response_body = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now() + interval '30 days'"))

    __table_args__ = (
        UniqueConstraint("actor_id", "scope", "idempotency_key", name="uq_idempotency_records_actor_scope_key"),
        CheckConstraint("char_length(idempotency_key) BETWEEN 8 AND 128", name="ck_idempotency_records_key_length"),
        CheckConstraint("char_length(request_hash) = 64", name="ck_idempotency_records_request_hash"),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )


class PayoutBeneficiary(Base):
    """A destination account a payout may go to. Only VERIFIED rows can be
    used. Verification (bank account-name inquiry plus dual control) is a
    separate flow; this table is the registry the payout path reads from, so
    a disbursement request can never carry a free-text account number."""

    __tablename__ = "payout_beneficiaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    bank_bin = Column(Text, nullable=False)
    account_number = Column(Text, nullable=False)
    account_name = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="PENDING_VERIFICATION")
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("owner_user_id", "bank_bin", "account_number", name="uq_payout_beneficiaries_owner_account"),
        CheckConstraint(f"status IN ({_in(BENEFICIARY_STATUSES)})", name="ck_payout_beneficiaries_status"),
        CheckConstraint("bank_bin ~ '^[0-9]{6}$'", name="ck_payout_beneficiaries_bank_bin"),
        CheckConstraint("account_number ~ '^[0-9A-Za-z]{4,34}$'", name="ck_payout_beneficiaries_account_number"),
        CheckConstraint(
            "status <> 'VERIFIED' OR (verified_at IS NOT NULL AND verified_by IS NOT NULL)",
            name="ck_payout_beneficiaries_verified_fields",
        ),
    )


class PayoutInstruction(Base):
    """One intended bank transfer. Amount, destination and the bank request
    id are fixed at creation and immutable (DB trigger). Status moves only
    along PAYOUT_TRANSITIONS."""

    __tablename__ = "payout_instructions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_request_id = Column(Text, nullable=False, unique=True)
    purpose = Column(Text, nullable=False)
    contract_id = Column(UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=True)
    custodial_account_id = Column(
        UUID(as_uuid=True), ForeignKey("custodial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    beneficiary_id = Column(
        UUID(as_uuid=True), ForeignKey("payout_beneficiaries.id", ondelete="RESTRICT"), nullable=False
    )
    # Snapshot of what is sent to the bank, kept even if the registry row
    # changes later.
    beneficiary_bank_bin = Column(Text, nullable=False)
    beneficiary_account_number = Column(Text, nullable=False)
    beneficiary_account_name = Column(Text, nullable=False)
    amount = Column(Numeric(20, 0), nullable=False)
    currency = Column(Text, nullable=False, server_default="VND")
    description = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="PENDING_APPROVAL")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    attempt_count = Column(Integer, nullable=False, server_default="0")
    last_submitted_at = Column(DateTime(timezone=True), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=True)
    bank_transaction_ref = Column(Text, nullable=True, unique=True)
    failure_code = Column(Text, nullable=True)
    failure_reason = Column(Text, nullable=True)
    ledger_transaction_id = Column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    settled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # The double-spend backstop: at most one live disbursement per
        # contract, whatever code path (or person with psql) tries to add one.
        Index(
            "uq_payout_instructions_active_disbursement",
            "contract_id",
            unique=True,
            postgresql_where=text(
                f"purpose = 'SME_DISBURSEMENT' AND status NOT IN ({_in(PAYOUT_RELEASED_STATUSES)})"
            ),
        ),
        Index("ix_payout_instructions_status_next_check", "status", "next_check_at"),
        CheckConstraint(f"purpose IN ({_in(PAYOUT_PURPOSES)})", name="ck_payout_instructions_purpose"),
        CheckConstraint(f"status IN ({_in(PAYOUT_STATUSES)})", name="ck_payout_instructions_status"),
        CheckConstraint("amount > 0", name="ck_payout_instructions_amount_positive"),
        CheckConstraint("currency = 'VND'", name="ck_payout_instructions_currency"),
        CheckConstraint("attempt_count >= 0", name="ck_payout_instructions_attempt_count"),
        CheckConstraint("client_request_id ~ '^[A-Z0-9]{8,60}$'", name="ck_payout_instructions_client_request_id"),
        CheckConstraint("char_length(description) BETWEEN 1 AND 140", name="ck_payout_instructions_description"),
        CheckConstraint(
            "purpose <> 'SME_DISBURSEMENT' OR contract_id IS NOT NULL",
            name="ck_payout_instructions_disbursement_contract",
        ),
        # Dual control: nothing past PENDING_APPROVAL without a second person.
        CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'CANCELLED') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_payout_instructions_approved_before_send",
        ),
        CheckConstraint(
            "approved_by IS NULL OR approved_by <> created_by",
            name="ck_payout_instructions_maker_ne_checker",
        ),
        CheckConstraint(
            "status <> 'SUCCEEDED' OR ledger_transaction_id IS NOT NULL",
            name="ck_payout_instructions_succeeded_has_ledger",
        ),
    )


class PayoutEvent(Base):
    """Append-only audit trail for one payout instruction (DB trigger
    rejects UPDATE/DELETE). STATUS_CHANGED rows are written by the
    `payout_instruction_log_status` trigger, so even a manual UPDATE is
    recorded; the other types are written by app/payments/payouts.py."""

    __tablename__ = "payout_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instruction_id = Column(
        UUID(as_uuid=True), ForeignKey("payout_instructions.id", ondelete="RESTRICT"), nullable=False
    )
    event_type = Column(Text, nullable=False)
    attempt_no = Column(Integer, nullable=True)
    from_status = Column(Text, nullable=True)
    to_status = Column(Text, nullable=True)
    outcome = Column(Text, nullable=True)
    bank_code = Column(Text, nullable=True)
    detail = Column(JSONB, nullable=True)
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"event_type IN ({_in(PAYOUT_EVENT_TYPES)})", name="ck_payout_events_type"),
        Index("ix_payout_events_instruction_created", "instruction_id", "created_at"),
    )
