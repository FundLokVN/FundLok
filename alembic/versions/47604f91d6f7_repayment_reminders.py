"""repayment reminders

One row per SME per working day the daily repayment reminder was emailed
(or tried), so a doubled or retried Cloud Scheduler call never sends it
twice. See app/scheduled_jobs/ and
docs/specs/scheduled_jobs/cloud-scheduler-jobs.md.

Revision ID: 47604f91d6f7
Revises: 70c7a22db421
Create Date: 2026-11-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '47604f91d6f7'
down_revision: Union[str, None] = '70c7a22db421'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repayment_reminders",
        sa.Column(
            "sme_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("reminder_date", sa.Date(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("from_address", sa.Text(), nullable=True),
        sa.Column("sent_to", sa.Text(), nullable=False),
        sa.Column("due_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("due_vnd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("earlier_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('SENDING', 'SENT', 'FAILED')", name="ck_repayment_reminders_status"
        ),
        sa.CheckConstraint(
            "channel IS NULL OR channel IN ('GMAIL', 'PLATFORM')",
            name="ck_repayment_reminders_channel",
        ),
    )


def downgrade() -> None:
    op.drop_table("repayment_reminders")
