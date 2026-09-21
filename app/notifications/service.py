"""Reading and writing in-app notifications."""
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications.models import NOTIFICATION_EVENTS, Notification

# The bell shows a short list, not an archive. Anything older is history the
# user reads elsewhere; loading it all would make the hover card slower the
# longer someone has been a customer.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def create_notification(
    db: AsyncSession,
    *,
    user_id: UUID,
    event: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[UUID] = None,
    data: Optional[dict[str, Any]] = None,
) -> Notification:
    """Stage a notification on the caller's transaction.

    Deliberately NOT async and it does not commit: the notification belongs to
    the same transaction as the thing it announces. A decision that rolls back
    must not leave an applicant told they were rejected, and a notification
    written in its own transaction could do exactly that.
    """
    if event not in NOTIFICATION_EVENTS:
        # A typo'd event code produces a notification the client cannot render
        # and nobody can search for. Better to fail where it was written.
        raise ValueError(f"unknown notification event {event!r}")

    row = Notification(
        user_id=user_id,
        event=event,
        entity_type=entity_type,
        entity_id=entity_id,
        data=data or {},
    )
    db.add(row)
    return row


async def list_notifications(
    db: AsyncSession, *, user_id: UUID, limit: int = DEFAULT_LIMIT
) -> tuple[list[Notification], int]:
    """This user's notifications, newest first, plus the total unread count."""
    limit = max(1, min(limit, MAX_LIMIT))

    rows = (
        (
            await db.execute(
                select(Notification)
                .where(Notification.user_id == user_id)
                .order_by(Notification.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # Counted over everything, not over `rows`: the badge is a count of what
    # is unread, not of what fitted on the page.
    unread = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
    ).scalar_one()

    return list(rows), int(unread)


async def mark_read(
    db: AsyncSession, *, notification_id: UUID, user_id: UUID
) -> Notification:
    """Mark one notification read. Scoped to the owner, and idempotent."""
    row = await db.get(Notification, notification_id)
    # 404 rather than 403 on someone else's row: a notification the caller does
    # not own should not be distinguishable from one that does not exist.
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Notification not found")

    # Re-reading an already-read notification keeps the first timestamp. When
    # they saw it is the question worth answering; when they clicked again is
    # not.
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def mark_all_read(db: AsyncSession, *, user_id: UUID) -> int:
    """Mark every unread notification read. Returns how many were changed."""
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return int(result.rowcount or 0)
