"""Records of the daily repayment reminder, so it is never sent twice.

RepaymentReminder: one row per SME per day the reminder was emailed (or tried).
RepaymentZaloReminder: one row per investor ticket per day the SME was sent
the Zalo reminder (or tried).

Each is written SENDING and committed before the message leaves, so a retried
or doubled scheduler call cannot send twice: only the run whose insert (or
retry update) went through sends. A FAILED row is retried by a later run
the same day, up to MAX_ATTEMPTS; a row left SENDING by a crash mid-send is
not, since the message may have gone.
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


class RepaymentZaloReminder(Base):
    """One row per investor ticket per day the SME's Zalo reminder was sent (or
    tried). Each message costs money, so it is claimed the same way as the
    email: written SENDING and committed before the send, so a retried or
    doubled scheduler call never sends it twice. FAILED is retried by a later
    run the same day, up to zalo_reminders.MAX_ATTEMPTS."""

    __tablename__ = "repayment_zalo_reminders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SENDING', 'SENT', 'FAILED')", name="ck_repayment_zalo_reminders_status"
        ),
    )

    tranche_id = Column(
        UUID(as_uuid=True), ForeignKey("tranches.id", ondelete="CASCADE"), primary_key=True
    )
    # The ICT working day the reminder is for.
    reminder_date = Column(Date, primary_key=True)
    sme_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status = Column(Text, nullable=False)
    # The 84xxxxxxxxx number it went to.
    sent_to = Column(Text, nullable=False)
    # What it said: the amount due that day (net) and the repayment days left.
    amount_vnd = Column(BigInteger, nullable=False)
    days_left = Column(Integer, nullable=False)
    attempts = Column(Integer, nullable=False, server_default="1")
    # Zalo's error code or a short name, never a message that could carry a token.
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    sent_at = Column(DateTime(timezone=True), nullable=True)
