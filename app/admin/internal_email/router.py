from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.internal_email import service
from app.admin.internal_email.schemas import (
    EmailContent,
    EmailPolicyOut,
    EmailPolicyUpdate,
    EmailPreview,
    EmailSettingsOut,
    EmailSettingsUpdate,
    EmailTestResult,
    InternalEmailOut,
    RecipientOut,
    SendEmailRequest,
)
from app.core.database import get_db
from app.users.models import Role, User
from app.utils.rbac import require_roles

# Every admin (ADMIN and SYSTEM_ADMIN) connects their own Gmail account and
# sends from it; /settings always means the signed-in admin's own. Only the
# org-wide policy (allowed recipient domains) is SYSTEM_ADMIN to change.
require_admin = require_roles(Role.ADMIN, Role.SYSTEM_ADMIN)
require_system_admin = require_roles(Role.SYSTEM_ADMIN)

router = APIRouter(prefix="/admin/email", tags=["admin-email"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/settings", response_model=EmailSettingsOut)
async def get_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The signed-in admin's own Gmail connection."""
    return await service.get_settings(db, actor_id=current_user.id)


@router.put("/settings", response_model=EmailSettingsOut)
async def update_settings(
    body: EmailSettingsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Connect or change your own Gmail account. A blank `app_password` keeps
    the stored one. The password is never returned."""
    return await service.update_settings(
        db, body, actor_id=current_user.id, ip_address=_ip(request)
    )


@router.delete("/settings", response_model=EmailSettingsOut)
async def clear_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Disconnect your account and delete its stored password."""
    return await service.clear_settings(db, actor_id=current_user.id, ip_address=_ip(request))


@router.post("/settings/test", response_model=EmailTestResult)
async def send_test_email(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Send a test email from your account to your own address."""
    return await service.send_test(db, actor=current_user, ip_address=_ip(request))


@router.post("/preview", response_model=EmailPreview)
async def preview_email(
    body: EmailContent,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The rendered email, exactly as it would be sent. Sends nothing."""
    return await service.preview(db, body, actor_id=current_user.id)


@router.post("/send", response_model=InternalEmailOut)
async def send_email(
    body: SendEmailRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    return await service.send(db, body, actor=current_user, ip_address=_ip(request))


@router.get("/messages", response_model=list[InternalEmailOut])
async def list_messages(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Your own sends; a system admin sees every admin's."""
    return await service.list_messages(db, actor=current_user, limit=limit, offset=offset)


@router.get("/recipients", response_model=list[RecipientOut])
async def list_recipients(
    q: str | None = Query(None, max_length=100),
    fundlok_only: bool = False,
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Users to pick for the To list: active, verified accounts, searched by
    name or email. `fundlok_only` keeps FundLok's own addresses."""
    return await service.list_recipients(db, query=q, fundlok_only=fundlok_only, limit=limit)


@router.get("/policy", response_model=EmailPolicyOut)
async def get_policy(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The org-wide rules every admin's sends follow (allowed domains)."""
    return await service.get_policy(db)


@router.put("/policy", response_model=EmailPolicyOut)
async def update_policy(
    body: EmailPolicyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    """SYSTEM_ADMIN only. Applies to every admin's next send."""
    return await service.update_policy(
        db, body, actor_id=current_user.id, ip_address=_ip(request)
    )
