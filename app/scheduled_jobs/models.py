"""One row per SME per day a repayment reminder was emailed (or tried).

Written SENDING and committed before the email leaves, so a retried or
doubled scheduler call cannot send twice: only the run whose insert (or
retry update) went through sends. A FAILED row is retried by a later run
the same day, up to reminders.MAX_ATTEMPTS; a row left SENDING by a crash
mid-send is not, since the email may have gone.
"""
from sqlalchemy import BigInteger, CheckConstraint, Column, Date, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.base import Base


class RepaymentReminder(Base):
    __tablename__ = "repayment_reminders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SENDING', 'SENT', 'FAILED')", name="ck_repayment_reminders_status"
        ),
        CheckConstraint(
            "channel IS NULL OR channel IN ('GMAIL', 'PLATFORM')",
            name="ck_repayment_reminders_channel",
        ),
    )

    sme_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # The ICT working day the reminder is for.
    reminder_date = Column(Date, primary_key=True)
    status = Column(Text, nullable=False)
    channel = Column(Text, nullable=True)
    from_address = Column(Text, nullable=True)
    sent_to = Column(Text, nullable=False)
    # What it said, for the record: payments and amount due that day, and
    # earlier days not yet marked paid.
    due_count = Column(Integer, nullable=False, server_default="0")
    due_vnd = Column(BigInteger, nullable=False, server_default="0")
    earlier_count = Column(Integer, nullable=False, server_default="0")
    attempts = Column(Integer, nullable=False, server_default="1")
    # A short code, never a message that could carry an address or token.
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    sent_at = Column(DateTime(timezone=True), nullable=True)
