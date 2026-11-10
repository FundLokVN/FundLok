import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.base import Base


class PhoneVerificationCode(Base):
    """One code sent to one number. Spec: docs/specs/users/phone-verification.md

    Only a user's most recent row can be redeemed; sending a new code retires
    the older ones without touching them. Rows are kept after use because they
    are also the send counter: the cooldown and the daily limits (per account
    and per number) count them.
    """

    __tablename__ = "phone_verification_codes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Canonical +84xxxxxxxxx, the form the user's phone is stored in once proven.
    phone = Column(Text, nullable=False)
    # HMAC-SHA256 of the code, keyed by SECRET_KEY and bound to this row's id.
    # Never the code itself: a database read must not hand out live codes.
    code_hash = Column(Text, nullable=False)
    channel = Column(Text, nullable=False)  # zalo | log
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Wrong guesses against this code; at the limit the code is dead.
    attempts = Column(Integer, nullable=False, server_default="0")
    consumed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_phone_verification_codes_user_created", "user_id", "created_at"),
        Index("ix_phone_verification_codes_phone_created", "phone", "created_at"),
    )


class ZaloOaToken(Base):
    """The Zalo OA token pair for one Zalo app, shared by every instance.

    Zalo issues a new refresh token on every refresh and retires the old one,
    so whichever instance refreshes must leave the new pair where the others
    will find it. Both tokens are Fernet-encrypted (app/phone_verification/
    zalo.py); `seeded_from` fingerprints the ZALO_OA_REFRESH_TOKEN that began
    this chain, so putting a new one in the environment starts over.
    """

    __tablename__ = "zalo_oa_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id = Column(Text, nullable=False, unique=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    access_expires_at = Column(DateTime(timezone=True), nullable=False)
    seeded_from = Column(Text, nullable=False)  # sha256 hex, never the token
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
