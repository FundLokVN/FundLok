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

NOTIFICATION_EVENTS = frozenset(
    {EVENT_APPLICATION_APPROVED, EVENT_APPLICATION_REJECTED}
)


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
