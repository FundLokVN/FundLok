"""Records of each user's acceptance of the Terms & Services and data consent,
and the one-time codes that confirm it.

ONE ROW PER ACCEPTANCE
A row holds the three tick boxes together, as they stood when the person
confirmed with the code: consents = {"TERMS": true, "DATA_PROCESSING": true,
"DATA_SHARING_TPV": true}. The user holds the consent for a version only when
their latest row for it has all three true; the database itself refuses a row
whose JSON is missing a box, has an extra key, or holds anything but booleans.

user_consents is evidence (Contract 1 Art. 6: electronic records may be used
as evidence), so the app only ever INSERTS rows: an acceptance is never edited,
and a later withdrawal (Art. 10.4) is a new row with that box false, not a
deletion.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Exactly the three boxes (documents.DOCUMENTS), each a boolean. `?&` requires
# all three keys: without it a missing key makes jsonb_typeof() NULL, and a
# CHECK that evaluates to NULL passes. `consents - array[...]` removes the three
# keys, so anything left over is an extra key.
CONSENTS_SHAPE_CHECK = (
    "jsonb_typeof(consents) = 'object'"
    " AND consents ?& ARRAY['TERMS', 'DATA_PROCESSING', 'DATA_SHARING_TPV']"
    " AND jsonb_typeof(consents -> 'TERMS') = 'boolean'"
    " AND jsonb_typeof(consents -> 'DATA_PROCESSING') = 'boolean'"
    " AND jsonb_typeof(consents -> 'DATA_SHARING_TPV') = 'boolean'"
    " AND consents - ARRAY['TERMS', 'DATA_PROCESSING', 'DATA_SHARING_TPV'] = '{}'::jsonb"
)


class UserConsent(Base):
    __tablename__ = "user_consents"
    __table_args__ = (
        CheckConstraint(CONSENTS_SHAPE_CHECK, name="ck_user_consents_consents_shape"),
        Index("ix_user_consents_user_version_created", "user_id", "version", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # documents.CURRENT_VERSION at the time, e.g. "tos-v0.3"
    version = Column(Text, nullable=False)
    # The three tick boxes as confirmed:
    # {"TERMS": true, "DATA_PROCESSING": true, "DATA_SHARING_TPV": true}
    consents = Column(JSONB, nullable=False)
    # How the person confirmed it: "EMAIL_OTP"
    verified_by = Column(Text, nullable=False)
    ip_address = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


class ConsentCode(Base):
    """A 6-digit code emailed to confirm an acceptance. Only its HMAC is kept."""

    __tablename__ = "consent_codes"
    __table_args__ = (Index("ix_consent_codes_user_created", "user_id", "created_at"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Set when the code is used, or retired by a newer code.
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
