"""Day 13: each Investor's own loan contract on a commit-then-accept round (Money flow FINAL v1.0)

Spec: docs/specs/direct_transfer/day13-loan-contracts.md. New table window_loan_contracts: generated at 00:00 on
Day 13 from the final pledge list, signed by the SME (enterprise digital signature) on Day 13 and by the Investor
(Zalo one-time code) on Day 14. Additive only. The downgrade refuses while any contract exists.

Revision ID: e3b9d7a2c418
Revises: c5e8a1f3d920
Create Date: 2026-11-18 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e3b9d7a2c418'
down_revision: Union[str, Sequence[str], None] = 'c5e8a1f3d920'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'window_loan_contracts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('window_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('funding_windows.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('investor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('contract_no', sa.Text(), nullable=False, unique=True),
        sa.Column('amount_vnd', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.Text(), server_default='GENERATED', nullable=False),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.Column('file_key', sa.Text(), nullable=False),
        sa.Column('sha256', sa.Text(), nullable=False),
        sa.Column('check_code', sa.Text(), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sme_signed_file_key', sa.Text(), nullable=True),
        sa.Column('sme_signed_sha256', sa.Text(), nullable=True),
        sa.Column('sme_signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sme_signature_check', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('investor_signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('investor_signed_file_key', sa.Text(), nullable=True),
        sa.Column('investor_signature', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.UniqueConstraint('window_id', 'investor_id', name='uq_window_loan_contracts_window_investor'),
        sa.CheckConstraint('amount_vnd > 0', name='ck_window_loan_contracts_amount'),
        sa.CheckConstraint(
            "status IN ('GENERATED','SME_SIGNED','INVESTOR_SIGNED','VOID')", name='ck_window_loan_contracts_status'
        ),
    )


def downgrade() -> None:
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM window_loan_contracts")).scalar()
    if count:
        raise RuntimeError("window_loan_contracts holds signed loan contracts; refusing to drop it")
    op.drop_table('window_loan_contracts')
