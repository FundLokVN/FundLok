"""Admin > Email: each admin's connected Gmail account, and every email sent.

ONE ROW PER ADMIN
Every admin connects their own Google account with their own App Password,
and their email goes out from that address. A row belongs to one user and is
only ever read or changed by that user (the service always filters on the
signed-in admin). It is deleted with the user.

WHY A TABLE, NOT system_settings
The settings hold a credential. A dedicated table gives the encrypted
password its own typed column, which is never selected into a dict that
could be serialised.

WHY THE LOG KEEPS THE CONTENT
internal_emails is the record of what each admin sent under FundLok's name,
from which address, and to whom. A row is written PENDING and committed
before Gmail is contacted, so even a crash mid-send leaves a trace.
"""
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from app.core.base import Base

STATUS_PENDING = "PENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"


class EmailSettings(Base):
    __tablename__ = "email_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The admin who owns this account. One row each.
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # The admin's Google account: the SMTP login and the From address.
    gmail_address = Column(Text, nullable=False)
    from_name = Column(Text, nullable=False)
    # Fernet "v1:<token>" (crypto.py). Never returned by the API.
    app_password_encrypted = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    # Set by a successful test email.
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    # The admin's email signature, appended to a message when they tick
    # "Include my email signature" in the composer. Optional: nothing is
    # appended when name is blank.
    signature_name = Column(Text, nullable=True)
    signature_title = Column(Text, nullable=True)
    signature_phone = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class InternalEmail(Base):
    __tablename__ = "internal_emails"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED')", name="ck_internal_emails_status"
        ),
        # The hourly limit counts one admin's recent rows.
        Index("ix_internal_emails_sent_by_created_at", "sent_by", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sent_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    from_address = Column(Text, nullable=False)
    recipients = Column(ARRAY(Text), nullable=False)
    # general | announcement | action_required | test
    template = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    # heading, body, button_label, button_url: what the admin wrote.
    content = Column(JSONB, nullable=False)
    status = Column(Text, nullable=False, default=STATUS_PENDING)
    # One of the X-Error-Code values in service.py when FAILED, or
    # SOME_RECIPIENTS_REFUSED when SENT to some but not all.
    error_code = Column(Text, nullable=True)
    # Each recipient gets their own copy; these are the ones that did not go
    # out (refused by Gmail, or left when the connection dropped).
    undelivered = Column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'")
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
