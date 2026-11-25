import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from jwt import InvalidTokenError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import BackgroundTasks, HTTPException, status

from app.auth import totp, totp_crypto
from app.auth.email_verification import require_verified_email
from app.auth.password_policy import reject_breached_password
from app.auth.user_agent import describe_browser, describe_device
from app.utils.email import send_new_device_signin_alert
from app.auth.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UserCreate,
)
from app.lending.models import AuditLog
from app.users.models import (
    USER_STATUS_SUSPENDED,
    RefreshToken,
    TotpRecoveryCode,
    User,
    WebAuthnChallenge,
    WebAuthnCredential,
)
from app.utils.password import hash_password, verify_password
from app.utils.jwt import (
    create_access_token,
    create_password_reset_token,
    decode_password_reset_token,
    create_verification_token,
    verify_email_token,
)
from app.utils.email import (
    send_existing_account_notice,
    send_password_changed_notice,
    send_password_reset_email,
    send_sign_in_locked_notice,
    send_verification_email,
)
from app.utils.audit import append_audit
from app.utils.captcha import verify_turnstile_token_async
from app.utils.locks import advisory_xact_lock
from app.core.config import settings

logger = logging.getLogger(__name__)


async def authenticate_user(db: AsyncSession, email: str, password: str):
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    # `password_hash` is nullable (OAuth accounts have none until the user sets
    # one). Reject those before passlib sees a None hash -- it would raise
    # rather than return False, turning a normal failed login into a 500.
    if not user or not user.password_hash:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def hash_refresh_token(token: str) -> str:
    """SHA-256 of the raw refresh JWT (HANDOFF-02 Fix B).

    Deliberately NOT the argon2 hasher in app/utils/password.py: a refresh
    token is already a long, high-entropy signed JWT, not a low-entropy human
    password, so there's nothing for a slow/memory-hard hash to brute-force
    protect against -- it would only add needless CPU/memory cost on every
    single /auth/refresh call. A fast, deterministic hash for DB lookup is
    the standard approach for opaque bearer tokens (this is how GitHub/Auth0
    store API keys and session tokens for revocation checks).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_token_pair(db: AsyncSession, user_id: UUID | str,
    *,
    session_id: UUID | None = None,
    session_deadline: datetime | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
    second_factor: str | None = None,
) -> dict:
    """Issue an access/refresh JWT pair and persist the refresh token (hashed)
    in `refresh_tokens` so it can be looked up, revoked, and rotated.

    `session_id` and `session_deadline` are passed on rotation to continue an
    existing sign-in; a fresh sign-in leaves both unset and starts a new
    session whose deadline is SESSION_MAX_LIFETIME_DAYS from now. The refresh
    token never outlives that deadline, however often it is rotated.

    `second_factor` ("otp" or "passkey") is set only by a sign-in that proved
    one. It goes into both tokens as the `mfa` claim, and refresh carries it
    forward, so admin sessions keep it for their whole life (get_current_user).

    Only stages the RefreshToken row via db.add(); the caller controls the
    transaction boundary (commit), matching the rest of this module.
    """
    uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    now = datetime.now(timezone.utc)
    # A fresh sign-in starts a session; a rotation carries the existing one
    # forward so the device does not look new after every refresh.
    sid = session_id or uuid4()
    deadline = session_deadline or now + timedelta(days=settings.SESSION_MAX_LIFETIME_DAYS)

    # `sid` binds the access token to this session: get_current_user refuses it
    # once the session has no live refresh token, so logging out ends it
    # immediately instead of when it expires.
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    mfa_claim = {"mfa": second_factor} if second_factor else {}
    access_token = create_access_token(
        data={"sub": str(uid), "sid": str(sid), **mfa_claim}, expires_delta=access_token_expires
    )

    expires_at = min(now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), deadline)
    refresh_token = create_access_token(
        data={"sub": str(uid), "typ": "refresh", **mfa_claim},
        expires_delta=expires_at - now,
    )
    db.add(
        RefreshToken(
            user_id=uid,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=expires_at,
            session_id=sid,
            user_agent=user_agent,
            ip_address=ip_address,
            last_used_at=now,
        )
    )

    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}


async def _revoke_session(
    db: AsyncSession, user_id: UUID, session_id: UUID, now: datetime
) -> int:
    """Revoke every live token in one session; returns how many there were.

    A session can briefly hold more than one live token — two refreshes racing
    inside the reuse grace window each get a pair — so ending a session means
    all of them, never just the token in hand.
    """
    result = await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.session_id == session_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return result.rowcount or 0


async def _session_has_live_token(
    db: AsyncSession, user_id: UUID, session_id: UUID, now: datetime
) -> bool:
    result = await db.execute(
        select(RefreshToken.id)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.session_id == session_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


# How long a just-rotated refresh token is still honoured. Rotation makes every
# refresh token single-use, but a browser routinely sends the same one twice
# within a moment: two tabs whose access tokens lapse together, or proxy.ts
# refreshing a page load while lib/api-client.ts refreshes a query. The loser
# of that race is not a thief, and refusing it — let alone treating it as theft
# — signs the user out. Same idea and order of magnitude as Okta's 30-second
# refresh-token grace period and Auth0's "reuse interval".
REFRESH_REUSE_GRACE = timedelta(seconds=30)


async def refresh_token_pair(
    db: AsyncSession,
    refresh_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> dict:
    """Verify + rotate a refresh token (HANDOFF-02 Fix B: stateful, revocable).

    JWT signature/expiry is checked first (cheap, no DB hit for garbage
    input); the DB is then the source of truth for revocation. On success the
    presented token is revoked and a new pair is issued in the same session.

    A token that was already rotated is one of two things:

      * within REFRESH_REUSE_GRACE of its rotation, and its session still
        live: the same browser racing itself. It gets a fresh pair too.
      * later than that: a copy is being replayed — the token was stolen, or
        the thief rotated it first and this is the real user arriving second.
        Either way one of the two holders is not the user, and there is no
        telling which, so the whole session is revoked (RFC 9700 §4.14.2) and
        the event goes on the user's security history. That is the reuse
        detection; before it, a replay was merely refused and whoever had
        rotated first kept the session.

    The row is locked (FOR UPDATE) so two concurrent refreshes of one token
    are decided one after the other instead of both passing the "not revoked"
    check and silently forking the session.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
    )
    try:
        payload = jwt.decode(
            refresh_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        if payload.get("typ") != "refresh":
            raise credentials_exception
        user_id = payload.get("sub")
        if not user_id:
            raise credentials_exception
    except InvalidTokenError:
        raise credentials_exception

    token_hash = hash_refresh_token(refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    )
    stored = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if stored is None or stored.expires_at < now:
        raise credentials_exception

    if stored.revoked_at is not None:
        # Rows from before session ids existed cannot be grouped into a
        # session, so they get neither the grace window nor family revocation.
        if stored.session_id is None:
            raise credentials_exception
        session_live = await _session_has_live_token(
            db, stored.user_id, stored.session_id, now
        )
        # A live session is what tells a rotation apart from a sign-out:
        # logout and every revoke path end the whole session, so a token they
        # revoked never finds one and can never be revived by the grace window.
        if not session_live:
            raise credentials_exception
        if now - stored.revoked_at > REFRESH_REUSE_GRACE:
            revoked = await _revoke_session(db, stored.user_id, stored.session_id, now)
            append_audit(
                db,
                entity_type="SESSION",
                entity_id=stored.user_id,
                action="SESSION_REUSE_DETECTED",
                actor_id=stored.user_id,
                after_state={
                    "session_id": str(stored.session_id),
                    "sessions_revoked": revoked,
                    "user_agent": user_agent,
                },
                ip_address=ip_address,
            )
            # Committed here, not by the caller: the caller only commits on
            # success, and this revocation must stick although we refuse.
            await db.commit()
            raise credentials_exception
        # Inside the grace window: fall through and issue a pair in the same
        # session. The presented row is already revoked; leave it so.

    # Refresh authenticates by token, not by get_current_user, so it does NOT
    # inherit the suspension check from there. Without this, a suspended
    # account keeps minting fresh credentials and its session row stays alive
    # indefinitely through rotation — the access tokens would be rejected
    # downstream, but we should stop issuing them at all.
    owner = (
        await db.execute(select(User).where(User.id == UUID(str(user_id))))
    ).scalar_one_or_none()
    if owner is None or owner.status == USER_STATUS_SUSPENDED:
        raise credentials_exception

    # The session's hard deadline, counted from its original sign-in: the
    # earliest row it has (rotation keeps revoked rows, it never deletes
    # them). A pre-session-id row is its own session, so its own age.
    session_started = stored.created_at
    if stored.session_id is not None:
        session_started = (
            await db.execute(
                select(func.min(RefreshToken.created_at)).where(
                    RefreshToken.user_id == stored.user_id,
                    RefreshToken.session_id == stored.session_id,
                )
            )
        ).scalar_one()
    deadline = session_started + timedelta(days=settings.SESSION_MAX_LIFETIME_DAYS)
    if now >= deadline:
        if stored.session_id is not None:
            await _revoke_session(db, stored.user_id, stored.session_id, now)
        else:
            stored.revoked_at = now
        await db.commit()
        raise credentials_exception

    if stored.revoked_at is None:
        stored.revoked_at = now
    stored.last_used_at = now
    db.add(stored)

    # Same session, new token: the device keeps its identity on the security
    # screen, and the metadata follows the latest request rather than the
    # original sign-in (an IP can change mid-session).
    return await create_token_pair(
        db,
        user_id,
        session_id=stored.session_id,
        session_deadline=deadline,
        user_agent=user_agent or stored.user_agent,
        ip_address=ip_address or stored.ip_address,
        # The presented token is signed, so its claim is the sign-in's own.
        second_factor=payload.get("mfa"),
    )


async def logout(db: AsyncSession, refresh_token: str | None) -> dict:
    """End the session the presented refresh token belongs to (HANDOFF-02 Fix B).

    The whole session, not just this token: every live token in it is revoked,
    which also kills its access tokens at once (get_current_user checks the
    session), and leaves nothing for the reuse grace window to revive.

    Forgiving by design: a missing, already-revoked, or unrecognized token
    still returns success -- logout should never fail just because the
    client's cookie was already gone or stale.
    """
    if refresh_token:
        token_hash = hash_refresh_token(refresh_token)
        result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        stored = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if stored is not None and stored.session_id is not None:
            await _revoke_session(db, stored.user_id, stored.session_id, now)
        elif stored is not None and stored.revoked_at is None:
            stored.revoked_at = now
            db.add(stored)
    return {"status": "success", "message": "Logged out successfully"}


def _parse_user_id(user_id: str) -> UUID:
    try:
        return UUID(user_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid token details")


async def login(
    db: AsyncSession,
    login_data: LoginRequest,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> tuple[User, dict]:
    await verify_turnstile_token_async(login_data.turnstile_token)

    # Checked before the password, so a locked account answers the same way
    # whether or not the guess was right. The lock makes count, verify and
    # record one step: without it a burst of parallel guesses all read the
    # count before any failure is written.
    login_key = _login_throttle_key(login_data.email)
    await advisory_xact_lock(db, f"auth-login:{login_key}")
    failed_so_far = await _recent_failed_logins(db, login_key)
    if failed_so_far >= MAX_FAILED_LOGINS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Try again in 15 minutes.",
            # SIGN_IN_LOCKED, not the middleware's RATE_LIMITED: this wait is
            # the full window, and the sign-in page says so in its own words.
            headers={"X-Error-Code": SIGN_IN_LOCKED},
        )

    user = await authenticate_user(db, login_data.email, login_data.password)
    if not user:
        owner_id = await _record_failed_login(db, login_data.email, login_key, ip_address)
        # This miss is the one that locks the address. The advisory lock above
        # makes it exactly one request, so the owner hears once per lock.
        notify = None
        if owner_id is not None and failed_so_far + 1 == MAX_FAILED_LOGINS:
            notify = await _record_sign_in_locked(db, owner_id, ip_address)
        # Committed before raising, or the rollback would take the count with it.
        await db.commit()
        if notify is not None:
            _send_despite_the_error(
                send_sign_in_locked_notice,
                notify,
                attempts=MAX_FAILED_LOGINS,
                minutes=int(FAILED_LOGIN_WINDOW.total_seconds() // 60),
                ip_address=ip_address,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    # A suspended account cannot start a new session. get_current_user also
    # rejects one on every request, which ends sessions already in flight;
    # this stops a new one being minted in the first place, so no refresh
    # token is issued to an account an operator has shut out.
    #
    # Checked AFTER the password, deliberately: answering "suspended" to a
    # wrong password would confirm the address is registered.
    if user.status == USER_STATUS_SUSPENDED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been suspended",
        )
    # Second factor, if the account has one. Nothing is issued here: no access
    # token, no refresh token, no session row — only a short-lived challenge the
    # caller must trade for tokens by proving possession of a code. Returning
    # tokens now and "requiring" the code afterwards would make 2FA cosmetic,
    # since the tokens would already work.
    if user.totp_enabled:
        return user, {"totp_required": True, "challenge_token": totp.create_challenge_token(user.id)}

    # "Have we seen this device before?" is asked BEFORE the new session row is
    # written, otherwise the sign-in we are about to record would itself count
    # as prior history and no device would ever look new.
    is_new_device = await is_unseen_device(db, user.id, user_agent)

    tokens = await create_token_pair(
        db, user.id, user_agent=user_agent, ip_address=ip_address
    )
    await db.commit()

    if is_new_device and user.signin_alerts_enabled and background_tasks is not None:
        # After the commit: the alert describes a sign-in that actually
        # happened. Queued rather than awaited so a slow mail server cannot
        # make signing in feel broken.
        background_tasks.add_task(
            send_new_device_signin_alert,
            user.email,
            device=describe_device(user_agent),
            browser=describe_browser(user_agent),
            ip_address=ip_address,
        )

    return user, tokens


# Wrong passwords allowed per email address inside the window before /login
# stops checking them. Per address, not per IP: until TRUSTED_PROXY_HOPS is
# set, every request behind the load balancer shares one peer address, so a
# per-IP counter would be a single global one. Keyed by address rather than
# account so an unknown address locks the same way a real one does; locking
# only real accounts would tell a caller which addresses are registered.
MAX_FAILED_LOGINS = 10
FAILED_LOGIN_WINDOW = timedelta(minutes=15)
SIGN_IN_FAILED = "SIGN_IN_FAILED"
SIGN_IN_LOCKED = "SIGN_IN_LOCKED"
# The owner is emailed when their address locks, but at most this often:
# a guesser who waits out every lock would otherwise get to send them an
# email every 15 minutes. Every lock still lands on the security screen.
LOCK_NOTICE_INTERVAL = timedelta(hours=1)

# Strong references to notices still being sent: the event loop keeps only
# weak ones, so an otherwise unreferenced task can vanish before it runs.
_background_notices: set[asyncio.Task] = set()


def _send_despite_the_error(send, /, *args, **kwargs) -> None:
    """Send an email on a worker thread without holding up the response, for
    a request that is about to fail.

    BackgroundTasks cannot do this: FastAPI runs that queue only with a
    response the endpoint returned, and an endpoint that raises HTTPException
    returns none, so add_task would never send.
    """
    task = asyncio.create_task(asyncio.to_thread(send, *args, **kwargs))
    _background_notices.add(task)
    task.add_done_callback(_notice_done)


def _notice_done(task: asyncio.Task) -> None:
    _background_notices.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # Nobody awaits the task, so this is the only place the failure shows.
        logger.error("Security notice not sent", exc_info=task.exception())


def _login_throttle_key(email: str) -> str:
    """A keyed digest of the normalised address, so audit rows for unknown
    addresses do not store whatever a caller typed."""
    return hmac.new(
        settings.SECRET_KEY.encode(),
        f"login:{email.strip().lower()}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


async def _recent_failed_logins(db: AsyncSession, login_key: str) -> int:
    since = datetime.now(timezone.utc) - FAILED_LOGIN_WINDOW
    result = await db.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.action == SIGN_IN_FAILED,
            AuditLog.after_state["login_key"].astext == login_key,
            AuditLog.created_at >= since,
        )
    )
    return result.scalar_one()


async def _record_failed_login(
    db: AsyncSession, email: str, login_key: str, ip_address: str | None
) -> UUID | None:
    """Record a wrong password; return the account it was for, if any."""
    # When the address has an account, the row is on the owner's security
    # history (actor = the account, as for TWO_FACTOR_FAILED), so a stream of
    # wrong passwords is something they can see.
    owner_id = (
        await db.execute(select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=owner_id,
        action=SIGN_IN_FAILED,
        actor_id=owner_id,
        after_state={"login_key": login_key},
        ip_address=ip_address,
    )
    return owner_id


async def _record_sign_in_locked(
    db: AsyncSession, owner_id: UUID, ip_address: str | None
) -> str | None:
    """Put the lock on the owner's security history; return the address to
    email about it, or None if they were emailed within LOCK_NOTICE_INTERVAL."""
    since = datetime.now(timezone.utc) - LOCK_NOTICE_INTERVAL
    emailed_recently = (
        await db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.entity_id == owner_id,
                AuditLog.action == SIGN_IN_LOCKED,
                AuditLog.after_state["notified"].astext == "true",
                AuditLog.created_at >= since,
            )
        )
    ).scalar_one() > 0
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=owner_id,
        action=SIGN_IN_LOCKED,
        actor_id=owner_id,
        after_state={"notified": not emailed_recently},
        ip_address=ip_address,
    )
    if emailed_recently:
        return None
    return (
        await db.execute(select(User.email).where(User.id == owner_id))
    ).scalar_one()


async def is_unseen_device(
    db: AsyncSession, user_id: UUID, user_agent: str | None
) -> bool:
    """Whether this user agent has never signed in to this account before.

    Deliberately a user-agent match, not a fingerprint: it is the only signal
    stored, it is stable for a given browser on a given machine, and the cost of
    a false "new device" is one extra email rather than a missed warning. An
    absent user agent is treated as NOT new -- an unknown client would otherwise
    alert on every single sign-in.

    An account's first sign-in ever is NOT new either: there is no earlier
    device to compare it with, and since alerts are on by default, every new
    account (a Google sign-up included) would otherwise be told about itself.
    Session rows are revoked, never deleted, so "no rows" means "never signed
    in", not "signed out everywhere".
    """
    if not user_agent:
        return False
    signed_in_before = select(RefreshToken.id).where(
        RefreshToken.user_id == user_id
    ).exists()
    seen_this_device = select(RefreshToken.id).where(
        RefreshToken.user_id == user_id,
        RefreshToken.user_agent == user_agent,
    ).exists()
    result = await db.execute(select(signed_in_before, seen_this_device))
    has_history, seen = result.one()
    return has_history and not seen


async def list_sessions(
    db: AsyncSession, user_id: UUID, *, current_session_id: UUID | None = None
) -> list[dict]:
    """Live sign-ins for a user, most recently used first.

    One entry per session. Rotation revokes the previous row, so a session
    normally has exactly one live row — but two refreshes racing inside the
    reuse grace window leave it two for a while, and listing both would show
    one device twice. The caller's own session (the `sid` of the access token
    it presented) is flagged so the UI can label it and refuse to revoke it.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .order_by(RefreshToken.last_used_at.desc().nullslast())
    )

    sessions: dict[UUID, dict] = {}
    for row in result.scalars().all():
        # A pre-migration row has no session_id and cannot be addressed by the
        # revoke endpoints, so listing it would offer an action that fails.
        # setdefault keeps the first row seen, i.e. the most recently used.
        if row.session_id is None:
            continue
        sessions.setdefault(
            row.session_id,
            {
                "session_id": row.session_id,
                "user_agent": row.user_agent,
                "ip_address": row.ip_address,
                "created_at": row.created_at,
                "last_used_at": row.last_used_at,
                "current": row.session_id == current_session_id,
            },
        )
    return list(sessions.values())


async def revoke_session(
    db: AsyncSession,
    user_id: UUID,
    session_id: UUID,
    *,
    current_session_id: UUID | None,
) -> dict:
    """Revoke every live token in one session — signing that device out.

    Scoped to the caller's own user_id: a session id is a UUID, but it must
    never be usable to sign out somebody else's device.
    """
    if not await _session_has_live_token(
        db, user_id, session_id, datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=404, detail="Session not found")

    if session_id == current_session_id:
        # Signing out the device you are using is logout, not session
        # management — different endpoint, and it also clears the cookies.
        raise HTTPException(
            status_code=400,
            detail="Cannot revoke the session you are signed in with; use logout",
        )

    revoked = await _revoke_session(db, user_id, session_id, datetime.now(timezone.utc))
    return {"revoked": revoked}


async def revoke_other_sessions(
    db: AsyncSession, user_id: UUID, *, current_session_id: UUID | None
) -> dict:
    """Sign out everywhere except here — the "I think someone else is in my
    account" button. The caller's own session is deliberately spared; with no
    current session (a password reset, made signed-out) every one is revoked.

    Counts sessions, not tokens, so "3 devices signed out" means three devices
    even if one of them was mid-rotation."""
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    now = datetime.now(timezone.utc)
    ended: set[UUID | None] = set()
    for row in result.scalars().all():
        if current_session_id is not None and row.session_id == current_session_id:
            continue
        row.revoked_at = now
        db.add(row)
        # A pre-session-id row is a sign-in of its own.
        ended.add(row.session_id if row.session_id is not None else row.id)
    return {"revoked": len(ended)}


async def _remove_passkeys(db: AsyncSession, user_id: UUID) -> int:
    """Delete every passkey (and any half-finished ceremony) on an account.
    Returns how many passkeys were removed."""
    result = await db.execute(delete(WebAuthnCredential).where(WebAuthnCredential.user_id == user_id))
    await db.execute(delete(WebAuthnChallenge).where(WebAuthnChallenge.user_id == user_id))
    return result.rowcount or 0


async def _clear_totp(db: AsyncSession, user: User) -> None:
    """Turn 2FA off and drop its secret and recovery codes."""
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_confirmed_at = None
    await db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id))


async def register_user(
    db: AsyncSession, user_in: UserCreate, background_tasks: BackgroundTasks
) -> dict:
    """Register a new user without leaking whether the email already exists.

      - new address         -> create the user, email a verification link
      - email already taken -> same acknowledgement as success, and email the
                               real owner an "account exists" notice, so the
                               endpoint can't be used to enumerate accounts
                               (mirrors forgot_password)
      - phone already taken -> reject with an explicit 400

    The phone branch is deliberately NOT anti-enumerable. It was silent between
    a333de9 and this change, which meant a legitimate signup whose number was
    already on file got the "verification email sent" screen and then nothing —
    no account, no email, no way to find out why. Product call: a user being
    able to fix their own signup beats phone-number enumeration resistance,
    which the email branch does not provide anyway once you know the address.
    """
    await verify_turnstile_token_async(user_in.turnstile_token)
    # Before the email lookup, so a refusal says nothing about the account.
    await reject_breached_password(user_in.password)

    # Shared by the "created" and "email already taken" outcomes so the two are
    # indistinguishable. Wording must not imply success or failure of account
    # creation specifically.
    ack = {
        "status": "success",
        "message": "If these details are available, a verification email has been sent.",
    }

    existing_email = (
        await db.execute(select(User).where(User.email == user_in.email))
    ).scalar_one_or_none()
    if existing_email is not None:
        # Don't create or reveal — tell the genuine owner someone used their email.
        background_tasks.add_task(send_existing_account_notice, to_email=user_in.email)
        return ack

    if user_in.phone:
        existing_phone = (
            await db.execute(select(User).where(User.phone == user_in.phone))
        ).scalar_one_or_none()
        if existing_phone is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone number already registered",
            )

    new_user = User(
        email=user_in.email,
        phone=user_in.phone,
        full_name=user_in.full_name,
        password_hash=hash_password(user_in.password),
        role=None,  # chosen later via PUT /users/me/role
        status="ACTIVE",
        email_verified=False,
    )
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent signup on the same email/phone (the DB
        # unique constraints are the real guard). Roll back and return the same
        # acknowledgement so the collision still isn't observable.
        await db.rollback()
        return ack
    await db.refresh(new_user)

    # Generate verification token and send verification email in background.
    token = create_verification_token(new_user.id)
    background_tasks.add_task(send_verification_email, to_email=new_user.email, token=token)

    return ack


async def verify_email(db: AsyncSession, token: str) -> dict:
    uid = _parse_user_id(verify_email_token(token))

    result = await db.execute(select(User).where(User.id == uid).with_for_update())
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.email_verified:
        return {"status": "success", "message": "Email already verified"}

    # A secret staged or enabled before this check existed has no proof of ownership.
    if user.totp_enabled or user.totp_secret:
        await _clear_totp(db, user)
    user.email_verified = True
    await db.commit()
    return {"status": "success", "message": "Email verified successfully"}


# Resend is public and every call that reaches an unverified account mails it,
# so without a limit it is a mail bomb aimed at any address registered once.
# Per account: one email per cooldown, and a ceiling per day. Counted from
# audit_logs, like the 2FA limit.
VERIFICATION_RESEND_COOLDOWN = timedelta(minutes=2)
MAX_VERIFICATION_EMAILS_PER_DAY = 5
VERIFICATION_EMAIL_SENT = "VERIFICATION_EMAIL_SENT"

# The one answer resend gives, whatever happened.
RESEND_VERIFICATION_ACK = {
    "status": "success",
    "message": "If this email is registered and not yet verified, a verification link has been sent.",
}


async def resend_verification(
    db: AsyncSession,
    body: ResendVerificationRequest,
    background_tasks: BackgroundTasks,
    *,
    ip_address: str | None = None,
) -> dict:
    """Mail a fresh verification link, at most once per cooldown per account.

    Unknown, already verified, throttled and sent all return the same
    message. Distinct answers told a caller which addresses have accounts
    (register and forgot-password already refuse to), and a throttled answer
    would tell them the same thing.
    """
    await verify_turnstile_token_async(body.turnstile_token)

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user is None or user.email_verified:
        return dict(RESEND_VERIFICATION_ACK)

    # Count-then-send: without the lock a burst of parallel calls all read
    # the same count and each sends.
    await advisory_xact_lock(db, f"auth-resend-verification:{user.id}")
    now = datetime.now(timezone.utc)
    sent_today, last_sent = (
        await db.execute(
            select(func.count(AuditLog.id), func.max(AuditLog.created_at)).where(
                AuditLog.entity_id == user.id,
                AuditLog.action == VERIFICATION_EMAIL_SENT,
                AuditLog.created_at >= now - timedelta(days=1),
            )
        )
    ).one()
    if sent_today >= MAX_VERIFICATION_EMAILS_PER_DAY or (
        last_sent is not None and now - last_sent < VERIFICATION_RESEND_COOLDOWN
    ):
        # Nothing to write; ends the transaction so the lock goes with it.
        await db.commit()
        return dict(RESEND_VERIFICATION_ACK)

    # No actor: the caller is anonymous, and this is not something the owner
    # did, so it stays off their security history.
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=user.id,
        action=VERIFICATION_EMAIL_SENT,
        actor_id=None,
        ip_address=ip_address,
    )
    await db.commit()

    token = create_verification_token(user.id)
    background_tasks.add_task(send_verification_email, to_email=user.email, token=token)
    return dict(RESEND_VERIFICATION_ACK)


async def forgot_password(db: AsyncSession, body: ForgotPasswordRequest, background_tasks: BackgroundTasks) -> dict:
    await verify_turnstile_token_async(body.turnstile_token)

    # Same response whether or not the email exists, to avoid user enumeration.
    response = {"status": "success", "message": "If the email is registered, a password reset link has been sent."}

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user:
        return response

    token = issue_password_reset_token(user)
    background_tasks.add_task(send_password_reset_email, to_email=user.email, token=token)
    return response


def password_fingerprint(password_hash: str | None) -> str:
    """A keyed digest of the account's current password hash.

    Put in a reset token (``pwv``) and checked at reset, it makes the token
    single-use without a table of spent tokens: resetting changes the hash, so
    the fingerprint no longer matches and the same link is dead. It also kills
    every other outstanding link the moment the password changes by any route.
    Keyed with SECRET_KEY because the token is readable by whoever holds it,
    and a bare hash of the hash would still be something to attack offline.
    An account with no password yet fingerprints the empty string.
    """
    return hmac.new(
        settings.SECRET_KEY.encode(), (password_hash or "").encode(), hashlib.sha256
    ).hexdigest()[:32]


def issue_password_reset_token(user: User) -> str:
    return create_password_reset_token(
        user.id, password_fingerprint=password_fingerprint(user.password_hash)
    )


async def reset_password(
    db: AsyncSession, body: ResetPasswordRequest, background_tasks: BackgroundTasks
) -> dict:
    user_id, token_fingerprint = decode_password_reset_token(body.token)
    uid = _parse_user_id(user_id)

    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Single use. The token only works against the password it was issued for:
    # once that password changes (by this reset or any other way) the link is
    # spent. A token with no fingerprint (issued before this check existed) is
    # refused the same way. Same message as an expired token, deliberately.
    if token_fingerprint is None or not hmac.compare_digest(
        token_fingerprint, password_fingerprint(user.password_hash)
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    await reject_breached_password(body.new_password)
    user.password_hash = hash_password(body.new_password)
    # The link was delivered to the address, so following it proves control of
    # it, the same proof verify_email accepts.
    was_verified = user.email_verified
    user.email_verified = True
    # Sign out every session. A reset is what someone does when they think the
    # account is compromised; leaving existing sessions alive meant whoever was
    # already in stayed in with the new password set. Unlike change_password
    # there is no "current" session to spare: this request is unauthenticated.
    revoked = await revoke_other_sessions(db, user.id, current_session_id=None)
    # Passkeys go too, for the same reason. A passkey is a sign-in of its own
    # that never asks for the password, so one added by whoever was in (a
    # stolen session, or a stranger who registered this address before its
    # owner) would otherwise outlive the reset and keep them in.
    passkeys_removed = await _remove_passkeys(db, user.id)
    # On an account nobody had proven the address for, 2FA was enrolled by an
    # unproven party too, and left in place it locks the real owner out behind
    # a stranger's authenticator. Same rule as the OAuth reclaim. On a verified
    # account 2FA stays: it is what stops a reset link alone taking it over.
    totp_cleared = False
    if not was_verified and (user.totp_enabled or user.totp_secret):
        await _clear_totp(db, user)
        totp_cleared = True
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=user.id,
        action="PASSWORD_RESET",
        actor_id=user.id,
        after_state={
            "sessions_revoked": revoked["revoked"],
            "passkeys_removed": passkeys_removed,
            "totp_cleared": totp_cleared,
        },
    )
    await db.commit()

    # Queued only after the commit: the notice must never describe a change
    # that didn't land. It's what makes an unauthorised reset visible to the
    # real owner while they can still act on it.
    background_tasks.add_task(send_password_changed_notice, to_email=user.email)

    return {"status": "success", "message": "Password reset successfully"}


async def list_security_events(
    db: AsyncSession, user_id: UUID, *, limit: int = 20, offset: int = 0
):
    """Recent security-relevant audit rows for one account, newest first.

    Filtered to SESSION/AUTH entity types: the audit log also carries business
    events (score runs, contracts) which belong on an admin trail, not on a
    user's security screen.
    """
    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.actor_id == user_id,
            AuditLog.entity_type.in_(("SESSION", "AUTH")),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


# --------------------------------------------------------------------------- #
# Two-factor authentication (TOTP)
#
# Enrolment is deliberately three steps rather than one:
#
#   setup   mint a secret, return the otpauth:// URI. Nothing is enabled yet, so
#           an abandoned setup cannot lock anyone out.
#   enable  prove possession with a live code. Only now does totp_enabled flip,
#           and the recovery codes are generated and shown exactly once.
#   disable requires the current password AND a live code (or a recovery code) —
#           a borrowed session must not be able to strip the account's second
#           factor, which is the whole point of having one.
# --------------------------------------------------------------------------- #


async def start_totp_setup(db: AsyncSession, current_user: User) -> tuple[str, str]:
    """Mint (or re-mint) a secret and return (secret, provisioning_uri).

    Re-running setup on an account that has not finished enrolling issues a
    FRESH secret: the previous one may have been half-scanned into an app the
    user has since deleted, and a stale entry that never produces an accepted
    code is worse than starting over.

    Refused outright once 2FA is on — rotating a live secret would silently
    invalidate the authenticator the user is currently relying on. Disable
    first, which requires the password.
    """
    require_verified_email(current_user, method="an authenticator app")
    if current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Two-factor authentication is already enabled",
        )
    # Checked before anything is generated, so a server without a key never
    # writes a secret it cannot protect.
    if not totp_crypto.is_configured():
        # Delegated so there is one message and one log line, not two copies.
        totp_crypto.require_configured()

    secret = totp.generate_secret()
    # Encrypted before it touches the row; the plaintext only ever leaves in
    # this response, for the QR code the client is about to render.
    current_user.totp_secret = totp_crypto.encrypt_secret(secret)
    db.add(current_user)
    await db.flush()

    return secret, totp.provisioning_uri(secret, current_user.email)


async def enable_totp(db: AsyncSession, current_user: User, code: str) -> list[str]:
    """Verify a code, turn 2FA on, and return one-time recovery codes."""
    require_verified_email(current_user, method="an authenticator app")
    if current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Two-factor authentication is already enabled",
        )
    secret = totp_crypto.decrypt_secret(current_user.totp_secret)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Start two-factor setup before enabling it",
        )
    if not totp.verify_code(secret, code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That code is not valid. Check your authenticator app and try again.",
        )

    current_user.totp_enabled = True
    current_user.totp_confirmed_at = datetime.now(timezone.utc)
    db.add(current_user)

    # Replace any codes from an earlier enrolment: a code printed before the
    # secret was re-minted must not still open the account.
    await _delete_recovery_codes(db, current_user.id)
    plaintext = totp.generate_recovery_codes()
    for code_value in plaintext:
        db.add(
            TotpRecoveryCode(
                user_id=current_user.id, code_hash=totp.hash_recovery_code(code_value)
            )
        )
    await db.flush()

    return plaintext


# --- Step-up: proving it is still the owner before changing sign-in methods ---
#
# Adding a passkey or an authenticator app, and turning 2FA off, are what an
# attacker holding a stolen session does next: a passkey they register keeps
# signing them in after the owner changes the password (only a reset removes
# passkeys), and an authenticator they enrol on an account without 2FA locks
# the owner out. A session alone must not be enough for any of them.
#
# Accounts with a password re-enter it. OAuth-only accounts have nothing to
# re-enter, so they need a session that signed in moments ago instead: a
# copied cookie or refresh token carries its session's ORIGINAL sign-in time
# through every rotation, so it cannot pass this once that sign-in is old.
STEP_UP_FAILED = "STEP_UP_FAILED"
MAX_STEP_UP_FAILURES = 5
STEP_UP_FAILURE_WINDOW = timedelta(minutes=15)
STEP_UP_MAX_SESSION_AGE = timedelta(minutes=10)


async def _recent_step_up_failures(
    db: AsyncSession, user_id: UUID, session_id: UUID | None
) -> int:
    since = datetime.now(timezone.utc) - STEP_UP_FAILURE_WINDOW
    query = select(func.count(AuditLog.id)).where(
        AuditLog.entity_id == user_id,
        AuditLog.action == STEP_UP_FAILED,
        AuditLog.created_at >= since,
    )
    # Counted per session, not per account. Per account, whoever held a
    # stolen session could miss five times every 15 minutes and keep the
    # owner, on their own session, from turning 2FA off or adding a passkey
    # for as long as they liked. A session only comes from a sign-in, so this
    # gives a thief no more guesses than before: still five, on the one
    # session they hold. (No session id: the account, as before.)
    if session_id is not None:
        query = query.where(AuditLog.after_state["session_id"].astext == str(session_id))
    return (await db.execute(query)).scalar_one()


async def _lock_step_up(db: AsyncSession, user: User, session_id: UUID | None) -> None:
    """Serialise step-up checks per account and refuse once over the limit.

    Locked so that count, verify and record are one step: without it, parallel
    requests all read the count before any failure is written, and a burst of
    hundreds got hundreds of password guesses (see complete_totp_login).
    """
    await advisory_xact_lock(db, f"auth-step-up:{user.id}")
    if await _recent_step_up_failures(db, user.id, session_id) >= MAX_STEP_UP_FAILURES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts. Try again in 15 minutes.",
            headers={"X-Error-Code": "STEP_UP_LOCKED"},
        )


async def _record_step_up_failure(
    db: AsyncSession,
    user: User,
    session_id: UUID | None,
    ip_address: str | None,
    detail: str,
) -> HTTPException:
    # Committed before the caller raises, or the rollback would take the count
    # with it. AUTH, so every miss is on the owner's security screen: a wrong
    # password from a live session means someone else is holding it.
    append_audit(
        db,
        entity_type="AUTH",
        entity_id=user.id,
        action=STEP_UP_FAILED,
        actor_id=user.id,
        after_state={"session_id": str(session_id)} if session_id else None,
        ip_address=ip_address,
    )
    await db.commit()
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def _require_recent_sign_in(
    db: AsyncSession, user: User, session_id: UUID | None
) -> None:
    """Step-up for an account with no password: a session that signed in
    within STEP_UP_MAX_SESSION_AGE, counted from its ORIGINAL sign-in."""
    started = None
    if session_id is not None:
        started = (
            await db.execute(
                select(func.min(RefreshToken.created_at)).where(
                    RefreshToken.user_id == user.id,
                    RefreshToken.session_id == session_id,
                )
            )
        ).scalar_one()
    if started is None or datetime.now(timezone.utc) - started > STEP_UP_MAX_SESSION_AGE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="For your security, sign in again to continue.",
            headers={"X-Error-Code": "REAUTH_REQUIRED"},
        )


async def require_step_up(
    db: AsyncSession,
    user: User,
    *,
    session_id: UUID | None,
    password: str | None,
    ip_address: str | None = None,
) -> None:
    """Gate adding a sign-in method on proof beyond the session itself."""
    if not user.password_hash:
        await _require_recent_sign_in(db, user, session_id)
        return
    if not password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Enter your password to continue.",
            headers={"X-Error-Code": "PASSWORD_REQUIRED"},
        )
    await _lock_step_up(db, user, session_id)
    if not verify_password(password, user.password_hash):
        raise await _record_step_up_failure(
            db, user, session_id, ip_address, "Password is incorrect"
        )


async def disable_totp(
    db: AsyncSession,
    current_user: User,
    password: str | None,
    code: str,
    *,
    session_id: UUID | None = None,
    ip_address: str | None = None,
) -> None:
    """Turn 2FA off. Requires the password (or, for an account without one, a
    fresh sign-in) AND a second-factor code.

    Both checks share the step-up attempt limit. Without one, a stolen session
    had an unlimited oracle here: first for the password ("Password is
    incorrect"), then for the code, and at the end the second factor gone.
    """
    if not current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Two-factor authentication is not enabled",
        )
    if not current_user.password_hash:
        # OAuth-only accounts can enrol TOTP on a fresh sign-in, so they turn
        # it off the same way. Asking for a password they do not have made
        # 2FA impossible to remove, and every try counted as a failure.
        await _require_recent_sign_in(db, current_user, session_id)
        await _lock_step_up(db, current_user, session_id)
    else:
        await _lock_step_up(db, current_user, session_id)
        if not password or not verify_password(password, current_user.password_hash):
            raise await _record_step_up_failure(
                db, current_user, session_id, ip_address, "Password is incorrect"
            )

    # A recovery code is accepted here on purpose: someone who has lost their
    # authenticator needs a way out that is not "contact support", and they have
    # already proved knowledge of the password (or signed in fresh) above.
    if not await _consume_second_factor(db, current_user, code):
        raise await _record_step_up_failure(
            db,
            current_user,
            session_id,
            ip_address,
            "That code is not valid. Use a code from your app or a recovery code.",
        )

    current_user.totp_enabled = False
    current_user.totp_secret = None
    current_user.totp_confirmed_at = None
    db.add(current_user)
    await _delete_recovery_codes(db, current_user.id)
    await db.flush()


async def complete_totp_login(
    db: AsyncSession,
    challenge_token: str,
    code: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> tuple[User, dict]:
    """Second half of a 2FA login: exchange a challenge token + code for tokens."""
    user_id = totp.verify_challenge_token(challenge_token)

    result = await db.execute(select(User).where(User.id == UUID(str(user_id))))
    user = result.scalar_one_or_none()
    if user is None or not user.totp_enabled:
        # Covers a challenge token minted before 2FA was turned off, and a
        # deleted account. Same message either way — this endpoint is reachable
        # without a session, so it must not become an account oracle.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired two-factor challenge",
        )

    # A six-digit code is only a second factor if it can't be guessed. Without
    # a limit, whoever had the password could re-mint challenges and try codes
    # until one landed (about 1 in 330,000 per guess, three codes being live
    # at once). Counted per account, not per challenge, for that reason.
    #
    # Locked so that count, verify and record are one step per account.
    # Without it, parallel requests all read the count before any failure is
    # written, and a burst of hundreds got hundreds of guesses.
    await advisory_xact_lock(db, f"auth-2fa:{user.id}")
    if await _recent_second_factor_failures(db, user.id) >= MAX_SECOND_FACTOR_FAILURES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect codes. Try again in 15 minutes.",
        )

    if not await _consume_second_factor(db, user, code):
        # Committed before raising, or the rollback would take the count with
        # it. AUTH, so it is on the owner's security screen: a wrong code here
        # means someone got past their password.
        append_audit(
            db,
            entity_type="AUTH",
            entity_id=user.id,
            action=SECOND_FACTOR_FAILED,
            actor_id=user.id,
            ip_address=ip_address,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That code is not valid",
        )

    # Asked before the session row is written, same as in login() — and it has
    # to happen here rather than in step one, or enabling 2FA would silently
    # switch new-device alerts off for the accounts that most want them.
    is_new_device = await is_unseen_device(db, user.id, user_agent)

    tokens = await create_token_pair(
        db, user.id, user_agent=user_agent, ip_address=ip_address, second_factor="otp"
    )
    await db.commit()

    if is_new_device and user.signin_alerts_enabled and background_tasks is not None:
        background_tasks.add_task(
            send_new_device_signin_alert,
            user.email,
            device=describe_device(user_agent),
            browser=describe_browser(user_agent),
            ip_address=ip_address,
        )

    return user, tokens


# Wrong codes allowed per account inside the window before /login/2fa stops
# checking them. Five per 15 minutes means about one lucky guess per two years
# of guessing around the clock, every miss on the owner's security screen as a
# critical event, and costs a fumbling owner a short wait at worst.
MAX_SECOND_FACTOR_FAILURES = 5
SECOND_FACTOR_FAILURE_WINDOW = timedelta(minutes=15)
SECOND_FACTOR_FAILED = "TWO_FACTOR_FAILED"


async def _recent_second_factor_failures(db: AsyncSession, user_id: UUID) -> int:
    since = datetime.now(timezone.utc) - SECOND_FACTOR_FAILURE_WINDOW
    result = await db.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.entity_id == user_id,
            AuditLog.action == SECOND_FACTOR_FAILED,
            AuditLog.created_at >= since,
        )
    )
    return result.scalar_one()


async def _consume_second_factor(db: AsyncSession, user: User, code: str) -> bool:
    """True if `code` is a valid TOTP code or an unused recovery code.

    A recovery code is marked used before this returns, so it is single-use even
    if the caller retries with the same value.
    """
    secret = totp_crypto.decrypt_secret(user.totp_secret)
    if secret and totp.verify_code(secret, code):
        return True

    result = await db.execute(
        select(TotpRecoveryCode).where(
            TotpRecoveryCode.user_id == user.id,
            TotpRecoveryCode.used_at.is_(None),
        )
    )
    for row in result.scalars().all():
        if totp.recovery_code_matches(code, row.code_hash):
            row.used_at = datetime.now(timezone.utc)
            db.add(row)
            await db.flush()
            return True

    return False


async def _delete_recovery_codes(db: AsyncSession, user_id: UUID) -> None:
    result = await db.execute(
        select(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user_id)
    )
    for row in result.scalars().all():
        await db.delete(row)


async def count_unused_recovery_codes(db: AsyncSession, user_id: UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(TotpRecoveryCode)
        .where(
            TotpRecoveryCode.user_id == user_id,
            TotpRecoveryCode.used_at.is_(None),
        )
    )
    return result.scalar_one() or 0
