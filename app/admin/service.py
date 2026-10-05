import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.sql import Select

# Only HTTPException: `status` is already a filter-parameter name all through
# this module, so importing fastapi.status would shadow it inside every list
# function. The one status code used below is written out instead.
from fastapi import BackgroundTasks, HTTPException

from app.admin.schemas import (
    ActorOut,
    AdminDecision,
    AdminDocumentUrlResponse,
    AdminKybVerificationOut,
    AdminKycAccountRef,
    AdminKycConflict,
    AdminKycImageUrlResponse,
    AdminKycVerificationOut,
    AdminLoanApplicationRow,
    AdminMode,
    AdminOverview,
    AdminProjectDetail,
    AdminScoreRunOut,
    AdminStats,
    AdminUserDetail,
    AuditLogOut,
    Page,
    ProjectRow,
    UNASSIGNED_ROLE,
    UserRow,
)
from app.gverify import service as gverify_service
from app.gverify import storage as gverify_storage
from app.gverify.models import (
    STATUS_APPROVED,
    STATUS_MANUAL_REVIEW,
    GVerifyKybVerification,
    GVerifyVerification,
)
from app.lending.models import (
    AuditLog,
    LoanApplication,
    LoanApplicationDocument,
    Project,
    ProjectOwnership,
    ScoreRun,
)
from app.notifications.models import (
    EVENT_APPLICATION_APPROVED,
    EVENT_APPLICATION_REJECTED,
)
from app.notifications import realtime
from app.utils.email import send_application_updated_email
from app.notifications.service import create_notification
from app.projects.service import activate_project_if_approved
from app.users.models import Role, User
from app.utils import r2
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

    # A user who has registered but not yet picked a role at /select-role has
    # role NULL (migration f2b3c4d5e6f7), so this group_by returns a None key.
    # It is bucketed under a real label rather than dropped: "how many people
    # signed up and never chose a role" is a number an operator wants, and a
    # None key fails `dict[str, int]` validation outright -- which 500'd the
    # whole overview endpoint in BOTH modes, since get_stats runs either way.
    users_by_role = {
        (role or UNASSIGNED_ROLE): count
        for role, count in (
            await db.execute(select(User.role, func.count(User.id)).group_by(User.role))
        ).all()
    }
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

    # Newest score run per application, in one query rather than N.
    # A company with several funding requests would otherwise cost a round
    # trip each, and the panel renders them all at once.
    latest_runs: dict[UUID, ScoreRun] = {}
    if rows:
        run_rows = (
            await db.execute(
                select(ScoreRun)
                .where(ScoreRun.application_id.in_([r.id for r in rows]))
                .order_by(ScoreRun.created_at.desc())
            )
        ).scalars().all()
        for run in run_rows:
            latest_runs.setdefault(run.application_id, run)

    # Why an ungraded run has no score, as of now. One assemble per such
    # application; the panel shows few, and only ungraded ones pay for it.
    from app.loans.grading_inputs import assemble

    missing: dict[UUID, list[str]] = {}
    for application_id, run in latest_runs.items():
        if run.decision == "INSUFFICIENT_DATA":
            missing[application_id] = (await assemble(db, application_id))["blockers"]

    kyb = await _latest_kyb_for_project(db, project_id)
    registration_verified = bool(kyb and kyb.is_approved)

    def _row(application) -> AdminLoanApplicationRow:
        out = AdminLoanApplicationRow.model_validate(application)
        out = out.model_copy(
            update={
                "missing_documents": missing_documents(
                    application.documents, registration_verified=registration_verified
                )
            }
        )
        run = latest_runs.get(application.id)
        if run is not None:
            out = out.model_copy(
                update={
                    "score_run": AdminScoreRunOut(
                        id=run.id,
                        status=run.status,
                        decision=run.decision,
                        final_grade=run.final_grade_precise,
                        interest_rate_pct=run.interest_rate_pct,
                        engine_version=run.engine_version,
                        params_version=run.params_version,
                        created_at=run.created_at,
                        missing_inputs=missing.get(application.id, []),
                    )
                }
            )
        return out

    return AdminProjectDetail(
        id=project.id,
        legal_name=project.legal_name,
        tax_id=project.tax_id,
        industry=project.industry,
        status=project.status,
        address=project.address,
        incorporation_date=project.incorporation_date,
        created_at=project.created_at,
        applications=[_row(row) for row in rows],
        kyb=kyb,
    )


# What the SME wizard requires, in wizard order. The business registration is
# normally not uploaded: the wizard takes it from the verified business
# certificate, so an approved verification covers it.
REQUIRED_DOCUMENT_TYPES = (
    "legal_charter",
    "business_registration",
    "e_invoice_data",
    "tax_filings",
    "cic_report",
)


def missing_documents(documents, *, registration_verified: bool) -> list[str]:
    """Required document types without a confirmed upload. A PENDING row (a
    presign whose object never landed) is not a document."""
    uploaded = {d.document_type for d in documents if d.status == "UPLOADED"}
    return [
        t
        for t in REQUIRED_DOCUMENT_TYPES
        if t not in uploaded and not (t == "business_registration" and registration_verified)
    ]


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
    background_tasks: BackgroundTasks | None = None,
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

    if decision.value == "APPROVED":
        owner_kyb = (
            await db.execute(
                select(GVerifyKybVerification)
                .join(ProjectOwnership, ProjectOwnership.user_id == GVerifyKybVerification.user_id)
                .where(ProjectOwnership.project_id == row.project_id)
                .order_by(GVerifyKybVerification.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        missing = missing_documents(
            row.documents,
            registration_verified=bool(owner_kyb and owner_kyb.status == STATUS_APPROVED),
        )
        if missing:
            raise HTTPException(
                status_code=409,
                detail=f"Required documents missing: {', '.join(missing)}",
                headers={"X-Error-Code": "DOCUMENTS_MISSING"},
            )

    before = {"admin_approval": row.admin_approval, "status": row.status}
    row.admin_approval = decision.value
    row.admin_approved_by = actor.id
    row.decided_at = datetime.now(timezone.utc)
    row.decision_note = note

    # A rejection ends the application, so the applicant-facing `status` has to
    # move with it. `admin_approval` is an internal gate column -- the SME
    # dashboard renders `status` -- so a rejection that only set the gate left
    # the applicant on "Submitted -- awaiting review" forever, waiting for an
    # answer that had already been given.
    #
    # An approval deliberately does NOT move `status`. This is one half of a
    # two-approval gate: create_contract() still requires the engine's, so
    # telling the applicant "approved" here would promise an outcome the other
    # half can still refuse. It stays under review until there is an offer.
    if decision is AdminDecision.rejected:
        row.status = "REJECTED"
    db.add(row)

    # The operator's half just landed; if the engine's (a LOCKED score run) is
    # already there, the project goes on the marketplace now.
    if decision is AdminDecision.approved:
        await db.flush()
        await activate_project_if_approved(db, row.id, actor_id=actor.id)

    # Tell the applicant. Staged on THIS transaction, not sent from it: if the
    # decision rolls back the notification goes with it, so nobody is ever told
    # about a verdict that was not recorded.
    #
    # One per owner rather than one per project: co-owners both act on the
    # company's behalf, and a decision only the first of them heard about is
    # how a company misses its own answer.
    owner_ids = (
        (
            await db.execute(
                select(ProjectOwnership.user_id).where(
                    ProjectOwnership.project_id == row.project_id
                )
            )
        )
        .scalars()
        .all()
    )
    event = (
        EVENT_APPLICATION_REJECTED
        if decision is AdminDecision.rejected
        else EVENT_APPLICATION_APPROVED
    )
    for owner_id in owner_ids:
        create_notification(
            db,
            user_id=owner_id,
            event=event,
            entity_type="LOAN_APPLICATION",
            entity_id=row.id,
            # Only what the copy needs to name the request. The operator's
            # note is deliberately absent: it is already on the application
            # the notification points at, and a copy here would go stale the
            # moment anyone edited the original.
            data={"amount_vnd": int(row.requested_amount)},
        )
    append_audit(
        db,
        entity_type="LOAN_APPLICATION",
        entity_id=row.id,
        action="APPLICATION_ADMIN_DECIDED",
        actor_id=actor.id,
        before_state=before,
        after_state={
            "admin_approval": row.admin_approval,
            "status": row.status,
            "note": note,
        },
        ip_address=ip_address,
    )
    await db.commit()
    # Only after the commit: a rolled-back decision must never ping anyone.
    await realtime.publish(owner_ids)
    if background_tasks is not None and owner_ids:
        # An email per owner, off the request: SMTP is blocking and slow, and a
        # mail failure must not turn a recorded decision into an error.
        addresses = (
            (await db.execute(select(User.email).where(User.id.in_(owner_ids))))
            .scalars()
            .all()
        )
        for address in addresses:
            background_tasks.add_task(send_application_updated_email, address)
    await db.refresh(row, ["admin_approval", "status", "decided_at", "decision_note"])
    return AdminLoanApplicationRow.model_validate(row)


# Ten minutes, matching the KYB certificate link: long enough to read a
# document, short enough that a pasted URL stops working quickly.
DOCUMENT_URL_TTL_SECONDS = 600


async def get_document_url(
    db: AsyncSession, document_id: UUID
) -> AdminDocumentUrlResponse:
    """A short-lived read URL for an uploaded application document.

    Admin-only at the route. No ownership filter is applied here on purpose —
    an operator reviewing applications is meant to see any of them — but the
    URL is still presigned and expiring rather than a public object.
    """
    row = await db.get(LoanApplicationDocument, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if row.status != "UPLOADED":
        # A presigned URL for an object that was never confirmed would 404 at
        # the storage layer; say so here instead.
        raise HTTPException(
            status_code=409, detail="This document was never finished uploading"
        )
    if not r2.is_configured():
        raise HTTPException(status_code=503, detail="Document storage is not configured")

    return AdminDocumentUrlResponse(
        url=r2.presign_get(row.file_key, expires_in=DOCUMENT_URL_TTL_SECONDS),
        expires_in=DOCUMENT_URL_TTL_SECONDS,
        content_type=row.content_type,
        original_filename=row.original_filename,
    )


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


# --------------------------------------------------------------------------- #
# KYC manual review
#
# The GVerify KYC engine parks an attempt in MANUAL_REVIEW when the face match
# would approve but another account already holds an APPROVED KYC with the same
# ID number (app/gverify/service.py). Face match alone can't tell a real selfie
# from the card's own portrait cropped and resubmitted, so a person decides.
# --------------------------------------------------------------------------- #

KYC_REVIEW_LIST_LIMIT = 100

# What the investor sees after a rejection. The operator's note is never shown
# to them: it is written for colleagues and may name the suspected fraud or the
# other account, which would confirm to an impostor whose identity they hold.
KYC_REJECTED_REASON = (
    "We could not complete your identity verification. Please contact support."
)


def _kyc_account_ref(user: User) -> AdminKycAccountRef:
    return AdminKycAccountRef(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        status=user.status,
        created_at=user.created_at,
    )


async def _kyc_verification_out(
    db: AsyncSession, row: GVerifyVerification
) -> AdminKycVerificationOut:
    owner = await db.get(User, row.user_id)
    conflicts = []
    for other in await gverify_service.approved_elsewhere(db, row.person_number, row.user_id):
        other_user = await db.get(User, other.user_id)
        if other_user is None:
            continue
        conflicts.append(
            AdminKycConflict(
                verification_id=other.id,
                user=_kyc_account_ref(other_user),
                full_name=other.full_name,
                date_of_birth=other.date_of_birth,
                approved_at=other.updated_at or other.created_at,
            )
        )
    return AdminKycVerificationOut(
        id=row.id,
        status=row.status,
        is_approved=row.is_approved,
        rejection_reason=row.rejection_reason,
        person_number=row.person_number,
        full_name=row.full_name,
        date_of_birth=row.date_of_birth,
        face_match_score=gverify_service.face_match_score(row),
        created_at=row.created_at,
        user=_kyc_account_ref(owner),
        conflicts=conflicts,
    )


async def _get_kyc_verification(db: AsyncSession, verification_id: UUID, *, for_update=False):
    query = select(GVerifyVerification).where(
        GVerifyVerification.id == verification_id,
        GVerifyVerification.verification_type == "KYC",
    )
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="KYC verification not found")
    return row


async def list_kyc_verifications(
    db: AsyncSession, *, status: str = STATUS_MANUAL_REVIEW
) -> list[AdminKycVerificationOut]:
    """KYC attempts in one status, oldest first so the queue is worked in order.

    Defaults to MANUAL_REVIEW, the only status an operator can act on. A parked
    attempt is always its account's newest (resubmission is refused while one
    is in review), so this is one row per waiting investor.
    """
    rows = (
        await db.execute(
            select(GVerifyVerification)
            .where(
                GVerifyVerification.verification_type == "KYC",
                GVerifyVerification.status == status,
            )
            .order_by(GVerifyVerification.created_at)
            .limit(KYC_REVIEW_LIST_LIMIT)
        )
    ).scalars().all()
    return [await _kyc_verification_out(db, row) for row in rows]


async def get_kyc_verification(
    db: AsyncSession, verification_id: UUID
) -> AdminKycVerificationOut:
    return await _kyc_verification_out(db, await _get_kyc_verification(db, verification_id))


async def get_kyc_image_url(
    db: AsyncSession, verification_id: UUID, name: str
) -> AdminKycImageUrlResponse:
    """A 10-minute read URL for one of an attempt's submitted images."""
    row = await _get_kyc_verification(db, verification_id)
    if name not in gverify_storage.KYC_DOCUMENT_NAMES:
        raise HTTPException(status_code=404, detail="Unknown KYC image")
    located = await asyncio.to_thread(gverify_storage.find_kyc_document, row.id, name)
    if located is None:
        # Retention is best-effort: storage may have been down or unconfigured
        # when the attempt ran. Say so rather than hand out a dead link.
        raise HTTPException(status_code=404, detail="This image was not stored")
    key, content_type = located
    return AdminKycImageUrlResponse(
        url=await asyncio.to_thread(gverify_storage.presign_kyc_document, key),
        expires_in=gverify_storage.CERTIFICATE_URL_TTL_SECONDS,
        content_type=content_type,
    )


async def resolve_kyc_verification(
    db: AsyncSession,
    verification_id: UUID,
    *,
    decision: AdminDecision,
    note: str | None,
    actor: User,
    ip_address: str | None = None,
) -> AdminKycVerificationOut:
    """Settle a KYC attempt the engine parked for a human.

    Rules, beyond the KYB equivalent:
      * Approving needs a note. It overrides a fraud signal (this ID is already
        verified on another account), so the reason has to be on record.
      * Nobody may settle their own attempt.
      * The row is locked, so two operators can't settle it at once.
      * The note stays internal; a rejected investor sees a fixed message.
    """
    row = await _get_kyc_verification(db, verification_id, for_update=True)
    if row.status != STATUS_MANUAL_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"Only a MANUAL_REVIEW attempt can be resolved (this one is {row.status})",
        )
    if row.user_id == actor.id:
        raise HTTPException(status_code=403, detail="You cannot review your own verification")
    note = (note or "").strip() or None
    if decision is AdminDecision.approved and not note:
        raise HTTPException(
            status_code=400,
            detail="A note is required to approve a flagged verification",
        )

    conflicts = await gverify_service.approved_elsewhere(db, row.person_number, row.user_id)
    before = {"status": row.status}
    row.status = decision.value
    row.rejection_reason = None if decision is AdminDecision.approved else KYC_REJECTED_REASON
    append_audit(
        db,
        entity_type="GVERIFY_KYC_VERIFICATION",
        entity_id=row.id,
        action="KYC_MANUAL_REVIEW_RESOLVED",
        actor_id=actor.id,
        before_state=before,
        after_state={
            "status": row.status,
            "note": note,
            "user_id": str(row.user_id),
            "conflicting_verification_ids": [str(c.id) for c in conflicts],
        },
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(row)
    return await _kyc_verification_out(db, row)
