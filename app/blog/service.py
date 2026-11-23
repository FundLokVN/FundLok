"""Blog post workflow and image upload. See docs/specs/blog/blog-posts.md.

Services flush; routers write the audit row and commit, as elsewhere in the app.
"""
import copy
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blog.models import BlogMedia, BlogPost
from app.blog.schemas import (
    MEDIA_CONTENT_TYPE_EXTENSIONS,
    MEDIA_MAX_SIZE,
    BlogMediaPresignRequest,
    BlogPostUpdate,
)
from app.blog.validation import (
    EMPTY_DOCUMENT,
    ContentError,
    keyword_key,
    normalize_keyword,
    reading_minutes,
    slugify,
    validate_cover,
    validate_document,
)
from app.core.config import settings
from app.users.models import User
from app.utils.r2 import delete_object, head_object, presign_put

DEFAULT_LOCALE = "vi"
FALLBACK_SLUG = "bai-viet"
_MAX_SLUG_SUFFIX = 100


def blog_error(status_code: int, code: str, detail: str) -> HTTPException:
    """A refusal the admin UI translates by its X-Error-Code."""
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def audit_meta(post: BlogPost) -> dict[str, Any]:
    """What an audit row keeps about a post: never the body."""
    return {
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "version": post.version,
    }


# --------------------------------------------------------------------------- #
# Slugs
# --------------------------------------------------------------------------- #


async def _slug_taken(db: AsyncSession, locale: str, slug: str, exclude_id: uuid.UUID | None = None) -> bool:
    query = select(BlogPost.id).where(BlogPost.locale == locale, BlogPost.slug == slug)
    if exclude_id is not None:
        query = query.where(BlogPost.id != exclude_id)
    return (await db.execute(query.limit(1))).first() is not None


async def _free_slug(db: AsyncSession, locale: str, title: str) -> str:
    base = slugify(title) or FALLBACK_SLUG
    for n in range(1, _MAX_SLUG_SUFFIX + 1):
        suffix = "" if n == 1 else f"-{n}"
        candidate = f"{base[: 80 - len(suffix)].rstrip('-')}{suffix}"
        if not await _slug_taken(db, locale, candidate):
            return candidate
    # A hundred posts with the same title: fall back to something unique.
    return f"{base[:67].rstrip('-')}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Admin reads
# --------------------------------------------------------------------------- #


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_posts_admin(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    status_filter: str | None = None,
    search: str | None = None,
) -> tuple[int, list[BlogPost]]:
    conditions = []
    if status_filter:
        conditions.append(BlogPost.status == status_filter)
    if search and search.strip():
        pattern = _like(search.strip())
        conditions.append(or_(BlogPost.title.ilike(pattern, escape="\\"), BlogPost.slug.ilike(pattern, escape="\\")))

    total = (await db.execute(select(func.count()).select_from(BlogPost).where(*conditions))).scalar_one()
    rows = await db.execute(
        select(BlogPost)
        .where(*conditions)
        .order_by(BlogPost.updated_at.desc(), BlogPost.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return total, list(rows.scalars())


async def get_post(db: AsyncSession, post_id: uuid.UUID, *, for_update: bool = False) -> BlogPost:
    query = select(BlogPost).where(BlogPost.id == post_id)
    if for_update:
        # Two admins saving at once: the second waits, then sees the bumped version.
        query = query.with_for_update()
    post = (await db.execute(query)).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog post not found")
    return post


# --------------------------------------------------------------------------- #
# Admin writes
# --------------------------------------------------------------------------- #


def _check_version(post: BlogPost, version: int) -> None:
    if version != post.version:
        raise blog_error(
            status.HTTP_409_CONFLICT,
            "BLOG_VERSION_CONFLICT",
            "This post was changed by someone else. Reload it before saving.",
        )


def _touch(post: BlogPost, actor: User) -> None:
    post.version += 1
    post.updated_by = actor.id
    post.updated_at = _now()


async def create_post(db: AsyncSession, *, title: str, actor: User) -> BlogPost:
    now = _now()
    post = BlogPost(
        id=uuid.uuid4(),
        locale=DEFAULT_LOCALE,
        slug=await _free_slug(db, DEFAULT_LOCALE, title),
        title=title,
        excerpt="",
        content=copy.deepcopy(EMPTY_DOCUMENT),
        reading_minutes=1,
        status="draft",
        author_id=actor.id,
        updated_by=actor.id,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(post)
    await db.flush()
    return post


async def update_post(db: AsyncSession, post: BlogPost, body: BlogPostUpdate, *, actor: User) -> BlogPost:
    _check_version(post, body.version)

    if body.slug != post.slug:
        if post.published_at is not None:
            raise blog_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "BLOG_SLUG_LOCKED",
                "The address of a post that has been published cannot change.",
            )
        if await _slug_taken(db, post.locale, body.slug, exclude_id=post.id):
            raise blog_error(status.HTTP_409_CONFLICT, "BLOG_SLUG_TAKEN", "Another post already uses this address.")

    try:
        content, plain_text = validate_document(body.content)
        cover_url, cover_alt = validate_cover(body.cover_image_url, body.cover_alt)
    except ContentError as exc:
        raise blog_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "BLOG_CONTENT_INVALID", str(exc)) from None

    post.title = body.title
    post.slug = body.slug
    post.seo_title = body.seo_title
    post.excerpt = body.excerpt
    post.content = content
    post.reading_minutes = reading_minutes(plain_text)
    post.cover_image_url = cover_url
    post.cover_alt = cover_alt
    post.focus_keyword = body.focus_keyword
    post.secondary_keywords = body.secondary_keywords
    _touch(post, actor)
    await db.flush()
    return post


async def publish_post(db: AsyncSession, post: BlogPost, *, version: int, actor: User) -> bool:
    """Publish; False when it already was (nothing changed, nothing to audit)."""
    _check_version(post, version)
    if post.status == "published":
        return False

    try:
        _, plain_text = validate_document(post.content)
    except ContentError as exc:
        raise blog_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "BLOG_CONTENT_INVALID", str(exc)) from None
    missing = [
        name
        for name, value in (("title", post.title), ("excerpt", post.excerpt), ("body", plain_text))
        if not (value or "").strip()
    ]
    if missing:
        raise blog_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "BLOG_PUBLISH_INCOMPLETE",
            f"Add {', '.join(missing)} before publishing.",
        )

    post.status = "published"
    if post.published_at is None:
        post.published_at = _now()
    _touch(post, actor)
    await db.flush()
    return True


async def unpublish_post(db: AsyncSession, post: BlogPost, *, version: int, actor: User) -> bool:
    """Back to draft; False when it was not published."""
    _check_version(post, version)
    if post.status != "published":
        return False
    post.status = "draft"
    _touch(post, actor)
    await db.flush()
    return True


async def delete_post(db: AsyncSession, post: BlogPost) -> None:
    if post.status == "published":
        raise blog_error(
            status.HTTP_409_CONFLICT,
            "BLOG_DELETE_PUBLISHED",
            "Unpublish this post before deleting it.",
        )
    await db.delete(post)
    await db.flush()


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #


def _media_storage() -> tuple[str, str]:
    bucket, base = settings.R2_PUBLIC_BUCKET, (settings.R2_PUBLIC_BASE_URL or "").rstrip("/")
    if not bucket or not base:
        raise blog_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BLOG_MEDIA_NOT_CONFIGURED",
            "Blog image storage is not configured",
        )
    return bucket, base


async def create_media_presign(
    db: AsyncSession, body: BlogMediaPresignRequest, *, actor: User
) -> tuple[BlogMedia, str, str]:
    bucket, base = _media_storage()
    ext = MEDIA_CONTENT_TYPE_EXTENSIONS.get(body.content_type)
    if ext is None:
        allowed = ", ".join(sorted(MEDIA_CONTENT_TYPE_EXTENSIONS))
        raise blog_error(status.HTTP_400_BAD_REQUEST, "BLOG_MEDIA_INVALID", f"Unsupported image type (allowed: {allowed})")
    if body.size_bytes > MEDIA_MAX_SIZE:
        raise blog_error(
            status.HTTP_400_BAD_REQUEST,
            "BLOG_MEDIA_INVALID",
            f"Image too large (max {MEDIA_MAX_SIZE // (1024 * 1024)} MB)",
        )

    media_id = uuid.uuid4()
    media = BlogMedia(
        id=media_id,
        key=f"blog/{media_id}.{ext}",
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        uploaded_by=actor.id,
        created_at=_now(),
    )
    upload_url = presign_put(media.key, body.content_type, content_length=body.size_bytes, bucket=bucket)
    db.add(media)
    await db.flush()
    return media, upload_url, f"{base}/{media.key}"


async def confirm_media(db: AsyncSession, media_id: uuid.UUID) -> tuple[BlogMedia, str]:
    bucket, base = _media_storage()
    media = (await db.execute(select(BlogMedia).where(BlogMedia.id == media_id))).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")

    head = head_object(media.key, bucket=bucket)
    if head is None:
        raise blog_error(status.HTTP_400_BAD_REQUEST, "BLOG_MEDIA_INVALID", "The image was not uploaded")
    if head["size"] is not None and head["size"] > MEDIA_MAX_SIZE:
        # Delete as well as refuse, so a refused upload doesn't stay public.
        delete_object(media.key, bucket=bucket)
        raise blog_error(status.HTTP_400_BAD_REQUEST, "BLOG_MEDIA_INVALID", "Uploaded image too large")

    if media.confirmed_at is None:
        media.confirmed_at = _now()
        await db.flush()
    return media, f"{base}/{media.key}"


# --------------------------------------------------------------------------- #
# Keywords
# --------------------------------------------------------------------------- #


async def find_focus_keyword_usage(
    db: AsyncSession, *, keyword: str, exclude_id: uuid.UUID | None = None
) -> tuple[str, list[BlogPost]]:
    """Other posts targeting the same focus keyword (two posts competing for one
    search hurt each other). Compared case-insensitively in Python: Postgres
    lower() only folds ASCII under a C collation, and Vietnamese is not ASCII.
    The blog is small, so reading every focus keyword is cheap."""
    normalized = normalize_keyword(keyword)
    if not normalized:
        return "", []
    query = select(BlogPost).where(BlogPost.focus_keyword.is_not(None))
    if exclude_id is not None:
        query = query.where(BlogPost.id != exclude_id)
    target = keyword_key(normalized)
    rows = await db.execute(query.order_by(BlogPost.updated_at.desc()))
    return normalized, [post for post in rows.scalars() if keyword_key(post.focus_keyword) == target]


# --------------------------------------------------------------------------- #
# Public reads
# --------------------------------------------------------------------------- #


async def list_published(db: AsyncSession, *, locale: str, limit: int) -> list[BlogPost]:
    rows = await db.execute(
        select(BlogPost)
        .where(BlogPost.status == "published", BlogPost.locale == locale)
        .order_by(BlogPost.published_at.desc(), BlogPost.id)
        .limit(limit)
    )
    return list(rows.scalars())


async def get_published(db: AsyncSession, *, locale: str, slug: str) -> BlogPost:
    post = (
        await db.execute(
            select(BlogPost).where(
                BlogPost.status == "published",
                BlogPost.locale == locale,
                BlogPost.slug == slug,
            )
        )
    ).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog post not found")
    return post
