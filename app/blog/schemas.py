"""Request/response models for the blog. See docs/specs/blog/blog-posts.md §4."""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.blog.models import SLUG_PATTERN

PostStatus = Literal["draft", "published"]
Locale = Literal["vi", "en"]

MEDIA_MAX_SIZE = 5 * 1024 * 1024  # 5 MB, same as avatars
MEDIA_CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class BlogPostCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


class BlogPostUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=80, pattern=SLUG_PATTERN)
    seo_title: str | None = Field(default=None, max_length=70)
    excerpt: str = Field(default="", max_length=300)
    content: dict[str, Any]
    cover_image_url: str | None = Field(default=None, max_length=500)
    cover_alt: str | None = Field(default=None, max_length=200)
    version: int = Field(ge=1)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("excerpt")
    @classmethod
    def _excerpt(cls, value: str) -> str:
        return value.strip()

    @field_validator("seo_title", "cover_image_url", "cover_alt")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _strip_or_none(value)


class BlogVersionBody(BaseModel):
    version: int = Field(ge=1)


class BlogPostSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    excerpt: str
    status: PostStatus
    cover_image_url: str | None
    cover_alt: str | None
    published_at: datetime | None
    updated_at: datetime
    created_at: datetime
    reading_minutes: int


class BlogPostDetail(BlogPostSummary):
    locale: Locale
    seo_title: str | None
    content: dict[str, Any]
    version: int
    author_id: UUID | None
    updated_by: UUID | None


class BlogPostListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[BlogPostSummary]


class PublicBlogPostSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    title: str
    excerpt: str
    cover_image_url: str | None
    cover_alt: str | None
    published_at: datetime
    updated_at: datetime
    reading_minutes: int


class PublicBlogPost(PublicBlogPostSummary):
    locale: Locale
    seo_title: str | None
    content: dict[str, Any]


class PublicBlogPostListResponse(BaseModel):
    items: list[PublicBlogPostSummary]


class BlogMediaPresignRequest(BaseModel):
    content_type: str = Field(max_length=50)
    size_bytes: int = Field(gt=0)


class BlogMediaPresignResponse(BaseModel):
    media_id: UUID
    upload_url: str
    public_url: str


class BlogMediaConfirmResponse(BaseModel):
    media_id: UUID
    public_url: str
