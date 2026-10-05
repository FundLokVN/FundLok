from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class NotificationOut(BaseModel):
    """One notification, as the bell renders it.

    No message text: `event` and `data` are what the client turns into copy in
    the reader's own language. See the model docstring.
    """

    id: UUID
    event: str
    entity_type: str | None
    entity_id: UUID | None
    data: dict
    read_at: datetime | None
    created_at: datetime | None

    model_config = {"from_attributes": True}


class NotificationListOut(BaseModel):
    """The list plus the unread count.

    The count is computed over ALL of the user's notifications, not just the
    page returned -- the badge on the bell would otherwise stop counting at
    whatever the page size happens to be.
    """

    items: list[NotificationOut]
    unread: int
