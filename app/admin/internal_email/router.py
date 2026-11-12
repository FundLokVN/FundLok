from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.access_log import audit_admin_read
from app.admin.internal_email import google, service
from app.admin.internal_email.schemas import (
    EmailContent,
    EmailOAuthStart,
    EmailPolicyOut,
    EmailPolicyUpdate,
    EmailPreview,
    EmailSettingsOut,
    EmailSettingsUpdate,
    EmailTestResult,
    InternalEmailOut,
    RecipientOut,
    ReminderSenderOut,
    ReminderSenderUpdate,
    SendEmailRequest,
)
from app.core.config import settings
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
    """Your sender name, signature and sending switch. The Google account is
    connected through /oauth/authorize, not here."""
    return await service.update_settings(
        db,
        body,
        actor_id=current_user.id,
        actor_email=current_user.email,
        ip_address=_ip(request),
    )


@router.get("/oauth/authorize", response_model=EmailOAuthStart)
async def start_google_oauth(
    response: Response,
    current_user: User = Depends(require_admin),
):
    """Google's consent URL for connecting your own Gmail account. The console
    sends the browser there; Google sends it back to /oauth/callback.

    Also sets the short-lived cookie that binds the flow to this browser (it
    holds the PKCE verifier). No Path attribute, so the browser scopes it to
    this URL's directory and returns it only to the callback."""
    start, verifier = service.start_oauth(actor_id=current_user.id)
    response.set_cookie(
        key=google.BINDING_COOKIE,
        value=verifier,
        max_age=int(google.STATE_TTL.total_seconds()),
        path=None,
        httponly=True,
        # Lax: sent on Google's top-level redirect back, never on a
        # cross-site subrequest.
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    response.headers["Cache-Control"] = "no-store"
    return start


@router.get("/oauth/callback", response_class=RedirectResponse, include_in_schema=False)
async def google_oauth_callback(
    request: Request,
    code: str | None = Query(None, max_length=2048),
    state: str | None = Query(None, max_length=4096),
    error: str | None = Query(None, max_length=256),
    db: AsyncSession = Depends(get_db),
):
    """Where Google sends the browser back. Not a session endpoint: the admin
    is the one in the signed `state`, and the browser must hold the binding
    cookie /oauth/authorize set (see google.py). Always answers with a
    redirect to Admin > Email, ?connected=true or ?error=<code>."""
    try:
        await service.complete_oauth(
            db,
            code=code,
            state=state,
            error=error,
            verifier=request.cookies.get(google.BINDING_COOKIE),
            ip_address=_ip(request),
        )
        target = service.console_url(connected="true")
    except service.OAuthCallbackError as exc:
        target = service.console_url(error=exc.code)
    redirect = RedirectResponse(target, status_code=303)
    redirect.delete_cookie(
        google.BINDING_COOKIE,
        path=None,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    redirect.headers["Cache-Control"] = "no-store"
    # The URL that reached us carried the one-time code.
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


@router.post("/disconnect", response_model=EmailSettingsOut)
async def disconnect_google(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Forget your connected Google account and revoke its token at Google
    (best effort). Your sender name and signature are kept."""
    return await service.disconnect(db, actor_id=current_user.id, ip_address=_ip(request))


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


@router.get(
    "/messages",
    response_model=list[InternalEmailOut],
    dependencies=[Depends(audit_admin_read("INTERNAL_EMAIL"))],
)
async def list_messages(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Your own sends; a system admin sees every admin's."""
    return await service.list_messages(db, actor=current_user, limit=limit, offset=offset)


@router.get(
    "/recipients",
    response_model=list[RecipientOut],
    dependencies=[Depends(audit_admin_read("USER"))],
)
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


@router.get(
    "/reminder-sender",
    response_model=ReminderSenderOut,
    # Lists other admins' names and addresses (SEC-02).
    dependencies=[Depends(audit_admin_read("USER"))],
)
async def get_reminder_sender(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    """SYSTEM_ADMIN only. Whose connected account sends the automatic
    repayment reminders, and which admins' accounts could."""
    return await service.get_reminder_sender(db)


@router.put("/reminder-sender", response_model=ReminderSenderOut)
async def update_reminder_sender(
    body: ReminderSenderUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    """SYSTEM_ADMIN only. `user_id: null` stops automatic reminders."""
    return await service.update_reminder_sender(
        db, body, actor_id=current_user.id, ip_address=_ip(request)
    )
