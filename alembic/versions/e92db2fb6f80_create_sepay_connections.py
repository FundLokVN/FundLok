"""create sepay_connections

One row per user who connected their SePay account over OAuth2
(docs/specs/oauth/sepay-connect.md, ADR-011). Tokens are stored encrypted.

Revision ID: e92db2fb6f80
Revises: 099de0f5b480
Create Date: 2026-10-30 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'e92db2fb6f80'
down_revision: Union[str, None] = '099de0f5b480'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sepay_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('access_token', sa.Text(), nullable=False),
        sa.Column('refresh_token', sa.Text(), nullable=False),
        sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('refresh_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), server_default='ACTIVE', nullable=False),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_refreshed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint("status IN ('ACTIVE','NEEDS_RECONNECT')", name='ck_sepay_connections_status'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )


def downgrade() -> None:
    op.drop_table('sepay_connections')
