"""extension_filings: the SME's monthly revenue filing and the automatic extension it gave (D-042)

Spec: docs/specs/repayment_schedule/automatic-extension.md. One row per round and month. When the shortfall test
passes, the row records the extension (delta_days, new start date) and keeps the replaced schedule rows
(`previous_rows`) so the old version stays readable. Additive only.

Revision ID: c2f8a6d4e917
Revises: b7e3c1d9a420
Create Date: 2026-11-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c2f8a6d4e917'
down_revision: Union[str, Sequence[str], None] = 'b7e3c1d9a420'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'extension_filings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('window_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('funding_windows.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('sme_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('period_month', sa.Date(), nullable=False),
        sa.Column('evidence_kind', sa.Text(), nullable=False),
        sa.Column('evidence_file_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('evidence_files.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('revenue_vnd', sa.BigInteger(), nullable=False),
        sa.Column('revenue_share', sa.Numeric(7, 4), nullable=False),
        sa.Column('due_vnd', sa.BigInteger(), nullable=False),
        sa.Column('shortfall_vnd', sa.BigInteger(), nullable=False),
        sa.Column('delta_days', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('previous_rows', postgresql.JSONB(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('filed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("evidence_kind IN ('TAX_DECLARATION','BANK_INFLOW')", name='ck_extension_filings_evidence_kind'),
        sa.CheckConstraint("status IN ('EXTENDED','NO_SHORTFALL','NO_ROOM')", name='ck_extension_filings_status'),
        sa.CheckConstraint('revenue_vnd >= 0 AND delta_days >= 0', name='ck_extension_filings_amounts'),
        sa.UniqueConstraint('window_id', 'period_month', name='uq_extension_filings_window_month'),
    )


def downgrade() -> None:
    op.drop_table('extension_filings')
