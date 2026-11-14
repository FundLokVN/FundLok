"""fee invoice notices

One row per fee invoice emailed (or being emailed) to its payer, so the
SME's platform activation-fee invoice is sent once and a failed send is
retried. See app/admin/fee_notice/ and docs/specs/admin/fee-invoice-email.md.

Revision ID: 70c7a22db421
Revises: dd37a6f4decf
Create Date: 2026-11-12 16:20:20.219292

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '70c7a22db421'
down_revision: Union[str, None] = 'dd37a6f4decf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fee_invoice_notices",
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fee_invoices.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("from_address", sa.Text(), nullable=True),
        sa.Column("sent_to", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('SENDING', 'SENT', 'FAILED')", name="ck_fee_invoice_notices_status"
        ),
        sa.CheckConstraint(
            "channel IS NULL OR channel IN ('GMAIL', 'PLATFORM')",
            name="ck_fee_invoice_notices_channel",
        ),
    )


def downgrade() -> None:
    op.drop_table("fee_invoice_notices")
