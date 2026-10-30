"""approve payment accounts on save

Payment accounts no longer wait for staff review: POST /payment-accounts saves them APPROVED, with
approved_by NULL. This approves the accounts already saved while review was still the rule, the same
way (approved_at = now, approved_by left NULL), so every account follows one rule. Data only.

Downgrade puts back to PENDING the accounts no member of staff decided (approved_by IS NULL), which is
exactly the set this upgrade and the new save path approve.

Revision ID: 099de0f5b480
Revises: 6fad6630401b
Create Date: 2026-10-29 15:46:03.370445

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '099de0f5b480'
down_revision: Union[str, None] = '6fad6630401b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE payment_accounts SET status = 'APPROVED', approved_at = now() "
        "WHERE status = 'PENDING'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE payment_accounts SET status = 'PENDING', approved_at = NULL "
        "WHERE status = 'APPROVED' AND approved_by IS NULL"
    )
