"""commit-then-accept flow: schema foundation (Phase 2 slice S1)

Spec: docs/specs/direct_transfer/commit-then-accept-flow.md section 3; ADR-011.

New tables: commitments, lender_standing, lender_unlock_acknowledgements, acceptance_reminders.
funding_windows: flow and the commit-then-accept deadlines and decision columns; two new statuses
(AWAITING_SME, AWAITING_PAYMENT); the two live-window partial unique indexes are dropped and recreated with the
new live set. investor_agreement_codes: tranche_id becomes nullable, window_id and purpose are added.
No behaviour change: every existing window is flow LEGACY.

Revision ID: b4d8e1a7c953
Revises: 3e9a6c1d7b52
Create Date: 2026-11-16 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4d8e1a7c953'
down_revision: Union[str, Sequence[str], None] = '3e9a6c1d7b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_LIVE = "status IN ('OPEN','FULL','CLOSING','REPAYING')"
NEW_LIVE = "status IN ('OPEN','FULL','CLOSING','AWAITING_SME','AWAITING_PAYMENT','REPAYING')"
OLD_STATUS = "status IN ('OPEN','FULL','CLOSING','REPAYING','FAILED','REPAID','CANCELLED')"
NEW_STATUS = (
    "status IN ('OPEN','FULL','CLOSING','AWAITING_SME','AWAITING_PAYMENT','REPAYING','FAILED','REPAID','CANCELLED')"
)


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _live_indexes(where: str) -> None:
    op.create_index(
        'uq_funding_windows_one_live_per_project', 'funding_windows', ['project_id'], unique=True,
        postgresql_where=sa.text(where),
    )
    op.create_index(
        'uq_funding_windows_one_live_per_sme', 'funding_windows', ['sme_user_id'], unique=True,
        postgresql_where=sa.text(where),
    )


def _drop_live_indexes(where: str) -> None:
    op.drop_index('uq_funding_windows_one_live_per_sme', table_name='funding_windows', postgresql_where=sa.text(where))
    op.drop_index(
        'uq_funding_windows_one_live_per_project', table_name='funding_windows', postgresql_where=sa.text(where)
    )


def upgrade() -> None:
    # --- funding_windows ---
    op.add_column('funding_windows', sa.Column('flow', sa.Text(), nullable=False, server_default='LEGACY'))
    op.add_column('funding_windows', _ts('commit_closes_at', nullable=True))
    op.add_column('funding_windows', _ts('sme_deadline_at', nullable=True))
    op.add_column('funding_windows', _ts('pay_deadline_at', nullable=True))
    op.add_column('funding_windows', sa.Column('sme_decision', sa.Text(), nullable=True))
    op.add_column('funding_windows', _ts('sme_decided_at', nullable=True))
    op.add_column(
        'funding_windows',
        sa.Column(
            'sme_decided_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'),
            nullable=True,
        ),
    )
    op.create_check_constraint('ck_funding_windows_flow', 'funding_windows', "flow IN ('LEGACY','COMMIT_THEN_ACCEPT')")
    op.create_check_constraint(
        'ck_funding_windows_sme_decision', 'funding_windows',
        "sme_decision IN ('ACCEPTED','DECLINED','AUTO_ACCEPTED','AUTO_DECLINED')",
    )
    op.drop_constraint('ck_funding_windows_status', 'funding_windows', type_='check')
    op.create_check_constraint('ck_funding_windows_status', 'funding_windows', NEW_STATUS)
    _drop_live_indexes(OLD_LIVE)
    _live_indexes(NEW_LIVE)

    # --- commitments ---
    op.create_table(
        'commitments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('window_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('investor_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('investor_account_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('investor_type', sa.Text(), nullable=False),
        sa.Column('amount_vnd', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.Text(), server_default='PLEDGED', nullable=False),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.Column('tranche_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('idempotency_key', sa.Text(), nullable=True),
        sa.Column('request_fingerprint', sa.Text(), nullable=True),
        sa.Column('pledge_file_key', sa.Text(), nullable=False),
        sa.Column('pledge_sha256', sa.Text(), nullable=False),
        sa.Column('pledge_certificate', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _ts('committed_at', nullable=False),
        _ts('created_at', server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint("investor_type IN ('INDIVIDUAL','ENTITY')", name='ck_commitments_investor_type'),
        sa.CheckConstraint('amount_vnd > 0', name='ck_commitments_amount'),
        sa.CheckConstraint("status IN ('PLEDGED','FOLDED','VOID')", name='ck_commitments_status'),
        sa.ForeignKeyConstraint(['window_id'], ['funding_windows.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['investor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['investor_account_id'], ['payment_accounts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['tranche_id'], ['tranches.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key'),
    )
    op.create_index('ix_commitments_window_status', 'commitments', ['window_id', 'status'])
    op.create_index('ix_commitments_window_investor', 'commitments', ['window_id', 'investor_id'])

    # --- lender_standing ---
    op.create_table(
        'lender_standing',
        sa.Column('investor_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.Text(), server_default='GOOD', nullable=False),
        sa.Column('no_show_count', sa.Integer(), server_default='0', nullable=False),
        _ts('locked_at', nullable=True),
        sa.Column('locked_window_id', postgresql.UUID(as_uuid=True), nullable=True),
        _ts('unlocked_at', nullable=True),
        _ts('closed_at', nullable=True),
        _ts('updated_at', server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint("status IN ('GOOD','LOCKED','CLOSED')", name='ck_lender_standing_status'),
        sa.ForeignKeyConstraint(['investor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['locked_window_id'], ['funding_windows.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('investor_id'),
    )

    # --- lender_unlock_acknowledgements ---
    op.create_table(
        'lender_unlock_acknowledgements',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('investor_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('locked_window_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('sentence_text', sa.Text(), nullable=False),
        sa.Column('sentence_version', sa.Text(), nullable=False),
        sa.Column('ekyc_verification_id', postgresql.UUID(as_uuid=True), nullable=False),
        _ts('acknowledged_at', nullable=False),
        sa.Column('ip_address', sa.Text(), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['investor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['locked_window_id'], ['funding_windows.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lender_unlock_ack_investor', 'lender_unlock_acknowledgements', ['investor_id'])

    # --- acceptance_reminders ---
    op.create_table(
        'acceptance_reminders',
        sa.Column('window_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('channel', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('sent_to', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        _ts('sent_at', nullable=False),
        sa.CheckConstraint("kind IN ('D_MINUS_3','D_MINUS_1')", name='ck_acceptance_reminders_kind'),
        sa.CheckConstraint("channel IN ('IN_APP','ZALO','EMAIL','SMS')", name='ck_acceptance_reminders_channel'),
        sa.CheckConstraint("status IN ('SENT','FAILED','SKIPPED')", name='ck_acceptance_reminders_status'),
        sa.ForeignKeyConstraint(['window_id'], ['funding_windows.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('window_id', 'kind', 'channel'),
    )

    # --- investor_agreement_codes ---
    op.alter_column('investor_agreement_codes', 'tranche_id', existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column(
        'investor_agreement_codes',
        sa.Column(
            'window_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('funding_windows.id', ondelete='RESTRICT'),
            nullable=True,
        ),
    )
    op.add_column(
        'investor_agreement_codes',
        sa.Column('purpose', sa.Text(), nullable=False, server_default='SERVICE_AGREEMENT'),
    )
    op.create_check_constraint(
        'ck_investor_agreement_codes_purpose', 'investor_agreement_codes',
        "purpose IN ('SERVICE_AGREEMENT','LOAN_CONTRACT')",
    )
    op.create_check_constraint(
        'ck_investor_agreement_codes_loan_contract_tranche', 'investor_agreement_codes',
        "purpose <> 'LOAN_CONTRACT' OR tranche_id IS NOT NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    blocking = bind.execute(
        sa.text(
            "SELECT count(*) FROM funding_windows WHERE flow = 'COMMIT_THEN_ACCEPT' "
            "OR status IN ('AWAITING_SME','AWAITING_PAYMENT')"
        )
    ).scalar_one()
    if blocking:
        raise RuntimeError(
            f"Cannot downgrade: {blocking} commit-then-accept window(s) exist. Their commitments and decisions "
            "would be lost. Finish or remove them first."
        )

    op.drop_constraint('ck_investor_agreement_codes_loan_contract_tranche', 'investor_agreement_codes', type_='check')
    op.drop_constraint('ck_investor_agreement_codes_purpose', 'investor_agreement_codes', type_='check')
    op.drop_column('investor_agreement_codes', 'purpose')
    op.drop_column('investor_agreement_codes', 'window_id')
    # One-time codes are transient; a code with no tranche cannot exist in the old shape.
    op.execute("DELETE FROM investor_agreement_codes WHERE tranche_id IS NULL")
    op.alter_column('investor_agreement_codes', 'tranche_id', existing_type=postgresql.UUID(as_uuid=True), nullable=False)

    op.drop_table('acceptance_reminders')
    op.drop_index('ix_lender_unlock_ack_investor', table_name='lender_unlock_acknowledgements')
    op.drop_table('lender_unlock_acknowledgements')
    op.drop_table('lender_standing')
    op.drop_index('ix_commitments_window_investor', table_name='commitments')
    op.drop_index('ix_commitments_window_status', table_name='commitments')
    op.drop_table('commitments')

    _drop_live_indexes(NEW_LIVE)
    _live_indexes(OLD_LIVE)
    op.drop_constraint('ck_funding_windows_status', 'funding_windows', type_='check')
    op.create_check_constraint('ck_funding_windows_status', 'funding_windows', OLD_STATUS)
    op.drop_constraint('ck_funding_windows_sme_decision', 'funding_windows', type_='check')
    op.drop_constraint('ck_funding_windows_flow', 'funding_windows', type_='check')
    op.drop_column('funding_windows', 'sme_decided_by')
    op.drop_column('funding_windows', 'sme_decided_at')
    op.drop_column('funding_windows', 'sme_decision')
    op.drop_column('funding_windows', 'pay_deadline_at')
    op.drop_column('funding_windows', 'sme_deadline_at')
    op.drop_column('funding_windows', 'commit_closes_at')
    op.drop_column('funding_windows', 'flow')
