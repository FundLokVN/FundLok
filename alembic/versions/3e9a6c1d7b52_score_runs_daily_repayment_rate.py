"""score_runs.daily_repayment_rate

HANDOFF-06 T1: the locked score run keeps the daily repayment rate (target daily repayment / average daily revenue, a
fraction rounded to 4 decimals, i.e. 2 decimals of a percent) so the Offer and Loan Contract can read it as the revenue
share s. Existing runs stay NULL: their rate was computed at 22 working days and is not back-filled.

Revision ID: 3e9a6c1d7b52
Revises: 6664b74ae183
Create Date: 2026-11-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3e9a6c1d7b52"
down_revision: Union[str, None] = "6664b74ae183"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("score_runs", sa.Column("daily_repayment_rate", sa.Numeric(7, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("score_runs", "daily_repayment_rate")
