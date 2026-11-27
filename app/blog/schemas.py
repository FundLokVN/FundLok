"""Request/response models for the blog. See docs/specs/blog/blog-posts.md §4."""
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.blog.models import KEYWORD_MAX_LENGTH, MAX_SECONDARY_KEYWORDS, SLUG_PATTERN
from app.blog.validation import keyword_key, normalize_keyword

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
    # Editor-only SEO targets (§3); default empty so an older client's save is valid.
    focus_keyword: str | None = Field(default=None, max_length=KEYWORD_MAX_LENGTH * 2)
    secondary_keywords: list[str] = Field(default_factory=list, max_length=20)
    version: int = Field(ge=1)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _keywords(self) -> "BlogPostUpdate":
        """Trim and collapse spaces, drop blanks and case-insensitive repeats, and
        never repeat the focus keyword among the secondary ones."""
        focus = normalize_keyword(self.focus_keyword) if self.focus_keyword else ""
        if len(focus) > KEYWORD_MAX_LENGTH:
            raise ValueError(f"focus_keyword is longer than {KEYWORD_MAX_LENGTH} characters")
        seen = {focus.casefold()} if focus else set()
        secondary: list[str] = []
        for raw in self.secondary_keywords:
            keyword = normalize_keyword(raw)
            if not keyword or keyword_key(keyword) in seen:
                continue
            if len(keyword) > KEYWORD_MAX_LENGTH:
                raise ValueError(f"a secondary keyword is longer than {KEYWORD_MAX_LENGTH} characters")
            seen.add(keyword_key(keyword))
            secondary.append(keyword)
        if len(secondary) > MAX_SECONDARY_KEYWORDS:
            raise ValueError(f"at most {MAX_SECONDARY_KEYWORDS} secondary keywords")
        self.focus_keyword = focus or None
        self.secondary_keywords = secondary
        return self

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


class BlogSearchStats(BaseModel):
    """A post's Google Search figures over the list's window (search-console-stats.md)."""

    clicks: int
    impressions: int
    # Average position, weighted by impressions; None with no impressions.
    position: float | None


class BlogIndexStatusOut(BaseModel):
    """Google's latest URL Inspection answer for the post's public address."""

    model_config = ConfigDict(from_attributes=True)

    # PASS = indexed; NEUTRAL = not indexed; PARTIAL / FAIL = indexed with
    # problems / an error; VERDICT_UNSPECIFIED = Google gave none.
    verdict: str
    coverage_state: str | None
    last_crawl_at: datetime | None
    checked_at: datetime


class BlogSitemapSubmitResult(BaseModel):
    submitted: bool
    # Why not, when not: NOT_CONFIGURED, or REFUSED (Google said no; the
    # service account needs Full permission on the property).
    reason: Literal["NOT_CONFIGURED", "REFUSED"] | None = None


class BlogSearchConsoleStatus(BaseModel):
    configured: bool
    last_synced_at: datetime | None
    # The newest day stored; the window is the `window_days` ending on it.
    data_through: date | None
    window_days: int


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
    view_count: int
    # Admin list only; None when Google has not reported the post.
    search: BlogSearchStats | None = None
    # Admin list only; None until the daily check has looked at the post.
    index_status: BlogIndexStatusOut | None = None


class BlogPostDetail(BlogPostSummary):
    locale: Locale
    seo_title: str | None
    content: dict[str, Any]
    version: int
    author_id: UUID | None
    updated_by: UUID | None
    focus_keyword: str | None
    secondary_keywords: list[str]


class KeywordUsagePost(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    title: str
    status: PostStatus


class KeywordUsageResponse(BaseModel):
    """Other posts whose focus keyword is the same, compared case-insensitively."""

    keyword: str
    posts: list[KeywordUsagePost]


class BlogPostListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[BlogPostSummary]
    search_console: BlogSearchConsoleStatus | None = None


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
    next_offset: int | None = None


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


# --------------------------------------------------------------------------- #
# SEO assistant (docs/specs/blog/seo-assistant.md)
# --------------------------------------------------------------------------- #

# About 30k tokens: far beyond any real post, short of an expensive mistake.
SEO_ARTICLE_MAX_CHARS = 100_000


class BlogSeoAssistantStatus(BaseModel):
    """Whether the editor shows "Suggest SEO" to the signed-in admin."""

    enabled: bool


class BlogSeoSuggestionRequest(BaseModel):
    """The draft on screen, unsaved edits included, as plain text."""

    title: str = Field(min_length=1, max_length=200)
    locale: Locale
    text: str = Field(min_length=1, max_length=SEO_ARTICLE_MAX_CHARS)

    @field_validator("title", "text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SeoTitleSuggestion(BaseModel):
    title: str
    intent: str


class BlogSeoSuggestions(BaseModel):
    """Checked against BlogPostUpdate's limits before it is returned."""

    titles: list[SeoTitleSuggestion]
    meta_description: str
    focus_keyword: str
    related_terms: list[str]
