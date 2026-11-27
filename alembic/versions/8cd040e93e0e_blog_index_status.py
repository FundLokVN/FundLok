"""Store Google's latest index status per blog post.

Revision ID: 8cd040e93e0e
Revises: 803b4ff6b4ad
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "8cd040e93e0e"
down_revision = "803b4ff6b4ad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blog_index_status",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("verdict", sa.String(length=30), nullable=False),
        sa.Column("coverage_state", sa.String(length=200), nullable=True),
        sa.Column("last_crawl_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_blog_index_status_slug"),
    )


def downgrade() -> None:
    op.drop_table("blog_index_status")
