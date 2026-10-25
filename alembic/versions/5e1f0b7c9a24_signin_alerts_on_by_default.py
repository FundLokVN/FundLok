"""sign-in alerts on by default

b8d41c60ea92 made the "a new device signed in" email opt-in, so almost nobody
got it: a warning that has to be found in settings first protects only the
people who least need it. New accounts now start with it on.

Existing accounts are switched on too, unless the owner has made a choice.
Every change through PATCH /users/me/security-preferences writes a
SIGNIN_ALERTS_CHANGED audit row, so an account with no such row is still on
the old default and is moved to the new one; an account with a row is left
exactly as its owner set it -- someone who switched alerts off keeps them off.

Revision ID: 5e1f0b7c9a24
Revises: 17ec24f17dba
Create Date: 2026-10-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = '5e1f0b7c9a24'
down_revision: Union[str, None] = '17ec24f17dba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Accounts whose owner never touched the setting.
_NEVER_CHOSEN = """
    NOT EXISTS (
        SELECT 1 FROM audit_logs a
        WHERE a.entity_type = 'AUTH'
          AND a.action = 'SIGNIN_ALERTS_CHANGED'
          AND a.entity_id = users.id
    )
"""


def upgrade() -> None:
    op.alter_column('users', 'signin_alerts_enabled', server_default='true')
    op.execute(
        f"UPDATE users SET signin_alerts_enabled = true "
        f"WHERE signin_alerts_enabled = false AND {_NEVER_CHOSEN}"
    )


def downgrade() -> None:
    # The inverse of upgrade(): back to opt-in for every account whose owner
    # never chose, which is exactly the set upgrade() switched on plus the
    # accounts created since (whose old default would have been off).
    op.alter_column('users', 'signin_alerts_enabled', server_default='false')
    op.execute(
        f"UPDATE users SET signin_alerts_enabled = false "
        f"WHERE signin_alerts_enabled = true AND {_NEVER_CHOSEN}"
    )
