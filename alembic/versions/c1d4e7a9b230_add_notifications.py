"""add notifications

The bell in the sidebar has rendered a hardcoded list since it was built. This
is the table behind it.

WHY A TABLE, NOT ONLY A PUSH
A realtime socket reaches whoever is looking at the screen at that moment. An
application decision matters most to the applicant who is not. The row is the
notification; a socket is only a hint to refetch it sooner.

WHY `event` + `data` INSTEAD OF A MESSAGE COLUMN
The copy is rendered client-side, in the reader's language, from a stable
event code plus its placeholders. A rendered sentence stored here would be
frozen in whatever language the writer happened to use.

Revision ID: c1d4e7a9b230
Revises: 9f2c4a7d1e30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c1d4e7a9b230"
down_revision = "9f2c4a7d1e30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text()),
        sa.Column("entity_id", UUID(as_uuid=True)),
        sa.Column("data", JSONB(), nullable=False, server_default="{}"),
        # NULL = unread.
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # A deleted user's notifications go with them: they address that user
        # and nobody else can read them.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    # Every read is "this user's, newest first".
    op.create_index(
        "ix_notifications_user_created", "notifications", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
