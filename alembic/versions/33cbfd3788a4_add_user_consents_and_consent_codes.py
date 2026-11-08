"""add user_consents and consent_codes (sign-up Terms & data consent)

Revision ID: 33cbfd3788a4
Revises: c4e8a1f7b2d9
Create Date: 2026-11-08 10:00:00.000000

Contract 1 (Terms & Services and data consent) is accepted at sign-up with
three tick boxes and a one-time code, and each acceptance is kept as evidence:
one user_consents row per acceptance, the three boxes together in a JSONB
`consents` column whose shape the database checks.
Spec: docs/specs/consent/sign-up-terms-consent.md
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '33cbfd3788a4'
down_revision: Union[str, None] = 'c4e8a1f7b2d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_consents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Text(), nullable=False),
        sa.Column('consents', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('verified_by', sa.Text(), nullable=False),
        sa.Column('ip_address', sa.Text(), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(consents) = 'object'"
            " AND consents ?& ARRAY['TERMS', 'DATA_PROCESSING', 'DATA_SHARING_TPV']"
            " AND jsonb_typeof(consents -> 'TERMS') = 'boolean'"
            " AND jsonb_typeof(consents -> 'DATA_PROCESSING') = 'boolean'"
            " AND jsonb_typeof(consents -> 'DATA_SHARING_TPV') = 'boolean'"
            " AND consents - ARRAY['TERMS', 'DATA_PROCESSING', 'DATA_SHARING_TPV'] = '{}'::jsonb",
            name='ck_user_consents_consents_shape',
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_user_consents_user_version_created', 'user_consents', ['user_id', 'version', 'created_at'], unique=False)
    op.create_table(
        'consent_codes',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('code_hash', sa.Text(), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_consent_codes_user_created', 'consent_codes', ['user_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_consent_codes_user_created', table_name='consent_codes')
    op.drop_table('consent_codes')
    op.drop_index('ix_user_consents_user_version_created', table_name='user_consents')
    op.drop_table('user_consents')
