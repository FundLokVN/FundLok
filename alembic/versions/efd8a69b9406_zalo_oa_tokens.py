"""zalo oa tokens

The Zalo OA token pair used to send phone verification codes by Zalo ZNS.
Zalo rotates the refresh token on every refresh, so the current pair is kept
here (Fernet-encrypted) for every instance to share. One row per Zalo app.
See docs/specs/users/phone-verification.md.

Revision ID: efd8a69b9406
Revises: 6849c7e7af26
Create Date: 2026-11-04 13:25:50.211406

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'efd8a69b9406'
down_revision: Union[str, None] = '6849c7e7af26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "zalo_oa_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("app_id", sa.Text(), nullable=False, unique=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seeded_from", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("zalo_oa_tokens")
