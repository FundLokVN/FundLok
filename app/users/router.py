from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent import service as consent_service
from app.core.database import get_db
from app.notifications.models import (
    EVENT_VERIFICATION_REQUIRED,
    VERIFICATION_CHECK_KYB,
    VERIFICATION_CHECK_KYC,
)
from app.notifications.service import create_notification
from app.users.models import Role, User
from app.users.schemas import (
    AvatarConfirmRequest,
    AvatarPresignRequest,
    AvatarPresignResponse,
    RoleSelectRequest,
    SetPasswordRequest,
    UserUpdateRequest,
)
from app.users.schemas import (
    ChangePasswordRequest,
    SecurityPreferencesOut,
    SecurityPreferencesUpdate,
)
from app.users.service import (
    complete_onboarding_tour,
    change_password,
    update_security_preferences,
    confirm_avatar,
    create_avatar_presign,
    select_user_role,
    set_password,
    update_user_profile,
    user_me_payload,
)
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user, get_current_user_without_second_factor

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def read_current_user(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_without_second_factor),
):
    payload = user_me_payload(current_user)
    payload["consent_required"] = await consent_service.is_required(db, current_user)
    return payload


@router.patch("/me")
async def update_current_user(
    body: UserUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    before = {
        "full_name": current_user.full_name,
        "phone": current_user.phone,
        "bio": current_user.bio,
        "email_signature": current_user.email_signature,
    }
    user = await update_user_profile(db, body, current_user)
    append_audit(
        db,
        entity_type="USER",
        entity_id=user.id,
        action="PROFILE_UPDATED",
        actor_id=user.id,
        before_state=before,
        after_state={
            "full_name": user.full_name,
            "phone": user.phone,
            "bio": user.bio,
            "email_signature": user.email_signature,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return user_me_payload(user)


@router.post("/me/password", status_code=200)
async def set_current_user_password(
    body: SetPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set a first password on an account that doesn't have one (Google or
    Microsoft sign-in only). Needs a recent sign-in; see set_password.

    Changing an existing password is NOT done here -- that's /me/password/change
    or forgot-password.
    """
    await set_password(
        db,
        body,
        current_user,
        background_tasks,
        session_id=getattr(request.state, "session_id", None),
    )
    # AUTH, not USER, so it shows on the owner's security screen
    # (list_security_events): a password they didn't set is the sign that
    # someone else holds their session.
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=current_user.id,
        action="PASSWORD_SET",
        actor_id=current_user.id,
        before_state={"has_password": False},
        after_state={"has_password": True},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "success", "message": "Password set successfully"}


@router.patch("/me/role")
async def select_role(
    body: RoleSelectRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await consent_service.require_consent(db, current_user)
    before = {"role": current_user.role}
    user = await select_user_role(db, body.role, current_user)
    append_audit(
        db,
        entity_type="USER",
        entity_id=user.id,
        action="ROLE_SELECTED",
        actor_id=user.id,
        before_state=before,
        after_state={"role": user.role},
        ip_address=request.client.host if request.client else None,
    )
    # A role is chosen once, so this is the one moment every new SME and
    # investor passes through. Nothing can be applied for or invested in
    # until they verify, and verification is otherwise only offered when they
    # hit that gate, so tell them now. Same transaction as the role: no
    # reminder for a selection that did not stick.
    create_notification(
        db,
        user_id=user.id,
        event=EVENT_VERIFICATION_REQUIRED,
        entity_type="USER",
        entity_id=user.id,
        data={
            "check": VERIFICATION_CHECK_KYB
            if body.role is Role.SME
            else VERIFICATION_CHECK_KYC
        },
    )
    await db.commit()
    return user_me_payload(user)


@router.post("/me/avatar/presign", response_model=AvatarPresignResponse, status_code=201)
def presign_avatar(
    body: AvatarPresignRequest,
    current_user: User = Depends(get_current_user),
):
    file_key, upload_url, expires_in = create_avatar_presign(body, current_user)
    return AvatarPresignResponse(
        file_key=file_key,
        upload_url=upload_url,
        expires_in=expires_in,
    )


@router.post("/me/avatar/confirm")
async def confirm_avatar_upload(
    body: AvatarConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await confirm_avatar(db, body, current_user)
    append_audit(
        db,
        entity_type="USER",
        entity_id=user.id,
        action="AVATAR_UPDATED",
        actor_id=user.id,
        after_state={"avatar_key": user.avatar_key},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return user_me_payload(user)


@router.post("/me/password/change", status_code=200)
async def change_current_user_password(
    body: ChangePasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Change an existing password.

    Separate from /me/password (which only sets a FIRST password) because the
    safety argument is different: this one is served off a session, so it
    requires the current password as proof the session is not borrowed. Every
    other session is revoked as part of the change -- see change_password.
    """
    revoked = await change_password(
        db,
        body,
        current_user,
        background_tasks,
        current_session_id=request.state.session_id,
        ip_address=request.client.host if request.client else None,
    )
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=current_user.id,
        action="PASSWORD_CHANGED",
        actor_id=current_user.id,
        after_state={"sessions_revoked": revoked},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {"status": "success", "sessions_revoked": revoked}


@router.get("/me/security-preferences", response_model=SecurityPreferencesOut)
async def get_security_preferences(
    current_user: User = Depends(get_current_user),
):
    return SecurityPreferencesOut(
        signin_alerts_enabled=bool(current_user.signin_alerts_enabled)
    )


@router.patch("/me/security-preferences", response_model=SecurityPreferencesOut)
async def patch_security_preferences(
    body: SecurityPreferencesUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle sign-in alerts.

    Audited: switching alerts OFF is exactly what someone who has taken over an
    account would do, so the change itself belongs in the security history.
    """
    before = bool(current_user.signin_alerts_enabled)
    user = await update_security_preferences(
        db, current_user, signin_alerts_enabled=body.signin_alerts_enabled
    )
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=user.id,
        action="SIGNIN_ALERTS_CHANGED",
        actor_id=user.id,
        before_state={"signin_alerts_enabled": before},
        after_state={"signin_alerts_enabled": bool(user.signin_alerts_enabled)},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return SecurityPreferencesOut(
        signin_alerts_enabled=bool(user.signin_alerts_enabled)
    )


@router.post("/me/onboarding-tour/complete")
async def complete_onboarding_tour_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark the first-run dashboard walkthrough as seen for this ACCOUNT.

    Not audited, unlike the security preferences beside it: nothing here can
    take an account away from its owner, and an audit row per dismissed tooltip
    would bury the events that matter.

    Returns the whole /me payload so the frontend can seed its cached user in
    one round trip rather than refetching after the write.
    """
    user = await complete_onboarding_tour(db, current_user)
    return user_me_payload(user)
