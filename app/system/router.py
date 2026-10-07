from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.system import service
from app.system.schemas import (
    MaintenanceState,
    MaintenanceStatus,
    MaintenanceUpdate,
    VerificationModeState,
    VerificationModeStatus,
    VerificationModeUpdate,
)
from app.users.models import Role, User
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(prefix="/system", tags=["system"])

require_system_admin = require_roles(Role.SYSTEM_ADMIN)


@router.get("/maintenance", response_model=MaintenanceStatus)
async def get_maintenance(db: AsyncSession = Depends(get_db)):
    """Public: lets the frontend show a maintenance banner / gate login."""
    return await service.get_maintenance(db)


@router.put("/maintenance", response_model=MaintenanceState)
async def set_maintenance(
    body: MaintenanceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    return await service.set_maintenance(
        db, enabled=body.enabled, message=body.message, actor_id=current_user.id
    )


@router.get("/verification-mode", response_model=VerificationModeStatus)
async def get_verification_mode(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Any signed-in user: lets the KYC/KYB screens say up front that results
    come from a person, not instantly, while MANUAL is in force."""
    return await service.get_verification_mode(db)


@router.get("/verification-mode/admin", response_model=VerificationModeState)
async def get_verification_mode_admin(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    """The same, with who changed it and when (system settings screen)."""
    return await service.get_verification_mode(db)


@router.put("/verification-mode", response_model=VerificationModeState)
async def set_verification_mode(
    body: VerificationModeUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    """SYSTEM_ADMIN only. MANUAL stops every call to the verification
    provider: new KYC/KYB submissions are stored and wait for an admin."""
    return await service.set_verification_mode(
        db,
        mode=body.mode,
        actor_id=current_user.id,
        ip_address=request.client.host if request.client else None,
    )
