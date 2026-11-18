"""The Personal Guarantor signs by a Zalo one-time code (Money flow FINAL v1.0).

Spec: docs/specs/loans/guarantor-zalo-otp.md

Money flow FINAL v1.0, section 1: "The guarantor (the owner, or the largest capital holder) signs the standing offer
by OTP sent by Zalo; the SME signs with its digital signature" (Day 0), and on Day 13 "the guarantor signs each [loan
contract] by OTP sent by Zalo (its signature is not a condition of acceptance)". So the guarantor needs no personal
digital signature: a code sent by Zalo, through the same approved OTP template as phone verification and the
investor's agreement (zalo.send_otp), to the phone on the guarantor's FundLok account is their signature.

The guarantor today is the signing SME user, as their approved eKYC names them (standing_offer.guarantor_for), so the
code goes to that account's phone. Ops rule "Guarantor ready before Day 0": signing opens only once that account has
an approved eKYC (TPV Datatrust) and a verified phone.

What makes the code a signature: it goes only to the phone on the guarantor's account, it is bound to the step and
its subject (a code for one listing proves nothing about another), it is single use, lives five minutes and dies
after five wrong guesses; the documents it signed are kept by hash with the code's record, the IP and the browser.

Every function takes `now` and never commits: the caller does.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import logging
import math
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.loans.guarantor_models import PURPOSES, GuarantorSignature, GuarantorSigningCode
from app.phone_verification import service as phone_codes
from app.phone_verification import zalo
from app.phone_verification.phone import canonical_vn_mobile, zalo_number
from app.users.models import User
from app.utils.audit import append_audit
from app.utils.locks import advisory_xact_lock

logger = logging.getLogger(__name__)

CODE_TTL = dt.timedelta(minutes=5)
RESEND_COOLDOWN = dt.timedelta(seconds=60)
MAX_CODES_PER_DAY = 5
MAX_ATTEMPTS_PER_CODE = 5
SIGNED_BY = "ZALO_OTP"


class GuarantorCodeError(HTTPException):
    """A refusal with its X-Error-Code. `persist`: the wrong guess must be kept even though the request fails, so
    the router commits before raising."""

    def __init__(self, status_code: int, code: str, detail: str, *, retry_after: dt.timedelta | None = None,
                 persist: bool = False):
        headers = {"X-Error-Code": code}
        if retry_after is not None:
            headers["Retry-After"] = str(max(1, math.ceil(retry_after.total_seconds())))
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.persist = persist


def masked_phone(canonical: str) -> str:
    """+84703564489 -> 0703***489."""
    national = "0" + canonical[3:]
    return f"{national[:4]}***{national[-3:]}"


def guarantor_phone(user: User) -> str | None:
    """The number the code goes to: the account's phone, once verified, when it is a Vietnamese mobile."""
    if user.phone is None or user.phone_verified_at is None:
        return None
    return canonical_vn_mobile(user.phone)


def _hash_code(row: GuarantorSigningCode, code: str) -> str:
    message = f"guarantor-signature:{row.id}:{row.purpose}:{row.subject_id}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


async def signature_for(db: AsyncSession, purpose: str, subject_id: UUID) -> GuarantorSignature | None:
    return (
        await db.execute(
            select(GuarantorSignature).where(
                GuarantorSignature.purpose == purpose, GuarantorSignature.subject_id == subject_id
            )
        )
    ).scalar_one_or_none()


async def send_code(
    db: AsyncSession, *, user: User, purpose: str, subject_id: UUID, now: dt.datetime
) -> GuarantorSigningCode:
    """Send the guarantor a fresh code by Zalo for `purpose` on `subject_id` (the caller checks the step is open
    and the guarantor is this user). A new code retires the last.

    409 PHONE_REQUIRED without a verified Vietnamese mobile; 429 CODE_COOLDOWN (one a minute) or CODE_DAILY_LIMIT
    (five a day); 400 PHONE_NO_ZALO; 502 CODE_NOT_SENT; 503 CODE_UNAVAILABLE when Zalo is not configured."""
    assert purpose in PURPOSES
    phone = guarantor_phone(user)
    if phone is None:
        raise GuarantorCodeError(
            409, "PHONE_REQUIRED",
            "Verify a Vietnamese mobile number on your account: the guarantor's code is sent to it by Zalo",
        )
    await advisory_xact_lock(db, f"guarantor-code:{user.id}")
    sends, first, last = (
        await db.execute(
            select(
                func.count(GuarantorSigningCode.id),
                func.min(GuarantorSigningCode.created_at),
                func.max(GuarantorSigningCode.created_at),
            ).where(GuarantorSigningCode.user_id == user.id, GuarantorSigningCode.created_at >= now - dt.timedelta(days=1))
        )
    ).one()
    if last is not None and now - last < RESEND_COOLDOWN:
        raise GuarantorCodeError(
            429, "CODE_COOLDOWN", "Please wait before requesting another code", retry_after=RESEND_COOLDOWN - (now - last)
        )
    if sends >= MAX_CODES_PER_DAY:
        raise GuarantorCodeError(
            429, "CODE_DAILY_LIMIT", "Too many codes requested today. Try again later.",
            retry_after=first + dt.timedelta(days=1) - now,
        )
    code = phone_codes._new_code()
    try:
        channel = phone_codes._channel()  # zalo, or log (DEBUG only)
        row = GuarantorSigningCode(
            id=uuid4(), user_id=user.id, purpose=purpose, subject_id=subject_id, phone=phone, channel=channel,
            expires_at=now + CODE_TTL, created_at=now,
        )
        row.code_hash = _hash_code(row, code)
        db.add(row)
        await db.flush()  # before the send: a database error should not cost a Zalo message
        if channel == "zalo":
            await zalo.send_otp(zalo_number(phone), code, tracking_id=row.id.hex)
        else:
            logger.warning("Guarantor code for %s (%s %s): %s (PHONE_OTP_CHANNEL=log)", phone, purpose, subject_id, code)
    except zalo.ZaloNotConfigured as exc:
        await db.rollback()
        logger.error("Guarantor code unavailable: %s", exc)
        raise GuarantorCodeError(503, "CODE_UNAVAILABLE", "The code is not available right now") from exc
    except zalo.ZaloError as exc:
        await db.rollback()  # an undelivered code neither counts nor starts a cooldown
        if exc.code in phone_codes.NO_ZALO_ERRORS:
            raise GuarantorCodeError(
                400, "PHONE_NO_ZALO", "Your phone number has no Zalo account that can receive the code"
            ) from exc
        raise GuarantorCodeError(502, "CODE_NOT_SENT", "Could not send the code. Please try again.") from exc
    return row


async def redeem_code(
    db: AsyncSession, *, user: User, purpose: str, subject_id: UUID, code: str, now: dt.datetime
) -> GuarantorSigningCode:
    """Use the guarantor's latest code for `purpose` on `subject_id`. 400 CODE_EXPIRED when there is no live code
    for it (none, used, expired, exhausted, or sent for something else); 400 CODE_INCORRECT, or
    CODE_TOO_MANY_ATTEMPTS on the fifth wrong guess (both `persist`)."""
    await advisory_xact_lock(db, f"guarantor-code:{user.id}")
    row = (
        await db.execute(
            select(GuarantorSigningCode)
            .where(GuarantorSigningCode.user_id == user.id)
            .order_by(GuarantorSigningCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.consumed_at is not None
        or row.expires_at <= now
        or row.attempts >= MAX_ATTEMPTS_PER_CODE
        or row.purpose != purpose
        or row.subject_id != subject_id
    ):
        raise GuarantorCodeError(400, "CODE_EXPIRED", "This code has expired. Request a new one.")
    if not hmac.compare_digest(row.code_hash, _hash_code(row, (code or "").strip())):
        row.attempts += 1
        await db.flush()
        exhausted = row.attempts >= MAX_ATTEMPTS_PER_CODE
        raise GuarantorCodeError(
            400,
            "CODE_TOO_MANY_ATTEMPTS" if exhausted else "CODE_INCORRECT",
            "Too many incorrect attempts. Request a new code." if exhausted else "Incorrect code",
            persist=True,
        )
    row.consumed_at = now
    await db.flush()
    return row


@dataclasses.dataclass(frozen=True)
class SignedDocument:
    document_type: str
    sha256: str | None = None
    check_code: str | None = None
    reference: str | None = None  # e.g. the contract number

    def as_dict(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


async def record_signature(
    db: AsyncSession,
    *,
    user: User,
    purpose: str,
    subject_id: UUID,
    code_row: GuarantorSigningCode,
    signer_name: str | None,
    signer_id_number: str,
    documents: list[SignedDocument],
    now: dt.datetime,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> GuarantorSignature:
    """Keep what the code signed, with an audit row. One per step and subject (the caller checks it is not yet
    signed)."""
    signature = GuarantorSignature(
        purpose=purpose,
        subject_id=subject_id,
        signer_user_id=user.id,
        signer_name=signer_name,
        signer_id_number=signer_id_number,
        phone=code_row.phone,
        code_id=code_row.id,
        documents=[d.as_dict() for d in documents],
        signed_at=now,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:500] or None,
    )
    db.add(signature)
    await db.flush()
    append_audit(
        db,
        entity_type="GUARANTOR_SIGNATURE",
        entity_id=signature.id,
        action="GUARANTOR_SIGNED_BY_ZALO_OTP",
        actor_id=user.id,
        after_state={
            "purpose": purpose,
            "subject_id": str(subject_id),
            "signed_by": SIGNED_BY,
            "phone_masked": masked_phone(code_row.phone),
            "code_id": str(code_row.id),
            "code_sent_at": code_row.created_at.isoformat(),
            "signer_id_number": signer_id_number,
            "documents": signature.documents,
        },
        ip_address=ip_address,
    )
    return signature
