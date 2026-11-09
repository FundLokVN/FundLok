"""add investment_confirmations (each commitment's filled-in Annex of Contract 4)

Revision ID: ee32119154bd
Revises: 918db72f448e
Create Date: 2026-11-09 10:00:00.000000

Every commitment's Investment Confirmation (the Annex of the Investor Service
Agreement) is filled in and kept when the investor agrees to it, before the
payment details are shown: the Annex values, the filled-in PDF in evidence
storage with its hash, and the signed agreement it is part of. One row per
tranche; the commitment's signed acceptance names it. Additive only; the app
never updates or deletes these rows.
Spec: docs/specs/market/investor-service-agreement.md
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ee32119154bd'
down_revision: Union[str, None] = '918db72f448e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'investment_confirmations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tranche_id', sa.UUID(), nullable=False),
        sa.Column('investor_id', sa.UUID(), nullable=False),
        sa.Column('agreement_id', sa.UUID(), nullable=False),
        sa.Column('listing_id', sa.UUID(), nullable=False),
        sa.Column('sme_name', sa.Text(), nullable=False),
        sa.Column('amount_vnd', sa.BigInteger(), nullable=False),
        sa.Column('annual_rate_pct', sa.Numeric(precision=6, scale=3), nullable=False),
        sa.Column('term_months', sa.Integer(), nullable=False),
        sa.Column('service_fee_vnd', sa.BigInteger(), nullable=False),
        sa.Column('service_fee_bps_per_year', sa.Integer(), nullable=False),
        sa.Column('expected_disbursement_date', sa.Date(), nullable=False),
        sa.Column('file_key', sa.Text(), nullable=False),
        sa.Column('document_sha256', sa.Text(), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agreement_id'], ['investor_service_agreements.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['investor_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['tranche_id'], ['tranches.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tranche_id'),
    )
    op.create_index(
        op.f('ix_investment_confirmations_investor_id'), 'investment_confirmations', ['investor_id'], unique=False
    )


def downgrade() -> None:
    # Dropping the table drops its index with it.
    op.drop_table('investment_confirmations')
