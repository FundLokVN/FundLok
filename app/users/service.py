from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from datetime import datetime, timezone

from uuid import UUID

from app.auth.password_policy import reject_breached_password
from app.auth.service import require_step_up, revoke_other_sessions
from app.phone_verification.phone import same_number
from app.users.models import Role, User
from app.users.schemas import (
    ChangePasswordRequest,
    AVATAR_CONTENT_TYPE_EXTENSIONS,
    AVATAR_MAX_SIZE,
    AvatarConfirmRequest,
    AvatarPresignRequest,
    SetPasswordRequest,
    UserUpdateRequest,
)
from app.utils.email import send_password_changed_notice
from app.utils.password import hash_password, verify_password
from app.utils.r2 import delete_object, head_object, presign_get, presign_put


def build_avatar_url(user: User) -> str | None:
    """Fresh presigned GET URL for the user's avatar, or None.

    Falls back to None (rather than failing the profile request) when no avatar
    is set or object storage is not configured for this environment.
    """
    if not user.avatar_key:
        return None
    try:
        return presign_get(user.avatar_key)
    except HTTPException:
        return None


# FundLok staff, not platform users: never asked for a phone, as for the consent.
_PHONE_EXEMPT_ROLES = {Role.ADMIN.value, Role.SYSTEM_ADMIN.value}


def phone_verification_pending(user: User) -> bool:
    """Whether the account must prove a phone number before it goes on. Every
    SME and investor, and every new account before it has a role: accounts that
    signed up before phone verification are asked too (spec rule 1)."""
    return (
        settings.PHONE_VERIFICATION_REQUIRED
        and user.role not in _PHONE_EXEMPT_ROLES
        and user.phone_verified_at is None
    )


def user_me_payload(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "phone": user.phone,
        "status": user.status,
        "email_verified": user.email_verified,
        "phone_verified": user.phone_verified_at is not None,
        # The phone gate (docs/specs/users/phone-verification.md): every SME
        # and investor account proves a phone number, a new one before it
        # chooses a role. Computed here so the frontend proxy has one flag to
        # route on and never re-derives the rule. Staff are never gated.
        "phone_verification_required": phone_verification_pending(user),
        "bio": user.bio,
        "email_signature": user.email_signature,
        "avatar_url": build_avatar_url(user),
        # Lets the profile page tell "change your password" from "set one for
        # the first time" (OAuth-only accounts have no hash). Never expose the
        # hash itself.
        "has_password": bool(user.password_hash),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        # Drives the first-run walkthrough. Null until the user finishes or
        # skips it; the frontend reads this instead of browser storage so the
        # state follows the account across devices.
        "onboarding_tour_completed_at": (
            user.onboarding_tour_completed_at.isoformat()
            if user.onboarding_tour_completed_at
            else None
        ),
    }


async def set_password(
    db: AsyncSession,
    body: SetPasswordRequest,
    current_user: User,
    background_tasks: BackgroundTasks,
    *,
    session_id: UUID | None,
) -> None:
    """Give a passwordless account (Google/Microsoft sign-in only) its first
    password.

    Only ever a *first* password. An account that already has one is refused
    and pointed at forgot-password, where the emailed token proves control of
    the mailbox, because overwriting an existing one off a borrowed session
    would be account takeover.

    A first password is not harmless either, so a session alone is not enough.
    Once it exists, require_step_up accepts it as proof for adding an
    authenticator or a passkey. Whoever held a stolen session could set one,
    enrol their own 2FA, and leave the owner facing a code they cannot give at
    their next Google sign-in; a reset keeps 2FA on a verified account, so
    that would be the end of it. Setting one therefore asks what those
    enrolments ask of a passwordless account: a session that signed in
    moments ago.
    """
    if current_user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account already has a password. Use Forgot password to change it.",
        )

    # No password to check, so this is the recent-sign-in branch.
    await require_step_up(db, current_user, session_id=session_id, password=None)

    await reject_breached_password(body.new_password)
    current_user.password_hash = hash_password(body.new_password)
    db.add(current_user)
    await db.commit()

    # Queued only after the commit, so the notice can't describe a change that
    # didn't land. `first_time=True` because "your password was changed" would
    # be untrue for an account that never had one.
    background_tasks.add_task(
        send_password_changed_notice, to_email=current_user.email, first_time=True
    )


async def update_user_profile(db: AsyncSession, body: UserUpdateRequest, current_user: User) -> User:
    # PATCH semantics: only touch fields the client actually sent. Sending an
    # explicit null clears the field; omitting it leaves the value unchanged.
    data = body.model_dump(exclude_unset=True)

    if "phone" in data:
        phone = data["phone"]
        if phone is not None:
            result = await db.execute(
                select(User).where(User.phone == phone, User.id != current_user.id)
            )
            clash = result.scalar_one_or_none()
            if clash:
                raise HTTPException(status_code=400, detail="Phone number already registered")
        # A proof covers one number. Re-saving the same number in another
        # spelling (0912… for +84912…) keeps it.
        if not same_number(current_user.phone, phone):
            current_user.phone_verified_at = None
        current_user.phone = phone

    if "full_name" in data:
        current_user.full_name = data["full_name"]

    if "bio" in data:
        current_user.bio = data["bio"]

    if "email_signature" in data:
        current_user.email_signature = data["email_signature"]

    db.add(current_user)
    await db.flush()
    return current_user


async def select_user_role(db: AsyncSession, role: Role, current_user: User) -> User:
    # One-time selection: once a role is set it can't be changed here, which
    # prevents a user from later switching into a different role on their own.
    if current_user.role:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role has already been set",
        )
    if phone_verification_pending(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verify your phone number before choosing a role",
        )
    current_user.role = role.value
    db.add(current_user)
    await db.flush()
    return current_user


def _avatar_key(user_id, ext: str) -> str:
    # One avatar per user; the deterministic key means a new upload of the same
    # type overwrites the previous object.
    return f"user/{user_id}/avatar.{ext}"


def create_avatar_presign(body: AvatarPresignRequest, current_user: User) -> tuple[str, str, int]:
    ext = AVATAR_CONTENT_TYPE_EXTENSIONS.get(body.content_type)
    if ext is None:
        allowed = ", ".join(sorted(AVATAR_CONTENT_TYPE_EXTENSIONS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported avatar content_type (allowed: {allowed})",
        )
    if body.size > AVATAR_MAX_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Avatar too large (max {AVATAR_MAX_SIZE} bytes)",
        )
    file_key = _avatar_key(current_user.id, ext)
    # Signed to the checked size; see presign_put.
    upload_url = presign_put(file_key, body.content_type, content_length=body.size)
    return file_key, upload_url, settings.R2_PRESIGN_EXPIRE_SECONDS


async def confirm_avatar(db: AsyncSession, body: AvatarConfirmRequest, current_user: User) -> User:
    # The client never picks an arbitrary key: it must be the one we issued for
    # this user, so a caller cannot point their avatar at someone else's object.
    expected_prefix = f"user/{current_user.id}/avatar."
    if not body.file_key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="file_key does not belong to this user")

    head = head_object(body.file_key)
    if head is None:
        raise HTTPException(status_code=400, detail="Object not found in storage")
    if head["size"] is not None and head["size"] > AVATAR_MAX_SIZE:
        # Delete as well as refuse, so a refused upload doesn't stay stored.
        delete_object(body.file_key)
        raise HTTPException(status_code=400, detail="Uploaded avatar too large")

    old_key = current_user.avatar_key
    if old_key and old_key != body.file_key:
        # Extension changed (e.g. png -> jpg): the old object would be orphaned.
        delete_object(old_key)

    current_user.avatar_key = body.file_key
    db.add(current_user)
    await db.flush()
    return current_user


async def change_password(
    db: AsyncSession,
    body: ChangePasswordRequest,
    current_user: User,
    background_tasks: BackgroundTasks,
    *,
    current_session_id: UUID | None = None,
    ip_address: str | None = None,
) -> int:
    """Change an existing password, proving control with the current one.

    Two things beyond swapping the hash, both of which are the point of the
    feature rather than extras:

      * every OTHER session is revoked. Changing a password because you think
        someone else is in the account achieves nothing if their session keeps
        working -- refresh tokens are independent of the password.
      * the owner is emailed. Same reasoning as set_password: an unauthorised
        change has to be visible to the real owner while they can still act.

    Returns the number of sessions revoked so the caller can tell the user.
    """
    if not current_user.password_hash:
        # No password to change; that path is set_password, which is safe
        # precisely because there is nothing to overwrite.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account has no password yet. Set one instead.",
        )

    # Through require_step_up rather than a bare verify_password, so a session
    # checking guesses here gets the same five misses as every other step-up,
    # and each miss is on the owner's security screen.
    await require_step_up(
        db,
        current_user,
        session_id=current_session_id,
        password=body.current_password,
        ip_address=ip_address,
    )

    if verify_password(body.new_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current one",
        )

    await reject_breached_password(body.new_password)
    current_user.password_hash = hash_password(body.new_password)
    db.add(current_user)

    # Spares the session making the change. It used to revoke every token
    # including this one, which the docstring above never promised: the user
    # was signed out of the device they had just secured, half an hour later
    # when the refresh failed — and at once, now that access tokens die with
    # their session.
    revoked = (
        await revoke_other_sessions(
            db, current_user.id, current_session_id=current_session_id
        )
    )["revoked"]

    await db.commit()

    # Queued after the commit so the notice can never describe a change that
    # was rolled back.
    background_tasks.add_task(send_password_changed_notice, current_user.email)
    return revoked


async def update_security_preferences(
    db: AsyncSession, current_user: User, *, signin_alerts_enabled: bool
) -> User:
    current_user.signin_alerts_enabled = signin_alerts_enabled
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return current_user


async def complete_onboarding_tour(db: AsyncSession, current_user: User) -> User:
    """Record that this account has seen the first-run walkthrough.

    Idempotent on purpose: the frontend fires this on "skip", on "got it" and
    on Escape, and a double-submit from a slow connection must not move the
    timestamp. The FIRST completion is the interesting one — it says when this
    account was onboarded — so a second call is a no-op rather than a refresh.
    """
    if current_user.onboarding_tour_completed_at is None:
        current_user.onboarding_tour_completed_at = datetime.now(timezone.utc)
        db.add(current_user)
        await db.commit()
        await db.refresh(current_user)
    return current_user
