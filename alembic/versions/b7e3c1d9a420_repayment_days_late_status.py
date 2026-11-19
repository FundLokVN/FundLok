"""repayment_days: LATE status (a day with no payment proof by 18:00 ICT on its due date)

Spec: docs/specs/repayment_schedule/late-status.md section 3 (AM-P8). The status CHECK gains 'LATE'; two nullable
columns record when a day was marked late and when proof later cleared it. No backfill: existing rows keep their
status (rows presumed paid without proof before this change keep their ledger legs; the ledger is append-only).

Revision ID: b7e3c1d9a420
Revises: a4d2c7e9b130
Create Date: 2026-11-19 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3c1d9a420'
down_revision: Union[str, Sequence[str], None] = 'a4d2c7e9b130'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ck_repayment_days_status', 'repayment_days', type_='check')
    op.create_check_constraint(
        'ck_repayment_days_status', 'repayment_days',
        "status IN ('DUE','LATE','PRESUMED_PAID','REPORTED_MISSED','MATCHED')",
    )
    op.add_column('repayment_days', sa.Column('late_marked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('repayment_days', sa.Column('late_cleared_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    late = op.get_bind().execute(sa.text("SELECT count(*) FROM repayment_days WHERE status = 'LATE'")).scalar()
    if late:
        raise RuntimeError(f"{late} repayment day(s) are LATE: settle or reset them before downgrading")
    op.drop_column('repayment_days', 'late_cleared_at')
    op.drop_column('repayment_days', 'late_marked_at')
    op.drop_constraint('ck_repayment_days_status', 'repayment_days', type_='check')
    op.create_check_constraint(
        'ck_repayment_days_status', 'repayment_days', "status IN ('DUE','PRESUMED_PAID','REPORTED_MISSED','MATCHED')"
    )
