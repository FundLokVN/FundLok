"""repayment zalo reminders

One row per investor ticket per working day the SME was sent the Zalo
repayment reminder (or tried), so a doubled or retried Cloud Scheduler call
never sends (and pays for) it twice. See app/scheduled_jobs/zalo_reminders.py
and docs/specs/scheduled_jobs/cloud-scheduler-jobs.md.

Revision ID: 5b8e2c1d9f40
Revises: 47604f91d6f7
Create Date: 2026-11-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '5b8e2c1d9f40'
down_revision: Union[str, None] = '47604f91d6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repayment_zalo_reminders",
        sa.Column(
            "tranche_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tranches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("reminder_date", sa.Date(), primary_key=True),
        sa.Column(
            "sme_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("sent_to", sa.Text(), nullable=False),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("days_left", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('SENDING', 'SENT', 'FAILED')", name="ck_repayment_zalo_reminders_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("repayment_zalo_reminders")
