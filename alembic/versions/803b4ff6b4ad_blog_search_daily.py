"""Store daily Google Search Console figures per blog post.

Revision ID: 803b4ff6b4ad
Revises: 6a81b27c0f4d
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "803b4ff6b4ad"
down_revision = "6a81b27c0f4d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blog_search_daily",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("position_weighted", sa.Float(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("clicks >= 0 AND impressions >= 0", name="chk_blog_search_daily_counts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "slug", name="uq_blog_search_daily_date_slug"),
    )
    op.create_index("idx_blog_search_daily_slug_date", "blog_search_daily", ["slug", "date"])


def downgrade() -> None:
    op.drop_index("idx_blog_search_daily_slug_date", table_name="blog_search_daily")
    op.drop_table("blog_search_daily")
