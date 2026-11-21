"""Blog posts and their uploaded images. See docs/specs/blog/blog-posts.md."""
import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    desc,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base

# Lowercase words joined by single hyphens, no dots: the front end's proxy
# skips any path containing "." (fundlok-front-end proxy.ts matcher), so a
# dotted slug would never be routed.
SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class BlogPost(Base):
    """One post. `content` is a TipTap document checked by app/blog/validation.py."""

    __tablename__ = "blog_posts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    locale = Column(String(5), nullable=False, server_default="vi")
    slug = Column(String(80), nullable=False)
    title = Column(String(200), nullable=False)
    seo_title = Column(String(70), nullable=True)
    excerpt = Column(String(300), nullable=False, server_default="")
    content = Column(JSONB, nullable=False)
    reading_minutes = Column(Integer, nullable=False, server_default="1")
    cover_image_url = Column(String(500), nullable=True)
    cover_alt = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False, server_default="draft")
    # Set on the first publish and kept after an unpublish: it is the date the
    # post first went live, and its presence locks the slug.
    published_at = Column(DateTime(timezone=True), nullable=True)
    author_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    version = Column(Integer, nullable=False, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("locale IN ('vi', 'en')", name="chk_blog_posts_locale"),
        CheckConstraint("status IN ('draft', 'published')", name="chk_blog_posts_status"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="chk_blog_posts_slug"),
        CheckConstraint("reading_minutes > 0", name="chk_blog_posts_reading_minutes"),
        CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name="chk_blog_posts_published_at",
        ),
        UniqueConstraint("locale", "slug", name="uq_blog_posts_locale_slug"),
        Index("idx_blog_posts_status_published_at", "status", desc("published_at")),
    )


class BlogMedia(Base):
    """An image uploaded for a post, stored in the public bucket under blog/."""

    __tablename__ = "blog_media"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String(255), nullable=False)
    content_type = Column(String(50), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    confirmed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="chk_blog_media_size_bytes"),
        UniqueConstraint("key", name="uq_blog_media_key"),
    )
