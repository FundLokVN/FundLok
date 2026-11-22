"""Personal Guarantor signs by Zalo one-time code (Money flow FINAL v1.0)

Spec: docs/specs/loans/guarantor-zalo-otp.md. New tables only: guarantor_signing_codes (the codes sent) and
guarantor_signatures (what each code signed). Additive: no existing row changes.

Revision ID: c5e8a1f3d920
Revises: d14a7c2e5b31
Create Date: 2026-11-18 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e8a1f3d920'
down_revision: Union[str, Sequence[str], None] = 'd14a7c2e5b31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PURPOSE = "purpose IN ('LISTING_DOCUMENTS','WINDOW_LOAN_CONTRACTS')"


def upgrade() -> None:
    op.create_table(
        'guarantor_signing_codes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('subject_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('phone', sa.Text(), nullable=False),
        sa.Column('code_hash', sa.Text(), nullable=False),
        sa.Column('channel', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(_PURPOSE, name='ck_guarantor_signing_codes_purpose'),
    )
    op.create_index('ix_guarantor_signing_codes_user_created', 'guarantor_signing_codes', ['user_id', 'created_at'])
    op.create_table(
        'guarantor_signatures',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('subject_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('signer_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('signer_name', sa.Text(), nullable=True),
        sa.Column('signer_id_number', sa.Text(), nullable=False),
        sa.Column('phone', sa.Text(), nullable=False),
        sa.Column('code_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('guarantor_signing_codes.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('documents', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ip_address', sa.Text(), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(_PURPOSE, name='ck_guarantor_signatures_purpose'),
        sa.UniqueConstraint('purpose', 'subject_id', name='uq_guarantor_signatures_purpose_subject'),
    )


def downgrade() -> None:
    op.drop_table('guarantor_signatures')
    op.drop_index('ix_guarantor_signing_codes_user_created', table_name='guarantor_signing_codes')
    op.drop_table('guarantor_signing_codes')
