"""Sign-up acceptance of the Terms & Services and data consent (Contract 1).

Every SME and investor accepts the current Terms version with three separate
tick boxes and a one-time code (Contract 1 Art. 2.3) before they use the
platform, and before any identity data reaches TPV Datatrust (Art. 10.3).
Each acceptance is one append-only row holding the three boxes together in
JSONB, with its version (Art. 6); consent counts only when all three are true.

The code goes to the account's email address until phone OTP ships. It is
6 digits from `secrets`, valid 10 minutes, single use, 5 wrong tries, and only
an HMAC of it is stored. Sends are limited per account, counted under an
advisory lock like the phone-verification and resend-verification limits, so
parallel calls cannot slip past.

Spec: docs/specs/consent/sign-up-terms-consent.md
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent import documents
from app.consent.models import ConsentCode, UserConsent
from app.consent.schemas import ConsentAcceptRequest, ConsentCodeOut, ConsentStatusOut
from app.core.config import settings
from app.users.models import Role, User
from app.utils import email as email_utils
from app.utils.audit import append_audit
from app.utils.email_templates import render_email
from app.utils.locks import advisory_xact_lock

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_CODES_PER_DAY = 10
MAX_ATTEMPTS_PER_CODE = 5

# FundLok staff, not platform users: never asked to accept the user Terms.
_EXEMPT_ROLES = {Role.ADMIN.value, Role.SYSTEM_ADMIN.value}


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


def is_exempt(user: User) -> bool:
    return user.role in _EXEMPT_ROLES


# --------------------------------------------------------------------------- #
# Status and the gate
# --------------------------------------------------------------------------- #


def all_accepted(consents: object) -> bool:
    """True only if every one of the three boxes is literally true. Anything
    else (a box missing, false, "true" as a string, not an object) is not
    consent."""
    return isinstance(consents, dict) and all(
        consents.get(doc) is True for doc in documents.DOCUMENTS
    )


async def accepted_at(db: AsyncSession, user_id) -> datetime | None:
    """When the user accepted the current version, or None. Read from their
    latest row for that version, so a later row with a box false (a
    withdrawal) ends the consent."""
    latest = (
        await db.execute(
            select(UserConsent.consents, UserConsent.created_at)
            .where(
                UserConsent.user_id == user_id,
                UserConsent.version == documents.CURRENT_VERSION,
            )
            .order_by(UserConsent.created_at.desc())
            .limit(1)
        )
    ).first()
    if latest is None or not all_accepted(latest.consents):
        return None
    return latest.created_at


# What an admin sees for a user (Admin > Users): the current Terms accepted,
# still owed, or not asked at all (FundLok staff).
STATE_ACCEPTED = "ACCEPTED"
STATE_REQUIRED = "REQUIRED"
STATE_NOT_APPLICABLE = "NOT_APPLICABLE"


async def states_for(db: AsyncSession, users) -> dict:
    """{user_id: (state, accepted_at)} for a page of users, in one query:
    each user's latest row for the current version (DISTINCT ON), judged the
    same way as the gate (all three boxes literally true)."""
    ids = [u.id for u in users if not is_exempt(u)]
    latest: dict = {}
    if ids:
        rows = (
            await db.execute(
                select(UserConsent.user_id, UserConsent.consents, UserConsent.created_at)
                .where(
                    UserConsent.user_id.in_(ids),
                    UserConsent.version == documents.CURRENT_VERSION,
                )
                .distinct(UserConsent.user_id)
                .order_by(UserConsent.user_id, UserConsent.created_at.desc())
            )
        ).all()
        latest = {row.user_id: row for row in rows}
    out = {}
    for user in users:
        if is_exempt(user):
            out[user.id] = (STATE_NOT_APPLICABLE, None)
            continue
        row = latest.get(user.id)
        if row is not None and all_accepted(row.consents):
            out[user.id] = (STATE_ACCEPTED, row.created_at)
        else:
            out[user.id] = (STATE_REQUIRED, None)
    return out


async def history(db: AsyncSession, user_id) -> list[UserConsent]:
    """Every consent row the user has, newest first: the evidence trail."""
    return list(
        (
            await db.execute(
                select(UserConsent)
                .where(UserConsent.user_id == user_id)
                .order_by(UserConsent.created_at.desc())
            )
        ).scalars()
    )


async def get_status(db: AsyncSession, user: User) -> ConsentStatusOut:
    when = await accepted_at(db, user.id)
    return ConsentStatusOut(
        version=documents.CURRENT_VERSION,
        required=when is None and not is_exempt(user),
        accepted_at=when,
    )


async def is_required(db: AsyncSession, user: User) -> bool:
    return not is_exempt(user) and await accepted_at(db, user.id) is None


async def require_consent(db: AsyncSession, user: User) -> None:
    """403 CONSENT_REQUIRED unless the user holds the current consent. Called
    before identity data goes to TPV Datatrust (eKYC) and before a role is
    chosen."""
    if await is_required(db, user):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "CONSENT_REQUIRED",
            "Accept the Terms & Services and data consent first.",
        )


# --------------------------------------------------------------------------- #
# One-time code
# --------------------------------------------------------------------------- #


def _hash(user_id, code: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), f"consent:{user_id}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


def _deliver_code(to_email: str, code: str) -> None:
    """Email the code. A seam: tests replace it to read the code. Runs as a
    background task, after the response, like every other email."""
    minutes = int(CODE_TTL.total_seconds() // 60)
    html = render_email("consent-code", code=code, minutes=str(minutes))
    text = (
        f"Your FundLok confirmation code is {code}. It expires in {minutes} minutes.\n\n"
        "Enter it to accept the Terms & Services and data consent. "
        "If you did not ask for it, ignore this email."
    )
    # Through the module, not a copied name, so the test suite's patch of
    # app.utils.email.send_email covers it.
    email_utils.send_email(
        to_email=to_email,
        subject="Your FundLok confirmation code",
        html_content=html,
        text_content=text,
    )


async def send_code(
    db: AsyncSession, user: User, background_tasks: BackgroundTasks
) -> ConsentCodeOut:
    await advisory_xact_lock(db, f"consent-code:{user.id}")
    now = datetime.now(timezone.utc)

    last_sent = await db.scalar(
        select(func.max(ConsentCode.created_at)).where(ConsentCode.user_id == user.id)
    )
    if last_sent is not None and now - last_sent < RESEND_COOLDOWN:
        wait = int((RESEND_COOLDOWN - (now - last_sent)).total_seconds()) + 1
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "CONSENT_CODE_RATE_LIMITED",
            f"Wait {wait} seconds before asking for another code.",
        )
    sent_today = await db.scalar(
        select(func.count())
        .select_from(ConsentCode)
        .where(ConsentCode.user_id == user.id, ConsentCode.created_at > now - timedelta(days=1))
    )
    if sent_today >= MAX_CODES_PER_DAY:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "CONSENT_CODE_RATE_LIMITED",
            "Too many codes today. Try again tomorrow.",
        )

    # Only the newest code works.
    await db.execute(
        update(ConsentCode)
        .where(ConsentCode.user_id == user.id, ConsentCode.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    code = f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"
    db.add(
        ConsentCode(
            user_id=user.id,
            code_hash=_hash(user.id, code),
            expires_at=now + CODE_TTL,
            created_at=now,
        )
    )
    await db.commit()
    background_tasks.add_task(_deliver_code, user.email, code)
    return ConsentCodeOut(
        sent_to=mask_email(user.email),
        expires_in_seconds=int(CODE_TTL.total_seconds()),
        resend_after_seconds=int(RESEND_COOLDOWN.total_seconds()),
    )


# --------------------------------------------------------------------------- #
# Accepting
# --------------------------------------------------------------------------- #


async def accept(
    db: AsyncSession,
    user: User,
    body: ConsentAcceptRequest,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> ConsentStatusOut:
    """Check the code and record one row holding all three boxes, each
    required true (by the schema, and again before the insert)."""
    if body.version != documents.CURRENT_VERSION:
        raise _error(
            status.HTTP_409_CONFLICT,
            "CONSENT_VERSION_CHANGED",
            "The Terms have changed. Reload the page and read the current version.",
        )

    await advisory_xact_lock(db, f"consent-code:{user.id}")
    now = datetime.now(timezone.utc)
    code_row = (
        await db.execute(
            select(ConsentCode)
            .where(ConsentCode.user_id == user.id, ConsentCode.consumed_at.is_(None))
            .order_by(ConsentCode.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if code_row is None or code_row.expires_at <= now or code_row.attempts >= MAX_ATTEMPTS_PER_CODE:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "CONSENT_CODE_EXPIRED",
            "That code has expired. Ask for a new one.",
        )
    if not hmac.compare_digest(code_row.code_hash, _hash(user.id, body.code)):
        code_row.attempts += 1
        await db.commit()
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "CONSENT_CODE_INVALID",
            "That code is not right. Check the email and try again.",
        )

    # The three boxes exactly as sent. The schema already refuses a false or
    # missing one; checked again here so no path can record partial consent.
    consents = {
        documents.TERMS: body.terms,
        documents.DATA_PROCESSING: body.data_processing,
        documents.DATA_SHARING_TPV: body.data_sharing_tpv,
    }
    if not all_accepted(consents):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="All three boxes must be ticked.",
        )

    code_row.consumed_at = now
    db.add(
        UserConsent(
            user_id=user.id,
            version=documents.CURRENT_VERSION,
            consents=consents,
            verified_by=documents.VERIFIED_BY_EMAIL_OTP,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512] or None,
            created_at=now,
        )
    )
    append_audit(
        db,
        entity_type="USER",
        entity_id=user.id,
        action="CONSENT_ACCEPTED",
        actor_id=user.id,
        after_state={
            "version": documents.CURRENT_VERSION,
            "consents": consents,
            "verified_by": documents.VERIFIED_BY_EMAIL_OTP,
        },
        ip_address=ip_address,
    )
    await db.commit()
    return await get_status(db, user)
