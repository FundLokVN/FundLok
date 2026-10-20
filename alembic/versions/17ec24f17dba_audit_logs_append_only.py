"""audit_logs append only (compliance audit SEC-02)

Makes `audit_logs` append-only at the database layer, as the ledger,
state_transition and score_run_inputs already are:

1. A row trigger refuses every UPDATE and DELETE, whoever runs it -- the
   table owner included. Its own guard function rather than
   ledger_immutability_guard(), whose message tells you to post a
   reversing transaction, which means nothing for an audit row.
2. UPDATE and DELETE are revoked from the role running this migration
   (in every deployed environment, the role the app connects as) and from
   PUBLIC. A table owner keeps the right to re-grant them, so this is a
   second lock, not a replacement for the trigger; real separation needs
   the app to connect as a role that does not own the table.

TRUNCATE stays possible on purpose. Row triggers do not fire on it, as with
the ledger, so the test suite's per-test TRUNCATE, scripts/seed_data.sql and
the guarded production reset (scripts/reset_prod_data.sql) keep working.

Consequence: a user who appears as `actor_id` on audit rows can no longer be
deleted. The FK's ON DELETE SET NULL would have to UPDATE those rows, and
the trigger refuses it. Nothing in the app deletes users today; an erasure
request has to anonymise the user row instead of deleting it.

Revision ID: 17ec24f17dba
Revises: 447e2f1fa9da
Create Date: 2026-10-20 13:48:39.178001

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '17ec24f17dba'
down_revision: Union[str, None] = '447e2f1fa9da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_immutability_guard()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                '% on % is not allowed: the audit log is append-only.',
                TG_OP, TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_log_immutability_guard();
        """
    )
    op.execute("REVOKE UPDATE, DELETE ON audit_logs FROM PUBLIC, CURRENT_USER;")


def downgrade() -> None:
    op.execute("GRANT UPDATE, DELETE ON audit_logs TO CURRENT_USER;")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs;")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutability_guard();")
