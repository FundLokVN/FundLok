"""blog_posts and blog_media: admin-written blog with public read API

Revision ID: 749861cba438
Revises: c4e9a2b7d1f6
Create Date: 2026-11-21 12:00:00

Two new tables, nothing existing is touched. `blog_posts.content` is a TipTap
document validated by app/blog/validation.py; `blog_media` records images
uploaded to the public bucket (R2_PUBLIC_BUCKET) under blog/.

See docs/specs/blog/blog-posts.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "749861cba438"
down_revision: Union[str, None] = "c4e9a2b7d1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


def upgrade() -> None:
    op.create_table(
        "blog_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("locale", sa.String(length=5), nullable=False, server_default="vi"),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("seo_title", sa.String(length=70), nullable=True),
        sa.Column("excerpt", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("reading_minutes", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cover_image_url", sa.String(length=500), nullable=True),
        sa.Column("cover_alt", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("locale IN ('vi', 'en')", name="chk_blog_posts_locale"),
        sa.CheckConstraint("status IN ('draft', 'published')", name="chk_blog_posts_status"),
        sa.CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="chk_blog_posts_slug"),
        sa.CheckConstraint("reading_minutes > 0", name="chk_blog_posts_reading_minutes"),
        sa.CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name="chk_blog_posts_published_at",
        ),
        sa.UniqueConstraint("locale", "slug", name="uq_blog_posts_locale_slug"),
    )
    op.create_index(
        "idx_blog_posts_status_published_at",
        "blog_posts",
        ["status", sa.text("published_at DESC")],
    )

    op.create_table(
        "blog_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=50), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes > 0", name="chk_blog_media_size_bytes"),
        sa.UniqueConstraint("key", name="uq_blog_media_key"),
    )


def downgrade() -> None:
    op.drop_table("blog_media")
    op.drop_index("idx_blog_posts_status_published_at", table_name="blog_posts")
    op.drop_table("blog_posts")
