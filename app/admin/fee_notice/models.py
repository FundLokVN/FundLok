"""One row per fee invoice FundLok has emailed (or tried to email) to the payer.

The row is written as SENDING and committed before any email leaves, so two
sweeps running at once cannot both send the same invoice: only the one whose
insert (or retry update) went through sends. A FAILED row is retried up to
service.MAX_ATTEMPTS times; a row left SENDING by a crash mid-send is never
retried, since the email may have gone.
"""
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.base import Base

STATUS_SENDING = "SENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"

# GMAIL: the admin account chosen under System settings (reminder sender).
# PLATFORM: the SMTP relay the platform's own emails use.
CHANNEL_GMAIL = "GMAIL"
CHANNEL_PLATFORM = "PLATFORM"


class FeeInvoiceNotice(Base):
    __tablename__ = "fee_invoice_notices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SENDING', 'SENT', 'FAILED')", name="ck_fee_invoice_notices_status"
        ),
        CheckConstraint(
            "channel IS NULL OR channel IN ('GMAIL', 'PLATFORM')",
            name="ck_fee_invoice_notices_channel",
        ),
    )

    invoice_id = Column(
        UUID(as_uuid=True),
        ForeignKey("fee_invoices.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status = Column(Text, nullable=False)
    # Set once sent: which way it went, and from which address.
    channel = Column(Text, nullable=True)
    from_address = Column(Text, nullable=True)
    sent_to = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, server_default="1")
    # A short code (an X-Error-Code, or the exception's class), never a
    # message that could carry the recipient or a token.
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    sent_at = Column(DateTime(timezone=True), nullable=True)
