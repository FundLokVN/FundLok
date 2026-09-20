from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import service
from app.admin.schemas import (
    AdminDecisionRequest,
    AdminKybVerificationOut,
    AdminLoanApplicationRow,
    AdminMode,
    AdminOverview,
    AdminProjectDetail,
    AuditLogOut,
)
from app.core.database import get_db
from app.users.models import Role, User
from app.utils.rbac import require_roles

router = APIRouter(prefix="/admin", tags=["admin"])

require_admin = require_roles(Role.ADMIN, Role.SYSTEM_ADMIN)


@router.get("/overview", response_model=AdminOverview)
async def get_overview(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    mode: AdminMode = Query(default=AdminMode.users),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=14, ge=1, le=100),
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    role: str | None = Query(default=None, description="users mode only"),
    industry: str | None = Query(default=None, description="projects mode only"),
):
    return await service.get_overview(
        db,
        mode=mode,
        page=page,
        page_size=page_size,
        search=search,
        status=status,
        role=role,
        industry=industry,
    )


@router.get("/projects/{project_id}", response_model=AdminProjectDetail)
async def get_project_detail(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The admin project preview: one company plus every funding request it has
    made. Admin-only, like every route on this router — SMEs read their own
    applications through /loans, never through here."""
    return await service.get_project_detail(db, project_id)


@router.post(
    "/kyb-verifications/{verification_id}/resolve",
    response_model=AdminKybVerificationOut,
)
async def resolve_kyb_verification(
    verification_id: UUID,
    body: AdminDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Settle a KYB attempt the engine parked in MANUAL_REVIEW. Admin-only."""
    return await service.resolve_kyb_verification(
        db,
        verification_id,
        decision=body.decision,
        note=body.note,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.post(
    "/applications/{application_id}/decision",
    response_model=AdminLoanApplicationRow,
)
async def decide_application(
    application_id: UUID,
    body: AdminDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Record the operator half of the two-approval gate. Admin-only.

    An application needs BOTH this and an approved verification before
    create_contract will let it become a facility.
    """
    return await service.decide_application(
        db,
        application_id,
        decision=body.decision,
        note=body.note,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def get_audit_logs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    entity_type: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    actor_id: UUID | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    return await service.list_audit_logs(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
    )
