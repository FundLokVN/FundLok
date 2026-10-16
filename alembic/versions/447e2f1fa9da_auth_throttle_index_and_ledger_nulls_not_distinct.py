"""auth throttle index + ledger_accounts NULLS NOT DISTINCT

Two fixes from the 2026-10-16 vbsec scan:

* ix_audit_logs_action_created_at: the failed sign-in, failed 2FA and
  verification-resend limits each count one action's rows over a recent
  window. Without an index every such count scanned the whole audit log.

* uq_ledger_accounts_role_party_contract becomes NULLS NOT DISTINCT. The
  platform-level accounts (OMNIBUS_CASH, FL_REVENUE) have owner_user_id and
  contract_id NULL, and with NULLs treated as distinct the constraint never
  stopped a duplicate of them, so two concurrent first postings could create
  two OMNIBUS_CASH rows. Requires PostgreSQL 15+.

Revision ID: 447e2f1fa9da
Revises: 707c1b468a42
Create Date: 2026-10-16 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '447e2f1fa9da'
down_revision: Union[str, None] = '707c1b468a42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEDGER_UQ = 'uq_ledger_accounts_role_party_contract'
_LEDGER_UQ_COLUMNS = ['account_type', 'custodial_account_id', 'owner_user_id', 'contract_id']


def upgrade() -> None:
    op.create_index(
        'ix_audit_logs_action_created_at', 'audit_logs', ['action', 'created_at'], unique=False
    )

    bind = op.get_bind()
    version = bind.execute(sa.text("SHOW server_version_num")).scalar()
    if int(version) < 150000:
        raise RuntimeError(
            f"NULLS NOT DISTINCT needs PostgreSQL 15 or later; this server is {version}."
        )

    # Ledger rows are append-only and reference these accounts, so duplicates
    # cannot be merged here. Refuse loudly instead of failing on the constraint.
    duplicates = bind.execute(
        sa.text(
            "SELECT account_type, custodial_account_id, owner_user_id, contract_id, count(*) "
            "FROM ledger_accounts "
            "GROUP BY account_type, custodial_account_id, owner_user_id, contract_id "
            "HAVING count(*) > 1"
        )
    ).fetchall()
    if duplicates:
        raise RuntimeError(
            "ledger_accounts already holds duplicate accounts; resolve them by hand "
            f"before this migration: {[tuple(row) for row in duplicates]}"
        )

    op.drop_constraint(_LEDGER_UQ, 'ledger_accounts', type_='unique')
    op.create_unique_constraint(
        _LEDGER_UQ, 'ledger_accounts', _LEDGER_UQ_COLUMNS, postgresql_nulls_not_distinct=True
    )


def downgrade() -> None:
    op.drop_constraint(_LEDGER_UQ, 'ledger_accounts', type_='unique')
    op.create_unique_constraint(_LEDGER_UQ, 'ledger_accounts', _LEDGER_UQ_COLUMNS)

    op.drop_index('ix_audit_logs_action_created_at', table_name='audit_logs')
