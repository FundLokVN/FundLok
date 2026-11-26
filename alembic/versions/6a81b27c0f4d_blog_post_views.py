"""Store aggregate blog post views.

Revision ID: 6a81b27c0f4d
Revises: ebeed4fd366b
"""
import sqlalchemy as sa
from alembic import op

revision = "6a81b27c0f4d"
down_revision = "ebeed4fd366b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("blog_posts", sa.Column("view_count", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("blog_posts", "view_count")
