"""add NEAC signature check to investor_service_agreements

Revision ID: 0538f8bc779a
Revises: ee32119154bd
Create Date: 2026-11-09 11:00:00.000000

A signed Investor Service Agreement is accepted only once NEAC (the National
Electronic Authentication Centre's own verification) says the signature is
valid, the document unchanged and the certificate not revoked, and the
signer's ID is the investor's. NEAC's verdict is kept with the agreement.
Nullable: records made outside the upload have none. Additive only.
Spec: docs/specs/market/investor-service-agreement.md
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0538f8bc779a'
down_revision: Union[str, None] = 'ee32119154bd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'investor_service_agreements',
        sa.Column('signature_check', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'investor_service_agreements',
        sa.Column('signature_checked_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('investor_service_agreements', 'signature_checked_at')
    op.drop_column('investor_service_agreements', 'signature_check')
