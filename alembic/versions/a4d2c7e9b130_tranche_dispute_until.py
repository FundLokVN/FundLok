"""tranches.dispute_until: the 30-day dispute after an automatic confirmation (A5)

Spec: docs/specs/direct_transfer/commit-then-accept-flow.md section 4.6 and rule 8. Set to confirmed_at + 30 days when
a ticket is confirmed by the SME's 24-hour silence (or by the system's own receipt check); null otherwise. Additive
and nullable: existing rows keep NULL.

Revision ID: a4d2c7e9b130
Revises: e3b9d7a2c418
Create Date: 2026-11-18 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4d2c7e9b130'
down_revision: Union[str, Sequence[str], None] = 'e3b9d7a2c418'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tranches', sa.Column('dispute_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('tranches', 'dispute_until')
