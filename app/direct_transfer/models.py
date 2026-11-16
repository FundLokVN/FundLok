"""Direct-transfer ORM models (HANDOFF-04 B1).

Specs: docs/specs/direct_transfer/*.md. ADR-009.

Money is integer VND stored as BIGINT (Python int, no Decimal, no float). Timestamps are TIMESTAMPTZ.
Primary keys are UUID.

Append-only tables (evidence_log, cic_events) are protected twice: by a PostgreSQL BEFORE UPDATE/DELETE
trigger (created in the migration) and by SQLAlchemy ORM event listeners at the bottom of this file, so
application code cannot update or delete them either. Corrections are new rows.

This module is re-exported from `app/models/__init__.py` (shared file; flagged for Phat's OK).
"""
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.core.base import Base

TRANCHE_STATES = ("RESERVED", "SIGNED", "ATTESTED", "CONFIRMED", "ACTIVE", "EXPIRED", "UNDER_REVIEW", "VOID")
# States in which a tranche is "live" for the one-live-tranche-per-investor rule.
TRANCHE_LIVE_STATES = ("RESERVED", "SIGNED", "ATTESTED", "CONFIRMED", "UNDER_REVIEW", "ACTIVE")
# AWAITING_SME and AWAITING_PAYMENT belong to the commit-then-accept flow (ADR-011); no LEGACY window enters them.
WINDOW_STATES = (
    "OPEN", "FULL", "CLOSING", "AWAITING_SME", "AWAITING_PAYMENT", "REPAYING", "FAILED", "REPAID", "CANCELLED",
)
WINDOW_LIVE_STATES = ("OPEN", "FULL", "CLOSING", "AWAITING_SME", "AWAITING_PAYMENT", "REPAYING")
_WINDOW_LIVE_SQL = "status IN ('OPEN','FULL','CLOSING','AWAITING_SME','AWAITING_PAYMENT','REPAYING')"


def _uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts(**kw):
    return Column(DateTime(timezone=True), **kw)


class PaymentAccount(Base):
    __tablename__ = "payment_accounts"

    id = _uuid_pk()
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    holder_role = Column(Text, nullable=False)
    holder_type = Column(Text, nullable=False)
    # NULL for a bank outside the NAPAS list: bank_name is then the name the user typed, and no VietQR or
    # direct transfer can use the account (get_usable_account refuses it).
    bank_bin = Column(Text, nullable=True)
    bank_name = Column(Text, nullable=False)
    account_number = Column(Text, nullable=False)
    account_name = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default="PENDING")
    evidence_file_id = Column(UUID(as_uuid=True), ForeignKey("evidence_files.id", ondelete="SET NULL"), nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = _ts(nullable=True)
    rejection_reason = Column(Text, nullable=True)
    created_at = _ts(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "bank_bin", "account_number", name="uq_payment_accounts_user_bank_number"),
        CheckConstraint("holder_role IN ('SME','INVESTOR')", name="ck_payment_accounts_role"),
        CheckConstraint("holder_type IN ('INDIVIDUAL','ENTITY')", name="ck_payment_accounts_holder_type"),
        CheckConstraint("bank_bin ~ '^[0-9]{6}$'", name="ck_payment_accounts_bin"),
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED')", name="ck_payment_accounts_status"),
        # The constraint above never matches a NULL bin, so banks outside the list dedupe on the typed name.
        Index(
            "uq_payment_accounts_user_other_bank_number", "user_id", "bank_name", "account_number",
            unique=True, postgresql_where=text("bank_bin IS NULL"),
        ),
    )


class EvidenceFile(Base):
    __tablename__ = "evidence_files"

    id = _uuid_pk()
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose = Column(Text, nullable=False)
    storage_key = Column(Text, nullable=False, unique=True)
    sha256 = Column(Text, nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    content_type = Column(Text, nullable=False)
    original_filename = Column(Text, nullable=True)
    created_at = _ts(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('RECEIPT','BANK_STATEMENT','SME_STATEMENT','INVESTOR_STATEMENT','SIGNED_DOCUMENT',"
            "'BANK_CONFIRMATION','DECLARATION','INVOICE','OTHER')",
            name="ck_evidence_files_purpose",
        ),
        # One receipt file funds at most one tranche (file-hash uniqueness).
        Index("uq_evidence_receipt_sha256", "sha256", unique=True, postgresql_where=text("purpose = 'RECEIPT'")),
    )


class EvidenceLog(Base):
    """Append-only evidence log (DB trigger + ORM guard). Corrections are new rows."""

    __tablename__ = "evidence_log"

    id = _uuid_pk()
    entity_type = Column(Text, nullable=False)
    entity_id = Column(UUID(as_uuid=True), nullable=False)
    event_type = Column(Text, nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=True)
    actor_role = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    file_sha256 = Column(Text, nullable=True)
    occurred_at = _ts(nullable=False)
    # clock_timestamp(), not now(): rows written in one transaction must still order by insertion (now() is constant
    # for the whole transaction, which made same-instant timeline rows tie).
    created_at = _ts(server_default=func.clock_timestamp())

    __table_args__ = (Index("ix_evidence_log_entity", "entity_type", "entity_id", "occurred_at"),)


class FundingWindow(Base):
    __tablename__ = "funding_windows"

    id = _uuid_pk()
    listing_id = Column(UUID(as_uuid=True), ForeignKey("listings.id", ondelete="RESTRICT"), nullable=False, unique=True)
    contract_id = Column(UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    sme_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    sme_payment_account_id = Column(
        UUID(as_uuid=True), ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    target_vnd = Column(BigInteger, nullable=False)
    min_raise_vnd = Column(BigInteger, nullable=False)
    min_ticket_vnd = Column(BigInteger, nullable=False)
    annual_rate_pct = Column(Numeric(6, 3), nullable=False)
    term_months = Column(Integer, nullable=False)
    offer_terms = Column(JSONB, nullable=False)
    declared_outside_debt_vnd = Column(BigInteger, nullable=False, server_default="0")
    window_start = _ts(nullable=False)
    window_end = _ts(nullable=False)
    status = Column(Text, nullable=False, server_default="OPEN")
    close_reason = Column(Text, nullable=True)
    closing_started_at = _ts(nullable=True)
    close_wait_until = _ts(nullable=True)
    closed_at = _ts(nullable=True)
    confirmed_total_vnd = Column(BigInteger, nullable=True)
    repayment_start_date = Column(Date, nullable=True)
    facility_id = Column(UUID(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=True)
    idempotency_key = Column(Text, nullable=True, unique=True)
    # Commit-then-accept flow (ADR-011). LEGACY windows keep the old behaviour.
    flow = Column(Text, nullable=False, server_default="LEGACY")
    commit_closes_at = _ts(nullable=True)
    sme_deadline_at = _ts(nullable=True)
    pay_deadline_at = _ts(nullable=True)
    sme_decision = Column(Text, nullable=True)
    sme_decided_at = _ts(nullable=True)
    sme_decided_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    created_at = _ts(server_default=func.now())
    updated_at = _ts(server_default=func.now(), onupdate=func.now())

    tranches = relationship("Tranche", back_populates="window")

    __table_args__ = (
        CheckConstraint("target_vnd > 0 AND target_vnd <= 100000000", name="ck_funding_windows_target"),
        CheckConstraint("min_ticket_vnd > 0", name="ck_funding_windows_min_ticket"),
        CheckConstraint(
            "status IN ('OPEN','FULL','CLOSING','AWAITING_SME','AWAITING_PAYMENT','REPAYING','FAILED','REPAID','CANCELLED')",
            name="ck_funding_windows_status",
        ),
        CheckConstraint("flow IN ('LEGACY','COMMIT_THEN_ACCEPT')", name="ck_funding_windows_flow"),
        CheckConstraint(
            "sme_decision IN ('ACCEPTED','DECLINED','AUTO_ACCEPTED','AUTO_DECLINED')",
            name="ck_funding_windows_sme_decision",
        ),
        # One listing per SME (business) until the previous loan is repaid in full (Loc rule 1).
        Index(
            "uq_funding_windows_one_live_per_project",
            "project_id",
            unique=True,
            postgresql_where=text(_WINDOW_LIVE_SQL),
        ),
        # The same rule across the SME's projects (D-016, docs/specs/projects/one-borrower.md): a second project
        # cannot list while the first is open or repaying. open_funding_window reports it as SME_HAS_LIVE_LISTING.
        Index(
            "uq_funding_windows_one_live_per_sme",
            "sme_user_id",
            unique=True,
            postgresql_where=text(_WINDOW_LIVE_SQL),
        ),
    )


class Commitment(Base):
    """A lender's pledge to a commit-then-accept window. Moves no money and creates no tranche (ADR-011)."""

    __tablename__ = "commitments"

    id = _uuid_pk()
    window_id = Column(UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    investor_account_id = Column(
        UUID(as_uuid=True), ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    investor_type = Column(Text, nullable=False)
    amount_vnd = Column(BigInteger, nullable=False)
    status = Column(Text, nullable=False, server_default="PLEDGED")
    void_reason = Column(Text, nullable=True)
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=True)
    idempotency_key = Column(Text, nullable=True, unique=True)
    request_fingerprint = Column(Text, nullable=True)
    pledge_file_key = Column(Text, nullable=False)
    pledge_sha256 = Column(Text, nullable=False)
    pledge_certificate = Column(JSONB, nullable=False)
    committed_at = _ts(nullable=False)
    created_at = _ts(server_default=func.now())

    __table_args__ = (
        CheckConstraint("investor_type IN ('INDIVIDUAL','ENTITY')", name="ck_commitments_investor_type"),
        CheckConstraint("amount_vnd > 0", name="ck_commitments_amount"),
        CheckConstraint("status IN ('PLEDGED','FOLDED','VOID')", name="ck_commitments_status"),
        Index("ix_commitments_window_status", "window_id", "status"),
        Index("ix_commitments_window_investor", "window_id", "investor_id"),
    )


class LenderStanding(Base):
    __tablename__ = "lender_standing"

    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    status = Column(Text, nullable=False, server_default="GOOD")
    no_show_count = Column(Integer, nullable=False, server_default="0")
    locked_at = _ts(nullable=True)
    locked_window_id = Column(UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=True)
    unlocked_at = _ts(nullable=True)
    closed_at = _ts(nullable=True)
    updated_at = _ts(server_default=func.now(), onupdate=func.now())

    __table_args__ = (CheckConstraint("status IN ('GOOD','LOCKED','CLOSED')", name="ck_lender_standing_status"),)


class LenderUnlockAcknowledgement(Base):
    """Append-only by convention: the app only inserts."""

    __tablename__ = "lender_unlock_acknowledgements"

    id = _uuid_pk()
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    locked_window_id = Column(
        UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False
    )
    sentence_text = Column(Text, nullable=False)
    sentence_version = Column(Text, nullable=False)
    ekyc_verification_id = Column(UUID(as_uuid=True), nullable=False)
    acknowledged_at = _ts(nullable=False)
    ip_address = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)

    __table_args__ = (Index("ix_lender_unlock_ack_investor", "investor_id"),)


class AcceptanceReminder(Base):
    __tablename__ = "acceptance_reminders"

    window_id = Column(
        UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), primary_key=True
    )
    kind = Column(Text, primary_key=True)
    channel = Column(Text, primary_key=True)
    status = Column(Text, nullable=False)
    sent_to = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    sent_at = _ts(nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('D_MINUS_3','D_MINUS_1')", name="ck_acceptance_reminders_kind"),
        CheckConstraint("channel IN ('IN_APP','ZALO','EMAIL','SMS')", name="ck_acceptance_reminders_channel"),
        CheckConstraint("status IN ('SENT','FAILED','SKIPPED')", name="ck_acceptance_reminders_status"),
    )


class StandingOffer(Base):
    __tablename__ = "standing_offers"

    id = _uuid_pk()
    window_id = Column(UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False, unique=True)
    signer_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    provider = Column(Text, nullable=False)
    file_key = Column(Text, nullable=False)
    file_sha256 = Column(Text, nullable=False)
    certificate = Column(JSONB, nullable=False)
    signed_at = _ts(nullable=False)
    valid_until = _ts(nullable=False)


class Tranche(Base):
    """One investor loan contract (Loc.md 'Contract structure')."""

    __tablename__ = "tranches"

    id = _uuid_pk()
    window_id = Column(UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    investor_account_id = Column(UUID(as_uuid=True), ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True)
    investor_type = Column(Text, nullable=False)
    amount_vnd = Column(BigInteger, nullable=False)
    status = Column(Text, nullable=False, server_default="RESERVED")
    reference_code = Column(Text, nullable=False, unique=True)
    idempotency_key = Column(Text, nullable=True, unique=True)
    request_fingerprint = Column(Text, nullable=True)
    reserved_at = _ts(nullable=False)
    hold_expires_at = _ts(nullable=False)
    signed_at = _ts(nullable=True)
    receipt_submitted_at = _ts(nullable=True)
    attested_at = _ts(nullable=True)
    confirm_deadline = _ts(nullable=True)
    confirmed_at = _ts(nullable=True)
    activated_at = _ts(nullable=True)
    expired_at = _ts(nullable=True)
    voided_at = _ts(nullable=True)
    void_reason = Column(Text, nullable=True)
    review_cause = Column(Text, nullable=True)
    excluded_at_close = Column(Boolean, nullable=False, server_default="false")
    acceptance_file_key = Column(Text, nullable=True)
    acceptance_sha256 = Column(Text, nullable=True)
    acceptance_certificate = Column(JSONB, nullable=True)
    stub_receipt_date = Column(Date, nullable=True)
    stub_days = Column(Integer, nullable=False, server_default="0")
    stub_interest_vnd = Column(BigInteger, nullable=False, server_default="0")
    investor_fee_total_vnd = Column(BigInteger, nullable=False, server_default="0")
    holding_id = Column(UUID(as_uuid=True), ForeignKey("holdings.id", ondelete="SET NULL"), nullable=True)
    created_at = _ts(server_default=func.now())
    updated_at = _ts(server_default=func.now(), onupdate=func.now())

    window = relationship("FundingWindow", back_populates="tranches")

    __table_args__ = (
        CheckConstraint("amount_vnd > 0", name="ck_tranches_amount"),
        CheckConstraint(
            "status IN ('RESERVED','SIGNED','ATTESTED','CONFIRMED','ACTIVE','EXPIRED','UNDER_REVIEW','VOID')",
            name="ck_tranches_status",
        ),
        CheckConstraint("investor_type IN ('INDIVIDUAL','ENTITY')", name="ck_tranches_investor_type"),
        # One live tranche per investor per window (HANDOFF-04 I-5).
        Index(
            "uq_tranches_one_live_per_investor",
            "window_id",
            "investor_id",
            unique=True,
            postgresql_where=text("status IN ('RESERVED','SIGNED','ATTESTED','CONFIRMED','UNDER_REVIEW','ACTIVE')"),
        ),
        Index("ix_tranches_window_status", "window_id", "status"),
    )


class Transfer(Base):
    __tablename__ = "transfers"

    id = _uuid_pk()
    kind = Column(Text, nullable=False)
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=True)
    repayment_day_id = Column(UUID(as_uuid=True), ForeignKey("repayment_days.id", ondelete="RESTRICT"), nullable=True)
    fee_invoice_id = Column(UUID(as_uuid=True), ForeignKey("fee_invoices.id", ondelete="RESTRICT"), nullable=True)
    from_account_id = Column(UUID(as_uuid=True), ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True)
    to_account_id = Column(UUID(as_uuid=True), ForeignKey("payment_accounts.id", ondelete="RESTRICT"), nullable=True)
    amount_vnd = Column(BigInteger, nullable=False)
    reference = Column(Text, nullable=False)
    rail = Column(Text, nullable=False, server_default="VIETQR")
    confirmation_level = Column(Text, nullable=False, server_default="REPORTED")
    check_status = Column(Text, nullable=False, server_default="AWAITING_FIELDS")
    failure_codes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    evidence_file_id = Column(UUID(as_uuid=True), ForeignKey("evidence_files.id", ondelete="SET NULL"), nullable=True)
    parsed_fields = Column(JSONB, nullable=True)
    bank_txn_id = Column(Text, nullable=True)
    transferred_at = _ts(nullable=True)
    reported_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    idempotency_key = Column(Text, nullable=True, unique=True)
    due_by = _ts(nullable=True)  # REFUND: contract refund deadline
    created_at = _ts(server_default=func.now())
    updated_at = _ts(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("amount_vnd > 0", name="ck_transfers_amount"),
        CheckConstraint("kind IN ('DISBURSEMENT','REFUND','REPAYMENT','FEE_PAYMENT')", name="ck_transfers_kind"),
        CheckConstraint("rail IN ('VIETQR','STANDING_ORDER','BATCH_UPLOAD','MANUAL')", name="ck_transfers_rail"),
        CheckConstraint(
            "confirmation_level IN ('REPORTED','ATTESTED','CONFIRMED','VERIFIED')", name="ck_transfers_level"
        ),
        CheckConstraint(
            "check_status IN ('AWAITING_FIELDS','MATCHED','MISMATCH','REJECTED','DUE')", name="ck_transfers_check_status"
        ),
        # A bank transaction funds at most one tranche (and one refund).
        Index(
            "uq_transfers_bank_txn_id",
            "bank_txn_id",
            unique=True,
            postgresql_where=text("bank_txn_id IS NOT NULL AND kind IN ('DISBURSEMENT','REFUND')"),
        ),
    )


class Dispute(Base):
    __tablename__ = "disputes"

    id = _uuid_pk()
    subject_type = Column(Text, nullable=False)
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=True)
    repayment_day_id = Column(UUID(as_uuid=True), ForeignKey("repayment_days.id", ondelete="RESTRICT"), nullable=True)
    window_id = Column(UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False)
    cause = Column(Text, nullable=False)
    raised_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(Text, nullable=False, server_default="OPEN")
    required_next_party = Column(Text, nullable=False)
    response_deadline = _ts(nullable=True)
    outcome = Column(Text, nullable=True)
    decided_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at = _ts(nullable=True)
    decision_reason = Column(Text, nullable=True)
    bank_trace_reason = Column(Text, nullable=True)
    bank_trace_at = _ts(nullable=True)
    created_at = _ts(server_default=func.now())
    updated_at = _ts(server_default=func.now(), onupdate=func.now())

    evidence = relationship("DisputeEvidence", back_populates="dispute", order_by="DisputeEvidence.submitted_at")

    __table_args__ = (
        CheckConstraint("subject_type IN ('TRANCHE','REPAYMENT_DAY')", name="ck_disputes_subject_type"),
        CheckConstraint(
            "(subject_type = 'TRANCHE' AND tranche_id IS NOT NULL) OR "
            "(subject_type = 'REPAYMENT_DAY' AND repayment_day_id IS NOT NULL)",
            name="ck_disputes_subject",
        ),
        CheckConstraint(
            "cause IN ('RECEIPT_MISMATCH','SME_DISPUTE','LATE_RECEIPT','MISSED_DAY','LATE_REPORT')", name="ck_disputes_cause"
        ),
        CheckConstraint("status IN ('OPEN','BANK_TRACE','DECIDED')", name="ck_disputes_status"),
        CheckConstraint("required_next_party IN ('INVESTOR','SME','STAFF')", name="ck_disputes_next_party"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('RECEIVED','NOT_RECEIVED','PAID','NOT_PAID')", name="ck_disputes_outcome"
        ),
        Index(
            "uq_disputes_one_open_per_tranche",
            "tranche_id",
            unique=True,
            postgresql_where=text("status <> 'DECIDED' AND subject_type = 'TRANCHE'"),
        ),
        Index(
            "uq_disputes_one_open_per_repayment_day",
            "repayment_day_id",
            unique=True,
            postgresql_where=text("status <> 'DECIDED' AND subject_type = 'REPAYMENT_DAY'"),
        ),
    )


class DisputeEvidence(Base):
    __tablename__ = "dispute_evidence"

    id = _uuid_pk()
    dispute_id = Column(UUID(as_uuid=True), ForeignKey("disputes.id", ondelete="RESTRICT"), nullable=False)
    party = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    statement = Column(Text, nullable=True)
    file_id = Column(UUID(as_uuid=True), ForeignKey("evidence_files.id", ondelete="SET NULL"), nullable=True)
    submitted_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    submitted_at = _ts(nullable=False)

    dispute = relationship("Dispute", back_populates="evidence")

    __table_args__ = (
        CheckConstraint("party IN ('INVESTOR','SME','STAFF')", name="ck_dispute_evidence_party"),
        CheckConstraint("kind IN ('CLAIM','PROOF','COUNTER_PROOF','NOTE')", name="ck_dispute_evidence_kind"),
    )


class RepaymentDay(Base):
    __tablename__ = "repayment_days"

    id = _uuid_pk()
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=False)
    facility_id = Column(UUID(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False)
    scheduled_payment_id = Column(UUID(as_uuid=True), ForeignKey("scheduled_payment.id", ondelete="RESTRICT"), nullable=True)
    kind = Column(Text, nullable=False)
    day_index = Column(Integer, nullable=False)
    due_date = Column(Date, nullable=False)
    principal_vnd = Column(BigInteger, nullable=False)
    interest_vnd = Column(BigInteger, nullable=False)
    gross_vnd = Column(BigInteger, nullable=False)
    tax_withheld_vnd = Column(BigInteger, nullable=False, server_default="0")
    net_vnd = Column(BigInteger, nullable=False)
    status = Column(Text, nullable=False, server_default="DUE")
    report_deadline = _ts(nullable=False)
    marked_paid_at = _ts(nullable=True)
    marked_paid_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reported_at = _ts(nullable=True)
    reported_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    matched_at = _ts(nullable=True)
    settled_at = _ts(nullable=True)
    transfer_id = Column(
        UUID(as_uuid=True),
        ForeignKey("transfers.id", ondelete="SET NULL", use_alter=True, name="fk_repayment_days_transfer_id"),
        nullable=True,
    )
    created_at = _ts(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tranche_id", "kind", "day_index", name="uq_repayment_days_tranche_kind_index"),
        CheckConstraint("kind IN ('INSTALMENT','STUB')", name="ck_repayment_days_kind"),
        CheckConstraint("principal_vnd >= 0 AND interest_vnd >= 0", name="ck_repayment_days_nonneg"),
        CheckConstraint("gross_vnd = principal_vnd + interest_vnd", name="ck_repayment_days_gross"),
        CheckConstraint("tax_withheld_vnd >= 0 AND tax_withheld_vnd <= interest_vnd", name="ck_repayment_days_tax"),
        CheckConstraint("net_vnd = gross_vnd - tax_withheld_vnd", name="ck_repayment_days_net"),
        CheckConstraint(
            "status IN ('DUE','PRESUMED_PAID','REPORTED_MISSED','MATCHED')", name="ck_repayment_days_status"
        ),
        Index("ix_repayment_days_due", "facility_id", "due_date"),
    )


class CicEvent(Base):
    """Append-only CIC event log; `sent_at` is the single permitted mutation (NULL -> value)."""

    __tablename__ = "cic_events"

    id = _uuid_pk()
    event_type = Column(Text, nullable=False)
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=True)
    repayment_day_id = Column(UUID(as_uuid=True), ForeignKey("repayment_days.id", ondelete="RESTRICT"), nullable=True)
    window_id = Column(UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False)
    emit_key = Column(Text, nullable=False, unique=True)
    payload = Column(JSONB, nullable=False)
    occurred_at = _ts(nullable=False)
    sent_at = _ts(nullable=True)
    created_at = _ts(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('ATTESTED','VOID','DAILY_BALANCE','MISSED_DAY_CORRECTION')", name="ck_cic_events_type"
        ),
    )


class FeeInvoice(Base):
    __tablename__ = "fee_invoices"

    id = _uuid_pk()
    kind = Column(Text, nullable=False)
    window_id = Column(UUID(as_uuid=True), ForeignKey("funding_windows.id", ondelete="RESTRICT"), nullable=False)
    contract_id = Column(UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=False)
    payer_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    period_month = Column(Date, nullable=True)
    base_vnd = Column(BigInteger, nullable=False)
    fee_vnd = Column(BigInteger, nullable=False)
    vat_vnd = Column(BigInteger, nullable=False)
    late_fee_vnd = Column(BigInteger, nullable=False, server_default="0")
    total_vnd = Column(BigInteger, nullable=False)
    status = Column(Text, nullable=False, server_default="ISSUED")
    invoice_number = Column(Text, nullable=False, unique=True)
    provider = Column(Text, nullable=False)
    provider_ref = Column(Text, nullable=True)
    reference_code = Column(Text, nullable=False, unique=True)
    issued_at = _ts(nullable=False)
    due_at = _ts(nullable=False)
    paid_at = _ts(nullable=True)
    paid_bank_txn_id = Column(Text, nullable=True)
    created_at = _ts(server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('SME_ACTIVATION','INVESTOR_SERVICE')", name="ck_fee_invoices_kind"),
        CheckConstraint("fee_vnd >= 0 AND vat_vnd >= 0 AND late_fee_vnd >= 0", name="ck_fee_invoices_nonneg"),
        CheckConstraint("status IN ('ISSUED','PAID','OVERDUE','VOID')", name="ck_fee_invoices_status"),
        Index("ix_fee_invoices_payer_status", "payer_user_id", "status"),
        Index(
            "uq_fee_invoices_paid_bank_txn",
            "paid_bank_txn_id",
            unique=True,
            postgresql_where=text("paid_bank_txn_id IS NOT NULL"),
        ),
    )


class InvestorFeeAccrual(Base):
    __tablename__ = "investor_fee_accruals"

    id = _uuid_pk()
    tranche_id = Column(UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="RESTRICT"), nullable=False)
    investor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    period_month = Column(Date, nullable=False)
    received_gross_vnd = Column(BigInteger, nullable=False)
    fee_vnd = Column(BigInteger, nullable=False)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("fee_invoices.id", ondelete="SET NULL"), nullable=True)
    created_at = _ts(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tranche_id", "period_month", name="uq_investor_fee_accruals_tranche_month"),
        CheckConstraint("fee_vnd >= 0", name="ck_investor_fee_accruals_fee"),
    )


# --------------------------------------------------------------------------- #
# Application-layer append-only guards (the DB triggers in the migration are the
# authoritative control; these make a violation fail before it reaches the DB).
# --------------------------------------------------------------------------- #
class AppendOnlyViolation(Exception):
    pass


@event.listens_for(EvidenceLog, "before_update")
@event.listens_for(EvidenceLog, "before_delete")
def _evidence_log_is_append_only(mapper, connection, target):
    raise AppendOnlyViolation("evidence_log is append-only: post a new row instead")


@event.listens_for(CicEvent, "before_delete")
def _cic_events_no_delete(mapper, connection, target):
    raise AppendOnlyViolation("cic_events is append-only: corrections are new events")


@event.listens_for(CicEvent, "before_update")
def _cic_events_only_sent_at(mapper, connection, target):
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(target)
    for attr in state.mapper.column_attrs:
        if attr.key == "sent_at":
            continue
        hist = state.attrs[attr.key].history
        if hist.has_changes():
            raise AppendOnlyViolation(f"cic_events is append-only: only sent_at may change (tried {attr.key})")
    sent_hist = state.attrs["sent_at"].history
    if sent_hist.deleted and sent_hist.deleted[0] is not None:
        raise AppendOnlyViolation("cic_events.sent_at may only go from NULL to a value")
