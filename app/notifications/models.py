"""In-app notifications.

WHY A TABLE AND NOT JUST A PUSH
A live socket only reaches someone who is already looking at the screen, and
the events worth notifying about -- an application decision, most of all --
matter precisely when the applicant is not. So the row is the notification;
any realtime transport on top of it is a hint to refetch, never the record.

WHY NO TEXT COLUMN
`event` is a stable code and `data` holds its placeholders. The copy is
rendered by the client, in the reader's own language, from `notifications.*`
in the i18n dictionaries. Storing a rendered English sentence would hand every
Vietnamese-speaking SME an English notification, permanently -- the row
outlives the language the user had selected when it was written.
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.base import Base

# The events this table can carry. Kept as constants rather than an enum
# column: a new event type must not require a migration, but it must exist in
# one place the client can be checked against.
EVENT_APPLICATION_APPROVED = "APPLICATION_APPROVED"
EVENT_APPLICATION_REJECTED = "APPLICATION_REJECTED"
# Written once, when an account picks its role: "verify before you can apply or
# invest". `data["check"]` says which verification -- an SME verifies its
# business (KYB), an investor their identity (KYC) -- and the reminder is
# marked read when that check is approved.
EVENT_VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
# To the business, when an investor uploads their transfer receipt for an
# investment in its round: look for the money under the memo, or report it as
# not received. `data` carries the amount, the memo, the project and the
# investor's masked name.
EVENT_INVESTMENT_RECEIPT_SUBMITTED = "INVESTMENT_RECEIPT_SUBMITTED"
# To the business, when FundLok staff approve an investor's transfer to its
# account. `data["status"]` says how far: ATTESTED (the receipt matched; the
# business has two working days to say the money did not arrive) or CONFIRMED
# (a review settled as received). `data` also carries the amount, the memo,
# the project and the investor's masked name.
EVENT_INVESTMENT_TRANSFER_APPROVED = "INVESTMENT_TRANSFER_APPROVED"
# To the business, when FundLok staff denied the receipt it uploaded for a
# repayment to an investor: those days were due again. `data` carries the
# investment (`tranche_id`, `memo`), the investor's masked name, the amount and
# the note staff gave (`reason`). No longer written (the investor confirms a
# repayment now, staff do not decide it); kept so earlier ones still render.
EVENT_REPAYMENT_RECEIPT_REJECTED = "REPAYMENT_RECEIPT_REJECTED"
# To the investor, when the business uploads the receipt for a repayment to
# them: check the account and confirm it arrived. `data` carries the
# investment (`tranche_id`, `memo`), the receipt (`receipt_id`), the project
# and the amount.
EVENT_REPAYMENT_RECEIPT_SUBMITTED = "REPAYMENT_RECEIPT_SUBMITTED"
# To the business, when the investor confirms a repayment reached them: those
# days are paid. `data` carries the investment (`tranche_id`, `memo`), the
# investor's masked name and the amount.
EVENT_REPAYMENT_RECEIPT_CONFIRMED = "REPAYMENT_RECEIPT_CONFIRMED"

NOTIFICATION_EVENTS = frozenset(
    {
        EVENT_APPLICATION_APPROVED,
        EVENT_APPLICATION_REJECTED,
        EVENT_VERIFICATION_REQUIRED,
        EVENT_INVESTMENT_RECEIPT_SUBMITTED,
        EVENT_INVESTMENT_TRANSFER_APPROVED,
        EVENT_REPAYMENT_RECEIPT_REJECTED,
        EVENT_REPAYMENT_RECEIPT_SUBMITTED,
        EVENT_REPAYMENT_RECEIPT_CONFIRMED,
    }
)

# Values of `data["check"]` on a VERIFICATION_REQUIRED notification.
VERIFICATION_CHECK_KYC = "KYC"
VERIFICATION_CHECK_KYB = "KYB"


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # One of NOTIFICATION_EVENTS. The client maps it to copy, icon and tone.
    event = Column(Text, nullable=False)

    # What the event is about, so the client can link to it. Deliberately not
    # a foreign key: entity_type varies, and a notification about a deleted
    # thing should survive as history rather than vanish.
    entity_type = Column(Text)
    entity_id = Column(UUID(as_uuid=True))

    # Placeholders for the rendered copy (company name, amount, ...). Never
    # a full sentence -- see the module docstring.
    data = Column(JSONB, nullable=False, server_default="{}")

    # NULL = unread. A timestamp rather than a boolean because "when did they
    # see it" is the question that gets asked afterwards.
    read_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Every read is "this user's notifications, newest first".
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )
