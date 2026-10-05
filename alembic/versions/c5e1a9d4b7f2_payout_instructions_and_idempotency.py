"""Payout instructions, API idempotency records, ledger account uniqueness fix.

Adds the tables that make an outbound bank transfer exactly-once under
retries, timeouts and double clicks (spec
docs/specs/payments/payout-idempotency.md):

  - idempotency_records   one row per (actor, endpoint, Idempotency-Key)
  - payout_beneficiaries  verified destination registry
  - payout_instructions   one row per intended transfer; partial UNIQUE index
                          = at most one live disbursement per contract;
                          client_request_id UNIQUE and immutable
  - payout_events         append-only audit trail

DB-level guarantees (they hold even for hand-written SQL):
  - payout_instruction_guard(): BEFORE UPDATE/DELETE on payout_instructions.
    Amount, destination, client_request_id, purpose, contract, maker are
    immutable; approval and ledger link are write-once; status may move only
    along the allowed transition table; rows are never deleted.
  - payout_instruction_log_status(): AFTER INSERT/UPDATE writes a
    STATUS_CHANGED row to payout_events for every status change, with the
    acting user from the transaction-local setting `fundlok.actor_id`.
  - payout_events reuses ledger_immutability_guard() (append-only).

Also fixes uq_ledger_accounts_role_party_contract: PostgreSQL treats NULLs as
distinct, so the platform-level accounts (owner_user_id and contract_id both
NULL -- OMNIBUS_CASH, FL_REVENUE, SUSPENSE) were never protected and a
concurrent first use could create two OMNIBUS_CASH rows. Recreated as
UNIQUE NULLS NOT DISTINCT (PostgreSQL 15+). The upgrade refuses to run if
duplicates already exist, because ledger rows are append-only and cannot be
merged automatically.

Revision ID: c5e1a9d4b7f2
Revises: 7b722006b0d4
Create Date: 2026-10-05 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "c5e1a9d4b7f2"
down_revision: Union[str, None] = "7b722006b0d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PAYOUT_STATUSES = (
    "'PENDING_APPROVAL', 'APPROVED', 'SUBMITTING', 'SUBMITTED', 'UNKNOWN', "
    "'MANUAL_REVIEW', 'SUCCEEDED', 'FAILED', 'CANCELLED'"
)


def upgrade() -> None:
    # --- ledger_accounts: UNIQUE ... NULLS NOT DISTINCT --------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF current_setting('server_version_num')::int < 150000 THEN
                RAISE EXCEPTION 'PostgreSQL 15+ is required (UNIQUE NULLS NOT DISTINCT)';
            END IF;
            IF EXISTS (
                SELECT 1 FROM ledger_accounts
                GROUP BY account_type, custodial_account_id, owner_user_id, contract_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Duplicate ledger_accounts rows exist (same type/custodial/owner/contract). '
                    'Resolve them with a reviewed correction before running this migration.';
            END IF;
        END $$;
        """
    )
    op.drop_constraint("uq_ledger_accounts_role_party_contract", "ledger_accounts", type_="unique")
    op.execute(
        "ALTER TABLE ledger_accounts ADD CONSTRAINT uq_ledger_accounts_role_party_contract "
        "UNIQUE NULLS NOT DISTINCT (account_type, custodial_account_id, owner_user_id, contract_id)"
    )

    # --- idempotency_records ------------------------------------------------
    op.create_table(
        "idempotency_records",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now() + interval '30 days'"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(idempotency_key) BETWEEN 8 AND 128", name="ck_idempotency_records_key_length"),
        sa.CheckConstraint("char_length(request_hash) = 64", name="ck_idempotency_records_request_hash"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "scope", "idempotency_key", name="uq_idempotency_records_actor_scope_key"),
    )
    op.create_index("ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"])

    # --- payout_beneficiaries ----------------------------------------------
    op.create_table(
        "payout_beneficiaries",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("bank_bin", sa.Text(), nullable=False),
        sa.Column("account_number", sa.Text(), nullable=False),
        sa.Column("account_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="PENDING_VERIFICATION", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING_VERIFICATION', 'VERIFIED', 'REVOKED')", name="ck_payout_beneficiaries_status"
        ),
        sa.CheckConstraint("bank_bin ~ '^[0-9]{6}$'", name="ck_payout_beneficiaries_bank_bin"),
        sa.CheckConstraint("account_number ~ '^[0-9A-Za-z]{4,34}$'", name="ck_payout_beneficiaries_account_number"),
        sa.CheckConstraint(
            "status <> 'VERIFIED' OR (verified_at IS NOT NULL AND verified_by IS NOT NULL)",
            name="ck_payout_beneficiaries_verified_fields",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verified_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "bank_bin", "account_number", name="uq_payout_beneficiaries_owner_account"
        ),
    )

    # --- payout_instructions -----------------------------------------------
    op.create_table(
        "payout_instructions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_request_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("contract_id", UUID(as_uuid=True), nullable=True),
        sa.Column("custodial_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("beneficiary_id", UUID(as_uuid=True), nullable=False),
        sa.Column("beneficiary_bank_bin", sa.Text(), nullable=False),
        sa.Column("beneficiary_account_number", sa.Text(), nullable=False),
        sa.Column("beneficiary_account_name", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 0), nullable=False),
        sa.Column("currency", sa.Text(), server_default="VND", nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="PENDING_APPROVAL", nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bank_transaction_ref", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("ledger_transaction_id", UUID(as_uuid=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint("purpose IN ('SME_DISBURSEMENT')", name="ck_payout_instructions_purpose"),
        sa.CheckConstraint(f"status IN ({PAYOUT_STATUSES})", name="ck_payout_instructions_status"),
        sa.CheckConstraint("amount > 0", name="ck_payout_instructions_amount_positive"),
        sa.CheckConstraint("currency = 'VND'", name="ck_payout_instructions_currency"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_payout_instructions_attempt_count"),
        sa.CheckConstraint(
            "client_request_id ~ '^[A-Z0-9]{8,60}$'", name="ck_payout_instructions_client_request_id"
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 140", name="ck_payout_instructions_description"
        ),
        sa.CheckConstraint(
            "purpose <> 'SME_DISBURSEMENT' OR contract_id IS NOT NULL",
            name="ck_payout_instructions_disbursement_contract",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'CANCELLED') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_payout_instructions_approved_before_send",
        ),
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> created_by", name="ck_payout_instructions_maker_ne_checker"
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR ledger_transaction_id IS NOT NULL",
            name="ck_payout_instructions_succeeded_has_ledger",
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["custodial_account_id"], ["custodial_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["beneficiary_id"], ["payout_beneficiaries.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cancelled_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ledger_transaction_id"], ["ledger_transactions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id"),
        sa.UniqueConstraint("bank_transaction_ref"),
        sa.UniqueConstraint("ledger_transaction_id"),
    )
    op.create_index(
        "uq_payout_instructions_active_disbursement",
        "payout_instructions",
        ["contract_id"],
        unique=True,
        postgresql_where=sa.text("purpose = 'SME_DISBURSEMENT' AND status NOT IN ('FAILED', 'CANCELLED')"),
    )
    op.create_index(
        "ix_payout_instructions_status_next_check", "payout_instructions", ["status", "next_check_at"]
    )

    # --- payout_events (append-only) ----------------------------------------
    op.create_table(
        "payout_events",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("instruction_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=True),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("bank_code", sa.Text(), nullable=True),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("actor_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('STATUS_CHANGED', 'SUBMIT_RESULT', 'INQUIRY_RESULT', 'OUTCOME_APPLIED')",
            name="ck_payout_events_type",
        ),
        sa.ForeignKeyConstraint(["instruction_id"], ["payout_instructions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payout_events_instruction_created", "payout_events", ["instruction_id", "created_at"])
    op.execute(
        """
        CREATE TRIGGER trg_payout_events_append_only
        BEFORE UPDATE OR DELETE ON payout_events
        FOR EACH ROW EXECUTE FUNCTION ledger_immutability_guard();
        """
    )

    # --- payout_instructions guard: immutability + transition table --------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION payout_instruction_guard()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'DELETE on payout_instructions is not allowed: payout instructions are permanent records';
            END IF;

            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.client_request_id IS DISTINCT FROM OLD.client_request_id
               OR NEW.purpose IS DISTINCT FROM OLD.purpose
               OR NEW.contract_id IS DISTINCT FROM OLD.contract_id
               OR NEW.custodial_account_id IS DISTINCT FROM OLD.custodial_account_id
               OR NEW.beneficiary_id IS DISTINCT FROM OLD.beneficiary_id
               OR NEW.beneficiary_bank_bin IS DISTINCT FROM OLD.beneficiary_bank_bin
               OR NEW.beneficiary_account_number IS DISTINCT FROM OLD.beneficiary_account_number
               OR NEW.beneficiary_account_name IS DISTINCT FROM OLD.beneficiary_account_name
               OR NEW.amount IS DISTINCT FROM OLD.amount
               OR NEW.currency IS DISTINCT FROM OLD.currency
               OR NEW.description IS DISTINCT FROM OLD.description
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'payout_instructions %: amount, destination, request id and maker are immutable', OLD.id;
            END IF;

            IF OLD.approved_by IS NOT NULL
               AND (NEW.approved_by IS DISTINCT FROM OLD.approved_by
                    OR NEW.approved_at IS DISTINCT FROM OLD.approved_at) THEN
                RAISE EXCEPTION 'payout_instructions %: approval is write-once', OLD.id;
            END IF;

            IF OLD.ledger_transaction_id IS NOT NULL
               AND NEW.ledger_transaction_id IS DISTINCT FROM OLD.ledger_transaction_id THEN
                RAISE EXCEPTION 'payout_instructions %: ledger link is write-once', OLD.id;
            END IF;

            IF NEW.attempt_count < OLD.attempt_count THEN
                RAISE EXCEPTION 'payout_instructions %: attempt_count cannot decrease', OLD.id;
            END IF;

            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                   (OLD.status = 'PENDING_APPROVAL' AND NEW.status IN ('APPROVED', 'CANCELLED'))
                OR (OLD.status = 'APPROVED'         AND NEW.status IN ('SUBMITTING', 'CANCELLED'))
                OR (OLD.status = 'SUBMITTING'       AND NEW.status IN ('SUBMITTED', 'SUCCEEDED', 'FAILED', 'UNKNOWN', 'APPROVED'))
                OR (OLD.status = 'SUBMITTED'        AND NEW.status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN', 'MANUAL_REVIEW'))
                OR (OLD.status = 'UNKNOWN'          AND NEW.status IN ('SUBMITTED', 'SUCCEEDED', 'FAILED', 'APPROVED', 'MANUAL_REVIEW'))
                OR (OLD.status = 'MANUAL_REVIEW'    AND NEW.status IN ('SUCCEEDED', 'FAILED'))
            ) THEN
                RAISE EXCEPTION 'payout_instructions %: invalid status transition % -> %', OLD.id, OLD.status, NEW.status;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payout_instructions_guard
        BEFORE UPDATE OR DELETE ON payout_instructions
        FOR EACH ROW EXECUTE FUNCTION payout_instruction_guard();
        """
    )

    # --- payout_instructions status log --------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION payout_instruction_log_status()
        RETURNS TRIGGER AS $$
        DECLARE
            actor UUID := NULLIF(current_setting('fundlok.actor_id', true), '')::uuid;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO payout_events (id, instruction_id, event_type, attempt_no, from_status, to_status, actor_id, created_at)
                VALUES (gen_random_uuid(), NEW.id, 'STATUS_CHANGED', NEW.attempt_count, NULL, NEW.status, actor, now());
            ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
                INSERT INTO payout_events (id, instruction_id, event_type, attempt_no, from_status, to_status, actor_id, created_at)
                VALUES (gen_random_uuid(), NEW.id, 'STATUS_CHANGED', NEW.attempt_count, OLD.status, NEW.status, actor, now());
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payout_instructions_log_status
        AFTER INSERT OR UPDATE ON payout_instructions
        FOR EACH ROW EXECUTE FUNCTION payout_instruction_log_status();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_payout_instructions_log_status ON payout_instructions;")
    op.execute("DROP FUNCTION IF EXISTS payout_instruction_log_status();")
    op.execute("DROP TRIGGER IF EXISTS trg_payout_instructions_guard ON payout_instructions;")
    op.execute("DROP FUNCTION IF EXISTS payout_instruction_guard();")
    op.execute("DROP TRIGGER IF EXISTS trg_payout_events_append_only ON payout_events;")

    op.drop_index("ix_payout_events_instruction_created", table_name="payout_events")
    op.drop_table("payout_events")
    op.drop_index("ix_payout_instructions_status_next_check", table_name="payout_instructions")
    op.drop_index("uq_payout_instructions_active_disbursement", table_name="payout_instructions")
    op.drop_table("payout_instructions")
    op.drop_table("payout_beneficiaries")
    op.drop_index("ix_idempotency_records_expires_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")

    op.drop_constraint("uq_ledger_accounts_role_party_contract", "ledger_accounts", type_="unique")
    op.create_unique_constraint(
        "uq_ledger_accounts_role_party_contract",
        "ledger_accounts",
        ["account_type", "custodial_account_id", "owner_user_id", "contract_id"],
    )
