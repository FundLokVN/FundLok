"""Blog routes: admin editor API under /admin/blog, public reads under /api/v1/blog.

See docs/specs/blog/blog-posts.md §4. Admin reads return marketing content, no
personal data, so they are exempt from VIEW auditing
(tests/admin/test_admin_read_audit.py NOT_PERSONAL_DATA); every write is audited.
"""
import contextlib
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.blog import service
from app.blog.models import SLUG_PATTERN
from app.blog.schemas import (
    BlogMediaConfirmResponse,
    BlogMediaPresignRequest,
    BlogMediaPresignResponse,
    BlogPostCreate,
    BlogPostDetail,
    BlogPostListResponse,
    BlogPostUpdate,
    BlogVersionBody,
    Locale,
    PostStatus,
    PublicBlogPost,
    PublicBlogPostListResponse,
)
from app.core.database import get_db
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.rbac import require_roles

router = APIRouter(prefix="/api/v1/blog", tags=["blog"])
admin_blog_router = APIRouter(prefix="/admin/blog", tags=["admin-blog"])

require_admin = require_roles(Role.ADMIN, Role.SYSTEM_ADMIN)

POST_ENTITY = "BLOG_POST"
MEDIA_ENTITY = "BLOG_MEDIA"


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@contextlib.asynccontextmanager
async def _slug_race(db: AsyncSession) -> AsyncIterator[None]:
    """Two writers passed the slug check at once: the unique index stops the second."""
    try:
        yield
    except IntegrityError as exc:
        await db.rollback()
        if "uq_blog_posts_locale_slug" in str(exc.orig):
            raise service.blog_error(
                status.HTTP_409_CONFLICT, "BLOG_SLUG_TAKEN", "Another post already uses this address."
            ) from None
        raise


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #


@admin_blog_router.get("/posts", response_model=BlogPostListResponse)
async def list_posts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: PostStatus | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    total, items = await service.list_posts_admin(
        db, page=page, page_size=page_size, status_filter=status_filter, search=search
    )
    return BlogPostListResponse(total=total, page=page, page_size=page_size, items=items)


@admin_blog_router.post("/posts", response_model=BlogPostDetail, status_code=status.HTTP_201_CREATED)
async def create_post(
    body: BlogPostCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    async with _slug_race(db):
        post = await service.create_post(db, title=body.title, actor=current_user)
        append_audit(
            db,
            entity_type=POST_ENTITY,
            entity_id=post.id,
            action="BLOG_POST_CREATED",
            actor_id=current_user.id,
            after_state=service.audit_meta(post),
            ip_address=_ip(request),
        )
        await db.commit()
    return post


@admin_blog_router.get("/posts/{post_id}", response_model=BlogPostDetail)
async def get_post(
    post_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    return await service.get_post(db, post_id)


@admin_blog_router.put("/posts/{post_id}", response_model=BlogPostDetail)
async def update_post(
    post_id: UUID,
    body: BlogPostUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    async with _slug_race(db):
        post = await service.get_post(db, post_id, for_update=True)
        before = service.audit_meta(post)
        await service.update_post(db, post, body, actor=current_user)
        append_audit(
            db,
            entity_type=POST_ENTITY,
            entity_id=post.id,
            action="BLOG_POST_UPDATED",
            actor_id=current_user.id,
            before_state=before,
            after_state=service.audit_meta(post),
            ip_address=_ip(request),
        )
        await db.commit()
    return post


async def _change_status(
    db: AsyncSession,
    request: Request,
    current_user: User,
    post_id: UUID,
    version: int,
    *,
    publish: bool,
):
    post = await service.get_post(db, post_id, for_update=True)
    before = service.audit_meta(post)
    change = service.publish_post if publish else service.unpublish_post
    if await change(db, post, version=version, actor=current_user):
        append_audit(
            db,
            entity_type=POST_ENTITY,
            entity_id=post.id,
            action="BLOG_POST_PUBLISHED" if publish else "BLOG_POST_UNPUBLISHED",
            actor_id=current_user.id,
            before_state=before,
            after_state=service.audit_meta(post),
            ip_address=_ip(request),
        )
    await db.commit()
    return post


@admin_blog_router.post("/posts/{post_id}/publish", response_model=BlogPostDetail)
async def publish_post(
    post_id: UUID,
    body: BlogVersionBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    return await _change_status(db, request, current_user, post_id, body.version, publish=True)


@admin_blog_router.post("/posts/{post_id}/unpublish", response_model=BlogPostDetail)
async def unpublish_post(
    post_id: UUID,
    body: BlogVersionBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    return await _change_status(db, request, current_user, post_id, body.version, publish=False)


@admin_blog_router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_post(
    post_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    post = await service.get_post(db, post_id, for_update=True)
    before = service.audit_meta(post)
    await service.delete_post(db, post)
    append_audit(
        db,
        entity_type=POST_ENTITY,
        entity_id=post_id,
        action="BLOG_POST_DELETED",
        actor_id=current_user.id,
        before_state=before,
        ip_address=_ip(request),
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_blog_router.post(
    "/media/presign",
    response_model=BlogMediaPresignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def presign_media(
    body: BlogMediaPresignRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    media, upload_url, public_url = await service.create_media_presign(db, body, actor=current_user)
    await db.commit()
    return BlogMediaPresignResponse(media_id=media.id, upload_url=upload_url, public_url=public_url)


@admin_blog_router.post("/media/{media_id}/confirm", response_model=BlogMediaConfirmResponse)
async def confirm_media(
    media_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    media, public_url = await service.confirm_media(db, media_id)
    append_audit(
        db,
        entity_type=MEDIA_ENTITY,
        entity_id=media.id,
        action="BLOG_MEDIA_UPLOADED",
        actor_id=current_user.id,
        after_state={"key": media.key, "content_type": media.content_type, "size_bytes": media.size_bytes},
        ip_address=_ip(request),
    )
    await db.commit()
    return BlogMediaConfirmResponse(media_id=media.id, public_url=public_url)


# --------------------------------------------------------------------------- #
# Public (no auth): read by the Next.js server when it renders /blog pages
# --------------------------------------------------------------------------- #


@router.get("/posts", response_model=PublicBlogPostListResponse)
async def list_published_posts(
    locale: Locale = Query(default="vi"),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    return PublicBlogPostListResponse(items=await service.list_published(db, locale=locale, limit=limit))


@router.get("/posts/{locale}/{slug}", response_model=PublicBlogPost)
async def get_published_post(
    locale: Locale,
    slug: str = Path(max_length=80, pattern=SLUG_PATTERN),
    db: AsyncSession = Depends(get_db),
):
    return await service.get_published(db, locale=locale, slug=slug)
