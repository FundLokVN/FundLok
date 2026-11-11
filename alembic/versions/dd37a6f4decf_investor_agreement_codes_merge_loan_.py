"""investor agreement codes; merge loan signing and phone verification heads

The one-time codes investors confirm the Investor Service Agreement (Contract 4) with, sent to their phone by Zalo:
docs/specs/market/investor-service-agreement.md (v2.0). Also joins the two migration chains that both grew from
0538f8bc779a: listing signing (b8e2d6f4a1c7) and phone verification with the Zalo OA tokens (efd8a69b9406), which
this feature needs.

Revision ID: dd37a6f4decf
Revises: b8e2d6f4a1c7, efd8a69b9406
Create Date: 2026-11-11 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'dd37a6f4decf'
down_revision: Union[str, Sequence[str], None] = ('b8e2d6f4a1c7', 'efd8a69b9406')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "investor_agreement_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tranche_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tranches.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_investor_agreement_codes_investor_created", "investor_agreement_codes", ["investor_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_investor_agreement_codes_investor_created", table_name="investor_agreement_codes")
    op.drop_table("investor_agreement_codes")
