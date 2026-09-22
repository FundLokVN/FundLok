"""In-app notifications for the signed-in user.

Every route is scoped to `current_user`: there is no way to read or change
another account's notifications, and no admin view here -- an operator who
needs to know what a user was told reads the audit log, which is the record
that is meant to be inspected.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.notifications import service
from app.notifications.schemas import NotificationListOut, NotificationOut
from app.users.models import User
from app.utils.jwt import get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListOut)
async def list_my_notifications(
    limit: int = Query(service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items, unread = await service.list_notifications(
        db, user_id=current_user.id, limit=limit
    )
    return NotificationListOut(
        items=[NotificationOut.model_validate(row) for row in items], unread=unread
    )


@router.patch("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = await service.mark_read(
        db, notification_id=notification_id, user_id=current_user.id
    )
    return NotificationOut.model_validate(row)


@router.post("/read-all")
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    updated = await service.mark_all_read(db, user_id=current_user.id)
    return {"updated": updated}
