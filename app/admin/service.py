from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.sql import Select

# Only HTTPException: `status` is already a filter-parameter name all through
# this module, so importing fastapi.status would shadow it inside every list
# function. The one status code used below is written out instead.
from fastapi import HTTPException

from app.admin.schemas import (
    ActorOut,
    AdminDecision,
    AdminKybVerificationOut,
    AdminLoanApplicationRow,
    AdminMode,
    AdminOverview,
    AdminProjectDetail,
    AdminStats,
    AdminUserDetail,
    AuditLogOut,
    Page,
    ProjectRow,
    UserRow,
)
from app.gverify.models import STATUS_MANUAL_REVIEW, GVerifyKybVerification
from app.lending.models import AuditLog, LoanApplication, Project, ProjectOwnership
from app.users.models import Role, User
from app.utils.audit import append_audit


async def _paginate(db: AsyncSession, stmt: Select, *, page: int, page_size: int, row_model, order_col) -> Page:
    """Apply offset pagination to a select() statement and wrap rows in a Page envelope."""
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await db.execute(count_stmt)).scalar_one() or 0

    rows_stmt = stmt.order_by(order_col.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(rows_stmt)).scalars().all()

    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return Page[row_model](
        items=[row_model.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


async def get_stats(db: AsyncSession) -> AdminStats:
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    total_projects = (await db.execute(select(func.count(Project.id)))).scalar() or 0

    users_by_role = dict(
        (await db.execute(select(User.role, func.count(User.id)).group_by(User.role))).all()
    )
    users_by_status = dict(
        (await db.execute(select(User.status, func.count(User.id)).group_by(User.status))).all()
    )
    projects_by_status = dict(
        (await db.execute(select(Project.status, func.count(Project.id)).group_by(Project.status))).all()
    )

    return AdminStats(
        total_users=total_users,
        total_projects=total_projects,
        users_by_role=users_by_role,
        users_by_status=users_by_status,
        projects_by_status=projects_by_status,
    )


async def list_users(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 14,
    search: str | None = None,
    status: str | None = None,
    role: str | None = None,
) -> Page:
    stmt = select(User)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(User.email.ilike(like), User.full_name.ilike(like)))
    if status:
        stmt = stmt.where(User.status == status)
    if role:
        stmt = stmt.where(User.role == role)
    return await _paginate(db, stmt, page=page, page_size=page_size, row_model=UserRow, order_col=User.created_at)


async def list_projects(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 14,
    search: str | None = None,
    status: str | None = None,
    industry: str | None = None,
) -> Page:
    stmt = select(Project)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Project.legal_name.ilike(like), Project.industry.ilike(like)))
    if status:
        stmt = stmt.where(Project.status == status)
    if industry:
        stmt = stmt.where(Project.industry.ilike(industry))
    return await _paginate(db, stmt, page=page, page_size=page_size, row_model=ProjectRow, order_col=Project.created_at)


async def get_overview(
    db: AsyncSession,
    *,
    mode: AdminMode = AdminMode.users,
    page: int = 1,
    page_size: int = 14,
    search: str | None = None,
    status: str | None = None,
    role: str | None = None,
    industry: str | None = None,
) -> AdminOverview:
    """One-shot BFF payload: stats + whichever table the frontend asked for."""
    stats = await get_stats(db)
    if mode == AdminMode.projects:
        table = await list_projects(
            db, page=page, page_size=page_size, search=search, status=status, industry=industry
        )
    else:
        table = await list_users(
            db, page=page, page_size=page_size, search=search, status=status, role=role
        )
    return AdminOverview(stats=stats, mode=mode, table=table)


async def get_project_detail(db: AsyncSession, project_id: UUID) -> AdminProjectDetail:
    """One company and every funding request it has made.

    Not paginated: applications are per-company and a handful at most, so an
    envelope would cost the caller a second round trip to learn there was
    nothing on page two. Ordered newest first — an admin opening this is
    looking at the latest request, not the first one ever filed.
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    rows = (
        await db.execute(
            select(LoanApplication)
            .where(LoanApplication.project_id == project_id)
            # Eager-loaded: the panel renders every application's uploads, so
            # lazy loading here would be one query per funding request.
            .options(selectinload(LoanApplication.documents))
            .order_by(LoanApplication.created_at.desc())
        )
    ).scalars().all()

    return AdminProjectDetail(
        id=project.id,
        legal_name=project.legal_name,
        tax_id=project.tax_id,
        industry=project.industry,
        status=project.status,
        address=project.address,
        incorporation_date=project.incorporation_date,
        created_at=project.created_at,
        applications=[AdminLoanApplicationRow.model_validate(row) for row in rows],
        kyb=await _latest_kyb_for_project(db, project_id),
    )


async def _latest_kyb_for_project(
    db: AsyncSession, project_id: UUID
) -> AdminKybVerificationOut | None:
    """The engine half of the gate, for whoever owns this company.

    Joined through project_ownerships because KYB is a property of the USER who
    verified, not of the project row. Newest attempt wins, matching
    gverify.kyb_service.latest_for_user — a retry after a rejection is the one
    that counts.
    """
    row = (
        await db.execute(
            select(GVerifyKybVerification)
            .join(
                ProjectOwnership,
                ProjectOwnership.user_id == GVerifyKybVerification.user_id,
            )
            .where(ProjectOwnership.project_id == project_id)
            .order_by(GVerifyKybVerification.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    return AdminKybVerificationOut.model_validate(row) if row else None


async def resolve_kyb_verification(
    db: AsyncSession,
    verification_id: UUID,
    *,
    decision: AdminDecision,
    note: str | None,
    actor: User,
    ip_address: str | None = None,
) -> AdminKybVerificationOut:
    """Settle a KYB attempt the engine parked for a human.

    Only MANUAL_REVIEW is resolvable. An APPROVED or REJECTED attempt is the
    provider's own verdict and an operator must not quietly overwrite it — a
    retry creates a new attempt, which is the supported way to change the
    answer.
    """
    row = await db.get(GVerifyKybVerification, verification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="KYB verification not found")
    if row.status != STATUS_MANUAL_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"Only a MANUAL_REVIEW attempt can be resolved (this one is {row.status})",
        )

    before = {"status": row.status}
    row.status = decision.value
    if decision is AdminDecision.rejected:
        row.rejection_reason = note or row.rejection_reason
    db.add(row)
    append_audit(
        db,
        entity_type="GVERIFY_KYB_VERIFICATION",
        entity_id=row.id,
        action="KYB_MANUAL_REVIEW_RESOLVED",
        actor_id=actor.id,
        before_state=before,
        after_state={"status": row.status, "note": note},
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(row)
    return AdminKybVerificationOut.model_validate(row)


async def decide_application(
    db: AsyncSession,
    application_id: UUID,
    *,
    decision: AdminDecision,
    note: str | None,
    actor: User,
    ip_address: str | None = None,
) -> AdminLoanApplicationRow:
    """Record the operator half of the two-approval gate.

    One-way: a decided application cannot be re-decided. The decision is an
    audited event that downstream steps rely on, so flipping it after a
    contract exists would rewrite the basis that contract was created on.
    """
    # selectinload, not db.get(): the response model carries `documents`, and
    # validating a bare ORM row would lazy-load that relationship inside an
    # async session — which raises MissingGreenlet rather than emitting SQL.
    row = (
        await db.execute(
            select(LoanApplication)
            .where(LoanApplication.id == application_id)
            .options(selectinload(LoanApplication.documents))
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if row.admin_approval != "PENDING":
        raise HTTPException(
            status_code=409,
            detail=f"Application has already been {row.admin_approval.lower()}",
        )

    before = {"admin_approval": row.admin_approval}
    row.admin_approval = decision.value
    row.admin_approved_by = actor.id
    row.decided_at = datetime.now(timezone.utc)
    row.decision_note = note
    db.add(row)
    append_audit(
        db,
        entity_type="LOAN_APPLICATION",
        entity_id=row.id,
        action="APPLICATION_ADMIN_DECIDED",
        actor_id=actor.id,
        before_state=before,
        after_state={"admin_approval": row.admin_approval, "note": note},
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(row, ["admin_approval", "decided_at", "decision_note"])
    return AdminLoanApplicationRow.model_validate(row)


async def set_user_status(
    db: AsyncSession,
    user_id: UUID,
    *,
    new_status: str,
    note: str | None,
    actor: User,
    ip_address: str | None = None,
) -> AdminUserDetail:
    """Change an account's status.

    SUSPENDED is a real deny: get_current_user rejects the account on every
    request and login refuses to mint a new session. So the two guards below
    are not politeness, they are the difference between a reversible action
    and an unrecoverable one.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    # 1. No self-suspension. An operator who suspends their own account cannot
    #    undo it — the endpoint that would restore it is the one they just
    #    locked themselves out of.
    if target.id == actor.id:
        raise HTTPException(
            status_code=409, detail="You cannot change your own account status"
        )

    # 2. Only a SYSTEM_ADMIN may act on an admin-level account. Otherwise any
    #    ADMIN can suspend their peers — and every other ADMIN plus the
    #    SYSTEM_ADMIN — which is a takeover, not moderation.
    privileged = (Role.ADMIN.value, Role.SYSTEM_ADMIN.value)
    if target.role in privileged and actor.role != Role.SYSTEM_ADMIN.value:
        raise HTTPException(
            status_code=403,
            detail="Only a system admin can change an admin account's status",
        )

    before = {"status": target.status}
    target.status = new_status
    db.add(target)
    append_audit(
        db,
        entity_type="USER",
        entity_id=target.id,
        action="USER_STATUS_CHANGED",
        actor_id=actor.id,
        before_state=before,
        after_state={"status": new_status, "note": note},
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(target)
    return AdminUserDetail.model_validate(target)


async def list_audit_logs(
    db: AsyncSession,
    *,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    actor_id: UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = 100,
) -> list[AuditLogOut]:
    # Resolve the two UUID columns to user info in a single query (no N+1):
    #   - actor   -> the user who performed the action
    #   - entity_user -> the subject user, only when entity_type == 'USER'
    actor = aliased(User)
    entity_user = aliased(User)
    stmt = (
        select(AuditLog, actor, entity_user)
        .outerjoin(actor, actor.id == AuditLog.actor_id)
        .outerjoin(
            entity_user,
            and_(entity_user.id == AuditLog.entity_id, AuditLog.entity_type == "USER"),
        )
        .order_by(AuditLog.created_at.desc())
    )
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if created_after:
        stmt = stmt.where(AuditLog.created_at >= created_after)
    if created_before:
        stmt = stmt.where(AuditLog.created_at <= created_before)

    result = await db.execute(stmt.limit(limit))
    rows = result.all()
    out_list = []
    for log, actor_row, entity_user_row in rows:
        out = AuditLogOut.model_validate(log)
        out.actor = ActorOut.model_validate(actor_row) if actor_row else None
        out.entity_user = ActorOut.model_validate(entity_user_row) if entity_user_row else None
        out_list.append(out)
    return out_list
