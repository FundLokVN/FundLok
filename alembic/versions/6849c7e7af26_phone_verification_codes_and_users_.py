"""phone verification codes and users.phone_verified_at

A new account proves a Vietnamese mobile number by one-time code (a Zalo ZBS
template message) before it may choose a role. See
docs/specs/users/phone-verification.md.

users.phone_verified_at is NULL for every existing row: no number on file was
ever proven. Accounts that already have a role are not gated by it.

Revision ID: 6849c7e7af26
Revises: 0538f8bc779a
Create Date: 2026-11-04 13:02:39.104141

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '6849c7e7af26'
down_revision: Union[str, None] = '0538f8bc779a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "phone_verification_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_phone_verification_codes_user_created",
        "phone_verification_codes",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_phone_verification_codes_phone_created",
        "phone_verification_codes",
        ["phone", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_phone_verification_codes_phone_created", table_name="phone_verification_codes")
    op.drop_index("ix_phone_verification_codes_user_created", table_name="phone_verification_codes")
    op.drop_table("phone_verification_codes")
    op.drop_column("users", "phone_verified_at")
