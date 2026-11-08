"""add investor_service_agreements (Contract 4, signed and uploaded before the first commitment)

Revision ID: 918db72f448e
Revises: f736e64d867c
Create Date: 2026-11-08 23:30:00.000000

Contract 4 (Investor Service Agreement) is downloaded as a PDF filled in with
the investor's details, signed with their digital signature and uploaded back
before their first commitment; later commitments sign only its Annex
(Investment Confirmation), carried in their own signed acceptance. One row per
signed upload is the record: when, which version, Party B as filled in, and
the signed file's storage key, hash, name and size. The latest row for a
version counts. Additive only; the app never updates or deletes these rows.
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
        sa.Column('party_b', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('signed_file_key', sa.Text(), nullable=False),
        sa.Column('document_sha256', sa.Text(), nullable=False),
        sa.Column('file_name', sa.Text(), nullable=True),
        sa.Column('file_size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('ip_address', sa.Text(), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['investor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_investor_service_agreements_investor_version_signed',
        'investor_service_agreements',
        ['investor_id', 'version', 'signed_at'],
        unique=False,
    )


def downgrade() -> None:
    # Dropping the table drops its index with it.
    op.drop_table('investor_service_agreements')
