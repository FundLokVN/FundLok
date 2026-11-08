"""Prove a phone number with a one-time code sent through Stringee.

Spec: docs/specs/users/phone-verification.md

Every send costs money and lands on a stranger's phone if the number is wrong,
so sending is limited three ways: a cooldown per account, a daily ceiling per
account, and a daily ceiling per number (one number targeted from many
accounts). Counted from phone_verification_codes under advisory locks, like
the resend-verification limit, so a burst of parallel calls cannot slip past.
"""
import hashlib
import hmac
import logging
import math
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.phone_verification import stringee
from app.phone_verification.models import PhoneVerificationCode
from app.phone_verification.phone import equivalent_forms, stringee_number
from app.users.models import User
from app.utils.audit import append_audit
from app.utils.locks import advisory_xact_lock

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=5)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_SENDS_PER_USER_PER_DAY = 5
MAX_SENDS_PER_PHONE_PER_DAY = 5
MAX_ATTEMPTS_PER_CODE = 5

PHONE_VERIFIED = "PHONE_VERIFIED"
PHONE_RELEASED = "PHONE_RELEASED"

ALREADY_REGISTERED = "Phone number already registered"
CODE_EXPIRED = "This code has expired. Request a new one."
CODE_INCORRECT = "Incorrect code"
TOO_MANY_ATTEMPTS = "Too many incorrect attempts. Request a new code."


def _hash_code(row_id: UUID, code: str) -> str:
    # Bound to the row, so a hash copied onto another row proves nothing.
    message = f"{row_id}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"


async def _verified_holder(db: AsyncSession, phone: str, user_id: UUID) -> User | None:
    """Another account that has already proven this number, if any."""
    result = await db.execute(
        select(User).where(
            User.phone.in_(equivalent_forms(phone)),
            User.id != user_id,
            User.phone_verified_at.is_not(None),
        )
    )
    return result.scalars().first()


def _channel() -> str:
    channel = settings.STRINGEE_OTP_CHANNEL
    if channel == "log" and not settings.DEBUG:
        # A code in the server log is a code anyone with log access can use.
        raise stringee.StringeeNotConfigured("STRINGEE_OTP_CHANNEL=log is honoured only with DEBUG=True")
    return channel


async def _deliver(channel: str, phone: str, code: str) -> None:
    to = stringee_number(phone)
    if channel == "sms":
        minutes = int(CODE_TTL.total_seconds() // 60)
        text = settings.STRINGEE_SMS_TEMPLATE.replace("{code}", code).replace("{minutes}", str(minutes))
        await stringee.send_sms(to, text)
    elif channel == "voice":
        await stringee.call_with_code(to, code)
    else:
        logger.warning("Phone verification code for %s: %s (STRINGEE_OTP_CHANNEL=log)", phone, code)


def _too_many(detail: str, retry_after: timedelta) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(max(1, math.ceil(retry_after.total_seconds())))},
    )


async def send_code(db: AsyncSession, user: User, phone: str) -> PhoneVerificationCode:
    """Text (or call) a fresh code to `phone`, a canonical +84 number.

    The new row retires any earlier code: only the latest can be redeemed.
    """
    # Registration's captcha and the emailed link both stand between a bot and
    # this endpoint; a Google sign-in arrives with its email already verified.
    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verify your email address first")
    if user.phone_verified_at is not None and user.phone in equivalent_forms(phone):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This phone number is already verified")
    if await _verified_holder(db, phone, user.id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ALREADY_REGISTERED)

    # Always user then number, so two callers can never hold them crosswise.
    await advisory_xact_lock(db, f"phone-verification:user:{user.id}")
    await advisory_xact_lock(db, f"phone-verification:phone:{phone}")
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)

    def sends_since(*where):
        return select(
            func.count(PhoneVerificationCode.id),
            func.min(PhoneVerificationCode.created_at),
            func.max(PhoneVerificationCode.created_at),
        ).where(PhoneVerificationCode.created_at >= day_ago, *where)

    user_sends, user_first, user_last = (
        await db.execute(sends_since(PhoneVerificationCode.user_id == user.id))
    ).one()
    number_sends, number_first, _ = (
        await db.execute(sends_since(PhoneVerificationCode.phone == phone))
    ).one()

    daily_limit = "Too many codes requested today. Try again later."
    refusal = None
    if user_last is not None and now - user_last < RESEND_COOLDOWN:
        refusal = _too_many("Please wait before requesting another code", RESEND_COOLDOWN - (now - user_last))
    elif user_sends >= MAX_SENDS_PER_USER_PER_DAY:
        refusal = _too_many(daily_limit, user_first + timedelta(days=1) - now)
    elif number_sends >= MAX_SENDS_PER_PHONE_PER_DAY:
        refusal = _too_many(daily_limit, number_first + timedelta(days=1) - now)
    if refusal is not None:
        # Nothing to write; ends the transaction so the locks go with it.
        await db.commit()
        raise refusal

    code = _new_code()
    try:
        channel = _channel()
        row = PhoneVerificationCode(
            id=uuid4(),
            user_id=user.id,
            phone=phone,
            channel=channel,
            expires_at=now + CODE_TTL,
            created_at=now,
        )
        row.code_hash = _hash_code(row.id, code)
        db.add(row)
        # Before the send: a database error should not cost an SMS.
        await db.flush()
        await _deliver(channel, phone, code)
    except stringee.StringeeNotConfigured as exc:
        await db.rollback()
        logger.error("Phone verification unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Phone verification is not available right now",
        ) from exc
    except stringee.StringeeError as exc:
        # Rolled back, so an undelivered code neither counts against the
        # limits nor starts a cooldown the user would wait out for nothing.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not send the verification code. Please try again.",
        ) from exc

    await db.commit()
    return row


async def verify_code(db: AsyncSession, user: User, code: str, *, ip_address: str | None = None) -> User:
    """Redeem the user's latest code. On success the number becomes theirs."""
    await advisory_xact_lock(db, f"phone-verification:user:{user.id}")
    now = datetime.now(timezone.utc)

    row = (
        await db.execute(
            select(PhoneVerificationCode)
            .where(PhoneVerificationCode.user_id == user.id)
            .order_by(PhoneVerificationCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.consumed_at is not None
        or row.expires_at <= now
        or row.attempts >= MAX_ATTEMPTS_PER_CODE
    ):
        await db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CODE_EXPIRED)

    if not hmac.compare_digest(row.code_hash, _hash_code(row.id, code)):
        row.attempts += 1
        await db.commit()
        detail = TOO_MANY_ATTEMPTS if row.attempts >= MAX_ATTEMPTS_PER_CODE else CODE_INCORRECT
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    row.consumed_at = now
    if await _verified_holder(db, row.phone, user.id):
        # Proven elsewhere since the code was sent. The code is spent either way.
        await db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ALREADY_REGISTERED)

    # A proven claim beats a typed one. Registration accepts any number
    # unverified, so without this anyone could park on a stranger's number and
    # lock its owner out of verifying it.
    holders = (
        await db.execute(
            select(User).where(User.phone.in_(equivalent_forms(row.phone)), User.id != user.id)
        )
    ).scalars().all()
    for holder in holders:
        append_audit(
            db,
            entity_type="USER",
            entity_id=holder.id,
            action=PHONE_RELEASED,
            actor_id=None,
            before_state={"phone": holder.phone},
            after_state={"phone": None},
        )
        holder.phone = None
    # Release before claim, or the unique constraint trips on the claim.
    await db.flush()

    before = {"phone": user.phone, "phone_verified": user.phone_verified_at is not None}
    user.phone = row.phone
    user.phone_verified_at = now
    db.add(user)
    append_audit(
        db,
        entity_type="USER",
        entity_id=user.id,
        action=PHONE_VERIFIED,
        actor_id=user.id,
        before_state=before,
        after_state={"phone": row.phone, "phone_verified": True},
        ip_address=ip_address,
    )
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with another account verifying the same number.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ALREADY_REGISTERED)
    return user
