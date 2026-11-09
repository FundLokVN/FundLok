"""add investor_service_agreements (Contract 4, signed and uploaded with the first commitment)

Revision ID: 918db72f448e
Revises: f736e64d867c
Create Date: 2026-11-08 23:30:00.000000

Contract 4 (Investor Service Agreement) is signed with the investor's first
commitment: the commitment is made (amount held, code given, no money moved),
then the agreement is downloaded as a PDF filled in with the investor's
details and that commitment's Annex, signed with their digital signature and
uploaded back; the payment details are refused until then. Later commitments
sign only their own Annex (investment_confirmations). One row per investor per
version: when, which version, the commitment it was signed with, Party B as
filled in, and the signed file's storage key, hash, name and size. Additive
only; the app never updates or deletes these rows.
Spec: docs/specs/market/investor-service-agreement.md
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '918db72f448e'
down_revision: Union[str, None] = 'f736e64d867c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'investor_service_agreements',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('investor_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Text(), nullable=False),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tranche_id', sa.UUID(), nullable=True),
        sa.Column('party_b', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('signed_file_key', sa.Text(), nullable=False),
        sa.Column('document_sha256', sa.Text(), nullable=False),
        sa.Column('file_name', sa.Text(), nullable=True),
        sa.Column('file_size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('ip_address', sa.Text(), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['investor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['tranche_id'], ['tranches.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('investor_id', 'version', name='uq_investor_service_agreements_investor_version'),
    )


def downgrade() -> None:
    op.drop_table('investor_service_agreements')
