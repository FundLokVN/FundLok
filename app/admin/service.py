import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.sql import Select

# Only HTTPException: `status` is already a filter-parameter name all through
# this module, so importing fastapi.status would shadow it inside every list
# function. The one status code used below is written out instead.
from fastapi import BackgroundTasks, HTTPException

from app.admin.schemas import (
    AdminSignatureCheck,
    AdminPaymentAccountOut,
    ActorOut,
    AdminDecision,
    AdminDocumentUrlResponse,
    AdminKybCertificateUrlResponse,
    AdminKybReviewOut,
    AdminKybVerificationOut,
    AdminKycAccountRef,
    AdminKycConflict,
    AdminKycImageUrlResponse,
    AdminKycVerificationOut,
    AdminLoanApplicationRow,
    AdminMode,
    AdminOverview,
    AdminProjectDetail,
    AdminProjectOwnerOut,
    AdminScoreRunOut,
    AdminStats,
    AdminUserDetail,
    AdminUserFullDetail,
    AdminUserInvestmentSummary,
    AdminUserProjectRef,
    AuditLogOut,
    OVERRIDE_NOTE_MIN_LENGTH,
    Page,
    ProjectRow,
    UNASSIGNED_ROLE,
    UserRow,
    AdminConsentRecordOut,
    AdminUserConsent,
)
from app.consent import documents as consent_documents
from app.consent import service as consent_service
from app.core.config import settings
from app.gverify import kyb_service
from app.gverify import service as gverify_service
from app.gverify import storage as gverify_storage
from app.gverify.normalize import normalize_identifier
from app.gverify.models import (
    STATUS_APPROVED,
    STATUS_MANUAL_REVIEW,
    STATUS_REJECTED,
    GVerifyKybVerification,
    GVerifyVerification,
)
from app.lending.models import (
    AuditLog,
    Holding,
    LoanApplication,
    LoanApplicationDocument,
    Order,
    Project,
    ProjectOwnership,
    ScoreRun,
)
from app.notifications.models import (
    EVENT_APPLICATION_APPROVED,
    EVENT_APPLICATION_REJECTED,
)
from app.notifications import realtime
from app.auth.service import password_fingerprint
from app.utils.email import send_admin_invite_email, send_application_updated_email
from app.utils.jwt import create_password_reset_token
from app.notifications.service import create_notification
from app.projects.service import activate_project_if_approved
from app.banking.models import LinkedAccount
from app.direct_transfer.accounts import annotate_name_match, list_payment_accounts
from app.direct_transfer.schemas import FundingWindowOut, PaymentAccountOut
from app.direct_transfer.accounts import decide_payment_account
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import FundingWindow, PaymentAccount
from app.direct_transfer.windows import get_window_progress
from app.users.models import (
    USER_STATUS_ACTIVE,
    RefreshToken,
    Role,
    TotpRecoveryCode,
    User,
    WebAuthnCredential,
)
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


# What "ALL" means on the KYC and KYB review lists: every attempt a reviewer
# browses. FAILED (a provider outage) has nothing to review, and PENDING is
# still in flight.
REVIEW_LIST_ALL = "ALL"
REVIEW_LIST_STATUSES = (STATUS_MANUAL_REVIEW, STATUS_APPROVED, STATUS_REJECTED)


async def _review_page(db: AsyncSession, model, filters: list, *, status: str, page: int, page_size: int, out) -> Page:
    """One page of KYC or KYB attempts, built row by row with `out`.

    Attempts waiting for a reviewer come first and oldest first, the order the
    queue is worked in. Decided ones follow, newest first, since history is
    read from the latest decision back.
    """
    if status == REVIEW_LIST_ALL:
        filters = [*filters, model.status.in_(REVIEW_LIST_STATUSES)]
    else:
        filters = [*filters, model.status == status]
    stmt = select(model).where(*filters)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    waiting = model.status == STATUS_MANUAL_REVIEW
    rows = (
        await db.execute(
            stmt.order_by(
                waiting.desc(),
                case((waiting, model.created_at)).asc().nulls_last(),
                model.created_at.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return Page(
        items=[await out(db, row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
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
    page_out = await _paginate(db, stmt, page=page, page_size=page_size, row_model=UserRow, order_col=User.created_at)
    # Whether each user on the page accepted the current Terms: one query for
    # the page, judged exactly as the sign-up gate judges it.
    states = await consent_service.states_for(db, page_out.items)
    for row in page_out.items:
        row.consent_status, row.consent_accepted_at = states[row.id]
    return page_out


async def get_user_detail(db: AsyncSession, user_id: UUID) -> AdminUserFullDetail:
    """One account: the whole profile plus what it is connected to — owned
    projects, investor activity and security posture.

    The user-side twin of get_project_detail. Read-only, admin-only, not
    paginated: a user owns a handful of companies at most, and the investment
    side is aggregated rather than listed.
    """
    user = await db.get(User, user_id, populate_existing=True)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    project_rows = (
        await db.execute(
            select(Project, ProjectOwnership.role)
            .join(ProjectOwnership, ProjectOwnership.project_id == Project.id)
            .where(ProjectOwnership.user_id == user_id)
            .order_by(Project.created_at.desc())
        )
    ).all()

    project_ids = [project.id for project, _ in project_rows]
    app_stats: dict[UUID, tuple[int, int]] = {}
    if project_ids:
        app_stats_rows = (
            await db.execute(
                select(
                    LoanApplication.project_id,
                    func.count(LoanApplication.id),
                    func.coalesce(func.sum(LoanApplication.requested_amount), 0),
                )
                .where(LoanApplication.project_id.in_(project_ids))
                .group_by(LoanApplication.project_id)
            )
        ).all()
        for pid, count, total in app_stats_rows:
            app_stats[pid] = (count, int(total))

    projects = [
        AdminUserProjectRef(
            id=project.id,
            legal_name=project.legal_name,
            tax_id=project.tax_id,
            industry=project.industry,
            status=project.status,
            ownership_role=role,
            employee_count=project.employee_count,
            applications_count=app_stats.get(project.id, (0, 0))[0],
            total_requested_amount=app_stats.get(project.id, (0, 0))[1],
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
        for project, role in project_rows
    ]

    # Aggregated here rather than listed so the panel never needs a second
    # call. COALESCE because SUM over no rows is NULL and the schema is int.
    orders_count, orders_amount = (
        await db.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0))
            .where(Order.investor_id == user_id)
        )
    ).one()
    holdings_count, holdings_principal = (
        await db.execute(
            select(
                func.count(Holding.id),
                func.coalesce(func.sum(Holding.principal), 0),
            ).where(Holding.investor_id == user_id)
        )
    ).one()

    # Security & session insights
    active_sessions_count = (
        await db.execute(
            select(func.count(RefreshToken.id)).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > func.now(),
            )
        )
    ).scalar() or 0

    last_sign_in_at = (
        await db.execute(
            select(func.max(func.coalesce(RefreshToken.last_used_at, RefreshToken.created_at))).where(
                RefreshToken.user_id == user_id
            )
        )
    ).scalar()

    passkeys_count = (
        await db.execute(
            select(func.count(WebAuthnCredential.id)).where(WebAuthnCredential.user_id == user_id)
        )
    ).scalar() or 0

    unused_recovery_codes_count = (
        await db.execute(
            select(func.count(TotpRecoveryCode.id)).where(
                TotpRecoveryCode.user_id == user_id,
                TotpRecoveryCode.used_at.is_(None),
            )
        )
    ).scalar() or 0

    # Identity verification & banking insights
    kyc_status = (
        await db.execute(
            select(GVerifyVerification.status)
            .where(
                GVerifyVerification.user_id == user_id,
                GVerifyVerification.verification_type == "KYC",
            )
            .order_by(GVerifyVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    linked_accounts_count = (
        await db.execute(
            select(func.count(LinkedAccount.id)).where(
                LinkedAccount.user_id == user_id,
                LinkedAccount.status == "ACTIVE",
            )
        )
    ).scalar() or 0
    payment_accounts = await list_payment_accounts(db, user_id=user_id)
    consent_state, consent_accepted_at = (await consent_service.states_for(db, [user]))[user.id]
    consent_records = await consent_service.history(db, user.id)

    return AdminUserFullDetail(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        status=user.status,
        email_verified=user.email_verified,
        created_at=user.created_at,
        updated_at=user.updated_at,
        phone=user.phone,
        bio=user.bio,
        avatar_key=user.avatar_key,
        email_signature=user.email_signature,
        # Derived, not stored: Google-OAuth users and freshly invited admins
        # have no password at all — worth knowing before suspending an account
        # whose owner would then have no way back in.
        has_password=user.password_hash is not None,
        signin_alerts_enabled=user.signin_alerts_enabled,
        totp_enabled=user.totp_enabled,
        totp_confirmed_at=user.totp_confirmed_at,
        onboarding_tour_completed_at=user.onboarding_tour_completed_at,
        active_sessions_count=active_sessions_count,
        last_sign_in_at=last_sign_in_at,
        passkeys_count=passkeys_count,
        unused_recovery_codes_count=unused_recovery_codes_count,
        kyc_status=kyc_status,
        linked_accounts_count=linked_accounts_count,
        payment_accounts=await _admin_payment_accounts(db, payment_accounts),
        projects=projects,
        consent=AdminUserConsent(
            status=consent_state,
            current_version=consent_documents.CURRENT_VERSION,
            accepted_at=consent_accepted_at,
            records=[AdminConsentRecordOut.model_validate(r) for r in consent_records],
        ),
        investments=AdminUserInvestmentSummary(
            orders_count=orders_count,
            orders_amount=orders_amount,
            holdings_count=holdings_count,
            holdings_principal=holdings_principal,
        ),
    )


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

    # The newest NEAC check of each signed listing document (the Service
    # Agreement and the Standing Offer, checked when the SME uploaded them),
    # in one query.
    from app.loans.agreement import neac_check
    from app.uploads.schemas import SIGNED_DOCUMENT_TYPES

    checks = await neac_check.latest_checks(
        db, [d.id for r in rows for d in r.documents if d.document_type in SIGNED_DOCUMENT_TYPES]
    )

    def _row(application) -> AdminLoanApplicationRow:
        out = AdminLoanApplicationRow.model_validate(application)
        out = out.model_copy(
            update={
                "missing_documents": missing_documents(
                    application.documents, registration_verified=registration_verified
                ),
                "documents": [
                    d.model_copy(update={"signature_check": AdminSignatureCheck.from_verdict(checks[d.id])})
                    if d.id in checks
                    else d
                    for d in out.documents
                ],
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
        owner=await _project_owner(db, project_id),
        funding_window=await _latest_funding_window(db, project_id),
    )


async def _latest_funding_window(db: AsyncSession, project_id: UUID) -> FundingWindowOut | None:
    window = (
        await db.execute(
            select(FundingWindow)
            .where(FundingWindow.project_id == project_id)
            .order_by(FundingWindow.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if window is None:
        return None
    progress = await get_window_progress(db, listing_id=window.listing_id, now=datetime.now(timezone.utc))
    return FundingWindowOut.model_validate(progress)


async def open_project_for_funding(
    db: AsyncSession, project_id: UUID, *, actor: User, ip_address: str | None
) -> FundingWindowOut:
    """Open a live project for investment by hand: for one that went live before its SME's bank account was
    approved on this path, or that could not open then. Returns the live window when it is already open; the
    error says why it cannot open otherwise (the audit row records who opened it)."""
    from app.market.funding import publish_project_for_funding

    now = datetime.now(timezone.utc)
    try:
        window = await publish_project_for_funding(
            db, project_id=project_id, now=now, actor_id=actor.id, trigger="ADMIN", ip_address=ip_address
        )
    except DirectTransferError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code}
        ) from exc
    await db.commit()
    return FundingWindowOut.model_validate(
        await get_window_progress(db, listing_id=window.listing_id, now=now)
    )


async def _project_owner(db: AsyncSession, project_id: UUID) -> AdminProjectOwnerOut | None:
    """The company's OWNER account and its latest KYC attempt.

    The first OWNER row: creation makes exactly one, and nothing adds co-owners
    yet. KYC is a property of the user, like KYB in _latest_kyb_for_project.
    """
    user = (
        await db.execute(
            select(User)
            .join(ProjectOwnership, ProjectOwnership.user_id == User.id)
            .where(ProjectOwnership.project_id == project_id, ProjectOwnership.role == "OWNER")
            .order_by(ProjectOwnership.created_at)
            .limit(1)
        )
    ).scalars().first()
    if user is None:
        return None
    kyc = await gverify_service.latest_for_user(db, user.id, "KYC")
    return AdminProjectOwnerOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone=user.phone,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        kyc_verification_id=kyc.id if kyc else None,
        kyc_status=kyc.status if kyc else None,
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
    business_name: str | None = None,
    tax_code: str | None = None,
    representative_id_numbers: list[str] | None = None,
) -> AdminKybVerificationOut:
    """Settle a KYB attempt parked for a human.

    Only MANUAL_REVIEW is resolvable. An APPROVED or REJECTED attempt is the
    provider's own verdict and an operator must not quietly overwrite it — a
    retry creates a new attempt, which is the supported way to change the
    answer. The row is locked so two operators can't settle it at once, and
    nobody may settle their own attempt.

    An attempt submitted in manual verification mode had no OCR and no
    registry check: approving it needs the business name and tax code as
    printed on the certificate (the SME's declared code pre-fills the latter).
    On an attempt the provider read, those fields are the provider's and are
    not editable here.
    """
    row = (
        await db.execute(
            select(GVerifyKybVerification)
            .where(GVerifyKybVerification.id == verification_id)
            .with_for_update()
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="KYB verification not found")
    if row.status != STATUS_MANUAL_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"Only a MANUAL_REVIEW attempt can be resolved (this one is {row.status})",
        )
    if row.user_id == actor.id:
        raise HTTPException(status_code=403, detail="You cannot review your own verification")

    provider_checked = kyb_service.provider_checked(row)
    if provider_checked and (business_name or tax_code):
        raise HTTPException(
            status_code=400,
            detail="This attempt's business details were read by the provider and cannot be edited",
        )
    if not provider_checked:
        effective_name = business_name or row.business_name
        effective_tax_code = tax_code or row.tax_code
        if decision is AdminDecision.approved and not (effective_name and effective_tax_code):
            raise HTTPException(
                status_code=400,
                detail="Enter the business name and tax code from the certificate to approve this verification",
            )
        if decision is AdminDecision.approved and settings.GVERIFY_KYB_REQUIRE_REP_MATCH:
            sme_id_number = await kyb_service.approved_kyc_person_number(db, row.user_id)
            clean_reps = [
                normalize_identifier(x) for x in (representative_id_numbers or []) if x
            ]
            if not sme_id_number or normalize_identifier(sme_id_number) not in clean_reps:
                raise HTTPException(
                    status_code=400,
                    detail=kyb_service.NOT_A_REPRESENTATIVE_REASON,
                )
        row.business_name = effective_name
        row.tax_code = effective_tax_code
        if tax_code and not row.license_code:
            row.license_code = tax_code

    note = (note or "").strip() or None
    before = {"status": row.status}
    row.status = decision.value
    if decision is AdminDecision.rejected:
        row.rejection_reason = note or row.rejection_reason
    else:
        row.rejection_reason = None
    db.add(row)
    append_audit(
        db,
        entity_type="GVERIFY_KYB_VERIFICATION",
        entity_id=row.id,
        action="KYB_MANUAL_REVIEW_RESOLVED",
        actor_id=actor.id,
        before_state=before,
        after_state={
            "status": row.status,
            "note": note,
            **(
                {
                    "provider_checked": False,
                    "business_name": row.business_name,
                    "tax_code": row.tax_code,
                }
                if not provider_checked
                else {}
            ),
        },
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(row)
    return AdminKybVerificationOut.model_validate(row)



async def _kyb_review_out(db: AsyncSession, row: GVerifyKybVerification) -> AdminKybReviewOut:
    owner = await db.get(User, row.user_id)
    kyc = await gverify_service.latest_for_user(db, row.user_id)
    approved_kyc = kyc if kyc is not None and kyc.is_approved else None
    projects = (
        await db.execute(
            select(Project.legal_name)
            .join(ProjectOwnership, ProjectOwnership.project_id == Project.id)
            .where(ProjectOwnership.user_id == row.user_id)
            .order_by(Project.created_at)
        )
    ).scalars().all()
    return AdminKybReviewOut(
        id=row.id,
        status=row.status,
        is_approved=row.is_approved,
        rejection_reason=row.rejection_reason,
        document_type=row.document_type,
        tax_code=row.tax_code,
        license_code=row.license_code[:32] if row.license_code else None,
        business_name=row.business_name,
        business_type=row.business_type,
        company_address=row.company_address,
        provider_checked=kyb_service.provider_checked(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
        user=_kyc_account_ref(owner),
        kyc_full_name=approved_kyc.full_name if approved_kyc else None,
        kyc_person_number=approved_kyc.person_number if approved_kyc else None,
        projects=[name for name in projects if name],
    )


async def _get_kyb_verification(db: AsyncSession, verification_id: UUID) -> GVerifyKybVerification:
    row = await db.get(GVerifyKybVerification, verification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="KYB verification not found")
    return row


async def list_kyb_verifications(
    db: AsyncSession, *, status: str = STATUS_MANUAL_REVIEW, page: int = 1, page_size: int = 20
) -> Page[AdminKybReviewOut]:
    """KYB attempts in one status, or ALL, a page at a time (_review_page)."""
    return await _review_page(
        db, GVerifyKybVerification, [], status=status, page=page, page_size=page_size, out=_kyb_review_out
    )


async def get_kyb_verification(db: AsyncSession, verification_id: UUID) -> AdminKybReviewOut:
    return await _kyb_review_out(db, await _get_kyb_verification(db, verification_id))


async def get_kyb_certificate_url(
    db: AsyncSession,
    verification_id: UUID,
    actor: User | None = None,
    ip_address: str | None = None,
) -> AdminKybCertificateUrlResponse:
    """A 10-minute read URL for an attempt's submitted certificate."""
    row = await _get_kyb_verification(db, verification_id)
    located = await asyncio.to_thread(gverify_storage.find_kyb_certificate, row.id)
    if located is None:
        raise HTTPException(status_code=404, detail="This certificate was not stored")
    key, content_type = located
    if actor is not None:
        append_audit(
            db,
            entity_type="GVERIFY_KYB_VERIFICATION",
            entity_id=row.id,
            action="KYB_CERTIFICATE_VIEWED",
            actor_id=actor.id,
            after_state={
                "subject_user_id": str(row.user_id),
                "expires_in": gverify_storage.CERTIFICATE_URL_TTL_SECONDS,
            },
            ip_address=ip_address,
        )
        await db.commit()
    return AdminKybCertificateUrlResponse(
        url=await asyncio.to_thread(gverify_storage.presign_kyb_certificate, key),
        expires_in=gverify_storage.CERTIFICATE_URL_TTL_SECONDS,
        content_type=content_type,
    )


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
    #
    # FOR UPDATE: the PENDING check below is check-then-act. Unlocked, two
    # concurrent decisions (two operators, or one double-submitting APPROVED
    # and REJECTED) both passed it; the approval put the project on the
    # marketplace and the rejection then overwrote the row. The second
    # request now waits here and sees the first one's decision.
    row = (
        await db.execute(
            select(LoanApplication)
            .where(LoanApplication.id == application_id)
            .options(selectinload(LoanApplication.documents))
            .with_for_update()
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


ADMIN_INVITE_TTL = timedelta(hours=24)


async def invite_admin(
    db: AsyncSession,
    *,
    email: str,
    full_name: str,
    actor: User,
    background_tasks: BackgroundTasks,
    ip_address: str | None = None,
) -> AdminUserDetail:
    """Create an ADMIN account and email the invitee a link to set a password.

    SYSTEM_ADMIN only (enforced by the route): granting admin access is acting
    on an admin-level account, the same line set_user_status draws.

    The account is created with no password, so it cannot be signed into with
    one until the invitee sets theirs through the emailed link -- an ordinary
    password-reset token, single-use because it is bound to the current
    (empty) password. email_verified is set because the address is vouched
    for by the inviter and proven when the link is used; without it the
    frontend's verification gate would trap the new admin on /verify-email.
    """
    # Emails are stored as entered (register does the same), so compare
    # case-insensitively: "Ops@x.com" and "ops@x.com" must not become two
    # accounts, one of them an admin.
    existing = (
        await db.execute(select(User.id).where(func.lower(User.email) == email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(
        email=email,
        full_name=full_name,
        role=Role.ADMIN.value,
        status=USER_STATUS_ACTIVE,
        email_verified=True,
        password_hash=None,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Lost a race with another invite/registration for the same address.
        await db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists") from exc

    is_external = not email.strip().lower().endswith("@fundlok.com")
    append_audit(
        db,
        entity_type="USER",
        entity_id=user.id,
        action="ADMIN_INVITED",
        actor_id=actor.id,
        after_state={
            "email": email,
            "role": Role.ADMIN.value,
            "is_external_domain": is_external,
        },
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(user)

    # Queued only after the commit, so no email ever points at an account
    # that does not exist.
    token = create_password_reset_token(
        user.id,
        password_fingerprint=password_fingerprint(user.password_hash),
        expires_delta=ADMIN_INVITE_TTL,
    )
    background_tasks.add_task(
        send_admin_invite_email,
        to_email=user.email,
        token=token,
        invited_by=actor.full_name or actor.email,
        expires_hours=int(ADMIN_INVITE_TTL.total_seconds() // 3600),
    )
    return AdminUserDetail.model_validate(user)


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


async def _kyc_conflicts(
    db: AsyncSession, others: list[GVerifyVerification]
) -> list[AdminKycConflict]:
    conflicts = []
    for other in others:
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
    return conflicts


async def _kyc_verification_out(
    db: AsyncSession, row: GVerifyVerification
) -> AdminKycVerificationOut:
    owner = await db.get(User, row.user_id)
    conflicts = await _kyc_conflicts(
        db, await gverify_service.approved_elsewhere(db, row.person_number, row.user_id)
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
        provider_checked=gverify_service.provider_checked(row),
        # No ID number yet means nothing was compared; "no conflicts" must not
        # be read as "clear".
        conflicts_evaluated=bool(row.person_number),
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
    db: AsyncSession, *, status: str = STATUS_MANUAL_REVIEW, page: int = 1, page_size: int = 20
) -> Page[AdminKycVerificationOut]:
    """KYC attempts in one status, or ALL, a page at a time (_review_page).

    Defaults to MANUAL_REVIEW, the only status an operator can act on. A parked
    attempt is always its account's newest (resubmission is refused while one
    is in review), so that queue is one row per waiting investor.
    """
    return await _review_page(
        db,
        GVerifyVerification,
        [GVerifyVerification.verification_type == "KYC"],
        status=status,
        page=page,
        page_size=page_size,
        out=_kyc_verification_out,
    )


async def get_kyc_verification(
    db: AsyncSession, verification_id: UUID
) -> AdminKycVerificationOut:
    return await _kyc_verification_out(db, await _get_kyc_verification(db, verification_id))


async def get_kyc_image_url(
    db: AsyncSession,
    verification_id: UUID,
    name: str,
    actor: User | None = None,
    ip_address: str | None = None,
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
    if actor is not None:
        append_audit(
            db,
            entity_type="GVERIFY_KYC_VERIFICATION",
            entity_id=row.id,
            action="KYC_IMAGE_VIEWED",
            actor_id=actor.id,
            after_state={
                "image_name": name,
                "subject_user_id": str(row.user_id),
                "expires_in": gverify_storage.CERTIFICATE_URL_TTL_SECONDS,
            },
            ip_address=ip_address,
        )
        await db.commit()
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
    person_number: str | None = None,
    full_name: str | None = None,
    date_of_birth: str | None = None,
    acknowledged_conflict_ids: list[UUID] | None = None,
) -> AdminKycVerificationOut:
    """Settle a KYC attempt parked for a human.

    Rules, beyond the KYB equivalent:
      * Nobody may settle their own attempt.
      * The row is locked, so two operators can't settle it at once; approvals
        of the same ID number are also serialised (advisory lock).
      * The note stays internal; a rejected investor sees a fixed message.
      * An attempt the provider read and flagged needs a note to approve.
      * An attempt submitted in manual verification mode had no OCR. Approving
        it needs the ID number and name typed from the card.
      * Whatever the mode, if the ID number is already verified on another
        account the approval is refused (409 ID_CONFLICT_ACK_REQUIRED, listing
        those accounts) unless the reviewer acknowledges every one of them in
        ``acknowledged_conflict_ids`` and adds a substantive note. A note
        alone never overrides: the reviewer must have been shown the conflict.
        The override is recorded in the audit log (KYC_ID_CONFLICT_OVERRIDDEN).
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

    provider_checked = gverify_service.provider_checked(row)
    entered = {"person_number": person_number, "full_name": full_name, "date_of_birth": date_of_birth}
    if provider_checked and any(entered.values()):
        raise HTTPException(
            status_code=400,
            detail="This attempt's ID details were read by the provider and cannot be edited",
        )
    if not provider_checked:
        if decision is AdminDecision.approved and not (person_number and full_name):
            raise HTTPException(
                status_code=400,
                detail="Enter the ID number and full name from the card to approve this verification",
            )
        if person_number:
            row.person_number = person_number
        if full_name:
            row.full_name = full_name
        if date_of_birth:
            row.date_of_birth = date_of_birth

    conflicts: list[GVerifyVerification] = []
    if decision is AdminDecision.approved:
        # Serialise on the number, then look: see lock_id_number. Held until
        # the commit/rollback below.
        await gverify_service.lock_id_number(db, row.person_number)
    conflicts = await gverify_service.approved_elsewhere(db, row.person_number, row.user_id)
    acknowledged = set(acknowledged_conflict_ids or [])
    override = False
    if decision is AdminDecision.approved and conflicts:
        unacknowledged = [c for c in conflicts if c.id not in acknowledged]
        if unacknowledged:
            # Nothing is persisted: the typed ID details written above are
            # thrown away, so a refused approval leaves the attempt untouched.
            listed = [
                c.model_dump(mode="json") for c in await _kyc_conflicts(db, conflicts)
            ]
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ID_CONFLICT_ACK_REQUIRED",
                    "message": (
                        "This ID number is already verified on other accounts. "
                        "Review them, then acknowledge them and add a note to approve."
                    ),
                    "conflicts": listed,
                },
            )
        # Every conflict is acknowledged. The schema guarantees a substantive
        # note comes with an acknowledgement; re-check here for other callers.
        if len(note or "") < OVERRIDE_NOTE_MIN_LENGTH:
            await db.rollback()
            raise HTTPException(
                status_code=400,
                detail=(
                    "Overriding an ID conflict needs a note of at least "
                    f"{OVERRIDE_NOTE_MIN_LENGTH} characters"
                ),
            )
        override = True
    if decision is AdminDecision.approved and not note and provider_checked:
        raise HTTPException(
            status_code=400,
            detail="A note is required to approve a flagged verification",
        )
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
            **(
                {"provider_checked": False, "entered_by_reviewer": {k: v for k, v in entered.items() if v}}
                if not provider_checked
                else {}
            ),
        },
        ip_address=ip_address,
    )
    if override:
        append_audit(
            db,
            entity_type="GVERIFY_KYC_VERIFICATION",
            entity_id=row.id,
            action="KYC_ID_CONFLICT_OVERRIDDEN",
            actor_id=actor.id,
            before_state=before,
            after_state={
                "status": row.status,
                "note": note,
                "user_id": str(row.user_id),
                "person_number": row.person_number,
                "acknowledged_verification_ids": sorted(str(c.id) for c in conflicts),
                "conflicting_user_ids": sorted({str(c.user_id) for c in conflicts}),
                "provider_checked": provider_checked,
            },
            ip_address=ip_address,
        )
    await db.commit()
    await db.refresh(row)
    return await _kyc_verification_out(db, row)



# --------------------------------------------------------------------------- #
# Bank account review
#
# A user's saved bank account (payment_accounts) is what an SME is paid into
# and an investor is repaid to. It starts PENDING; only an APPROVED account
# lists the SME's project or lets the investor invest. Each decision is a
# recorded event in the audit log: who decided, what, why, and on which
# account. Decisions are final (decide_payment_account refuses a second);
# a user whose account was rejected saves a corrected one.
# --------------------------------------------------------------------------- #
PAYMENT_ACCOUNT_REVIEW_STATUSES = ("PENDING", "APPROVED", "REJECTED")


async def _admin_payment_accounts(db: AsyncSession, accounts: list) -> list[AdminPaymentAccountOut]:
    """The admin view of each account: its owner and the member of staff who decided it, in two queries."""
    if not accounts:
        return []
    await annotate_name_match(db, accounts)  # name_match / expected_name: the KYC/KYB name to check the bank's name against
    user_ids = {a.user_id for a in accounts} | {a.approved_by for a in accounts if a.approved_by}
    users = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
    }
    out = []
    for account in accounts:
        owner = users.get(account.user_id)
        decider = users.get(account.approved_by) if account.approved_by else None
        out.append(
            AdminPaymentAccountOut(
                **PaymentAccountOut.model_validate(account).model_dump(),
                user_id=account.user_id,
                user_email=owner.email if owner else "",
                user_full_name=owner.full_name if owner else None,
                approved_by_email=decider.email if decider else None,
            )
        )
    return out


async def list_payment_accounts_for_review(db: AsyncSession, status_filter: str) -> list[AdminPaymentAccountOut]:
    """Saved bank accounts for the review queue, oldest first so the longest-waiting comes up first.
    ``status_filter`` is one of PAYMENT_ACCOUNT_REVIEW_STATUSES or ALL."""
    stmt = select(PaymentAccount).order_by(PaymentAccount.created_at, PaymentAccount.id)
    if status_filter != "ALL":
        stmt = stmt.where(PaymentAccount.status == status_filter)
    accounts = (await db.execute(stmt)).scalars().all()
    return await _admin_payment_accounts(db, list(accounts))


async def decide_payment_account_review(
    db: AsyncSession,
    account_id: UUID,
    *,
    decision: str,
    note: str | None,
    actor: User,
    ip_address: str | None,
    verified_holder_name: str | None = None,
    accept_difference: bool = False,
) -> AdminPaymentAccountOut:
    note = (note or "").strip() or None
    if not note:
        # Required either way (D-031): a rejection's note is the user's reason, an approval's records what was checked.
        raise HTTPException(
            status_code=422,
            detail="Give the reason for this decision" + (" (what you checked)" if decision == "APPROVED" else ""),
            headers={"X-Error-Code": "REASON_REQUIRED"},
        )
    account = await db.get(PaymentAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    if account.user_id == actor.id:
        raise HTTPException(status_code=403, detail="You cannot review your own bank account")
    before = {"status": account.status}
    now = datetime.now(timezone.utc)
    try:
        account = await decide_payment_account(
            db,
            account_id=account_id,
            decision=decision,
            reason=note,
            staff_id=actor.id,
            now=now,
            verified_holder_name=verified_holder_name,
            accept_difference=accept_difference,
        )
    except DirectTransferError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code}
        ) from exc
    append_audit(
        db,
        entity_type="PAYMENT_ACCOUNT",
        entity_id=account.id,
        action=f"PAYMENT_ACCOUNT_{decision}",
        actor_id=actor.id,
        before_state=before,
        after_state={
            "status": account.status,
            "note": note,
            "user_id": str(account.user_id),
            "holder_role": account.holder_role,
            "bank_name": account.bank_name,
            "account_last4": account.account_number[-4:],
        },
        ip_address=ip_address,
    )
    if account.status == "APPROVED" and account.holder_role == "SME":
        # The business can now be paid, so its live projects open for investment (best effort: the approval
        # stands even when a project cannot open yet; the project panel says why).
        from app.market.funding import publish_owner_projects

        await publish_owner_projects(
            db, user_id=account.user_id, now=now, actor_id=actor.id, trigger="PAYMENT_ACCOUNT_APPROVED"
        )
    await db.commit()
    return (await _admin_payment_accounts(db, [account]))[0]
