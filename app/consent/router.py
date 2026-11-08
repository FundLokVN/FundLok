from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent import service
from app.consent.schemas import ConsentAcceptRequest, ConsentCodeOut, ConsentStatusOut
from app.core.database import get_db
from app.users.models import User
from app.utils.jwt import get_current_user

# Spec: docs/specs/consent/sign-up-terms-consent.md
router = APIRouter(prefix="/consent", tags=["consent"])


@router.get("/status", response_model=ConsentStatusOut)
async def consent_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Whether the signed-in user still has to accept the current Terms."""
    return await service.get_status(db, current_user)


@router.post("/code", response_model=ConsentCodeOut)
async def send_consent_code(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Email a 6-digit code to the account's own address, to confirm the
    acceptance. Never to an address from the request."""
    return await service.send_code(db, current_user, background_tasks)


@router.post("/accept", response_model=ConsentStatusOut)
async def accept_consent(
    body: ConsentAcceptRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept the current Terms version: all three boxes and the code."""
    return await service.accept(
        db,
        current_user,
        body,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
