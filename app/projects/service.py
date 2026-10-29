from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status

from app.direct_transfer.models import PaymentAccount
from app.lending.models import LoanApplication, Project, ProjectOwnership
from app.projects.schemas import ProjectCreate
from app.users.models import Role, User


from sqlalchemy.exc import IntegrityError


async def create_project(
    db: AsyncSession, project_data: ProjectCreate, current_user: User
) -> tuple[Project, LoanApplication | None]:
    if current_user.role != Role.SME.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only SMEs can create projects",
        )
    try:
        # employee_count is stored; company_size is not — it is derived from
        # the count at grading time, so a change to the bands in the params
        # file cannot leave a stale size on the row.
        new_project = Project(
            **project_data.model_dump(
                exclude={"loan_application", "company_size"}
            ),
            status="DRAFT",
        )
        db.add(new_project)
        await db.flush()
        db.add(
            ProjectOwnership(
                project_id=new_project.id,
                user_id=current_user.id,
                role="OWNER",
            )
        )
        await db.flush()
        loan_app = None
        if project_data.loan_application is not None:
            loan_app = LoanApplication(
                project_id=new_project.id,
                requested_amount=project_data.loan_application.requested_amount,
                duration_months=project_data.loan_application.duration_months,
                purpose=project_data.loan_application.purpose,
                repayment_preference=project_data.loan_application.repayment_preference,
                status="DRAFT",
            )
            db.add(loan_app)
            await db.flush()
            # Re-read with `documents` eagerly loaded. ProjectLoanApplicationOut
            # includes that relationship, and letting Pydantic read it unloaded
            # fires a lazy load during model_validate -- which on an
            # AsyncSession raises MissingGreenlet (IO outside the greenlet
            # context), a 500 on every create-with-loan request. Same reason
            # get_my_projects() uses selectinload. Loading it here also picks up
            # the server-side defaults (status, created_at) in one round trip,
            # so no separate refresh() is needed.
            # populate_existing: the row is already in the identity map, so
            # without it SQLAlchemy keeps the Python-side values and the create
            # response echoes the client's precision ("800000000") while
            # GET /projects reports what Postgres stored in Numeric(15,2)
            # ("800000000.00"). Reloading makes both endpoints agree and makes
            # the response describe the row as persisted.
            loan_app = (
                await db.execute(
                    select(LoanApplication)
                    .where(LoanApplication.id == loan_app.id)
                    .options(selectinload(LoanApplication.documents))
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
        await db.refresh(new_project)
        return new_project, loan_app
    except IntegrityError as e:
        await db.rollback()
        err_msg = str(e.orig)
        if "tax_id" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your bussiness tax ID is already associated with another project. Please contact support if you believe this is an error.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database integrity error: check unique fields or constraints."
        )


async def get_my_projects(db: AsyncSession, current_user: User):
    if current_user.role != Role.SME.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only SMEs can view their projects",
        )
    result = await db.execute(
        select(Project)
        .join(ProjectOwnership)
        .where(ProjectOwnership.user_id == current_user.id)
        .options(
            selectinload(Project.loan_applications).selectinload(
                LoanApplication.documents
            )
        )
    )
    return result.scalars().all()


async def user_owns_project(db: AsyncSession, user_id: UUID, project_id: UUID) -> bool:
    result = await db.execute(
        select(ProjectOwnership).where(
            ProjectOwnership.user_id == user_id,
            ProjectOwnership.project_id == project_id,
        )
    )
    return result.scalar_one_or_none() is not None


# A saved account counts until staff reject it. There is no approval queue yet (HANDOFF-04 Epic 3.1);
# once there is, this becomes ("APPROVED",) so only checked accounts make a project visible.
PAYMENT_ACCOUNT_LISTABLE_STATUSES = ("PENDING", "APPROVED")


def owner_has_bank_account():
    """True for a project whose owner has saved a bank account: investors pay the SME directly
    (ADR-009), so a project with nowhere to pay into is not shown to them."""
    return exists().where(
        ProjectOwnership.project_id == Project.id,
        PaymentAccount.user_id == ProjectOwnership.user_id,
        PaymentAccount.status.in_(PAYMENT_ACCOUNT_LISTABLE_STATUSES),
    )


async def display_projects(db: AsyncSession, current_user: User):
    if current_user.role != Role.INVESTOR.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only investors can view projects",
        )
    # The investor marketplace card shows the deal terms (asking amount), not
    # just the company, so the application rides along. Eager-loaded for the
    # same reason as get_my_projects: a lazy load during response serialisation
    # raises MissingGreenlet on an AsyncSession. Documents are loaded only for
    # verified_document_types; the investor schema carries their types alone.
    result = await db.execute(
        select(Project)
        .where(Project.status == "ACTIVE", owner_has_bank_account())
        .options(selectinload(Project.loan_applications).selectinload(LoanApplication.documents))
    )
    return result.scalars().all()


async def locked_business_scores(db: AsyncSession, application_ids: list[UUID]) -> dict[UUID, float]:
    """Each application's business score from its LOCKED score run, in one
    query for the whole marketplace list. At most one run is locked per
    application (approve_score_run refuses a second), and it is the run the
    approval and any contract rest on, so it is the score to show. Rounded to
    2dp like the SME's approval summary (approval_for), so both read the same.
    """
    from app.lending.models import ScoreRun

    if not application_ids:
        return {}
    rows = (
        await db.execute(
            select(ScoreRun.application_id, ScoreRun.final_grade_precise).where(
                ScoreRun.application_id.in_(application_ids),
                ScoreRun.status == "LOCKED",
                ScoreRun.final_grade_precise.is_not(None),
            )
        )
    ).all()
    return {application_id: round(float(grade), 2) for application_id, grade in rows}


async def bank_rate_in_force(db: AsyncSession) -> float | None:
    """bank_rate_config.rate_pct in force today, as the marketplace's target
    return. None rather than a 500 if the seed row was ever removed: the
    listing still loads, and the card shows a dash."""
    from app.underwriting.service import get_current_bank_rate

    try:
        row = await get_current_bank_rate(db)
    except HTTPException:
        return None
    return float(row.rate_pct)


LISTED_STATUSES = ("OPEN", "FUNDED", "CLOSED")


async def marketplace_listings(db: AsyncSession, application_ids: list[UUID]):
    """Each application's listing for the marketplace card, in two queries
    for the whole list. An application has at most one contract
    (create_contract refuses a second) and a contract at most one listing
    (listings.contract_id is unique), so the mapping is one to one.
    """
    from sqlalchemy import func

    from app.lending.models import Contract, Listing, Order
    from app.projects.schemas import MarketplaceListingOut

    if not application_ids:
        return {}
    rows = (
        await db.execute(
            select(Contract.application_id, Listing)
            .join(Listing, Listing.contract_id == Contract.id)
            .where(Contract.application_id.in_(application_ids), Listing.status.in_(LISTED_STATUSES))
        )
    ).all()
    if not rows:
        return {}
    investors = dict(
        (
            await db.execute(
                select(Order.listing_id, func.count(func.distinct(Order.investor_id)))
                .where(Order.listing_id.in_([listing.id for _, listing in rows]), Order.status == "FILLED")
                .group_by(Order.listing_id)
            )
        ).all()
    )
    return {
        application_id: MarketplaceListingOut(
            id=listing.id,
            status=listing.status,
            target_amount=int(listing.target_amount),
            funded_amount=int(listing.funded_amount or 0),
            min_ticket=int(listing.min_ticket) if listing.min_ticket is not None else None,
            investor_count=investors.get(listing.id, 0),
            open_at=listing.open_at,
            close_at=listing.close_at,
        )
        for application_id, listing in rows
    }


def verified_document_types(application: LoanApplication) -> list[str]:
    """The document types the FundLok team reviewed before approving.

    An operator cannot approve while a required type is missing
    (admin decide_application), and uploads are refused once a request leaves
    DRAFT, so an approved request holds exactly what was reviewed. The business
    registration may have come from the verified certificate (KYB) instead of
    an upload; it was reviewed either way. Not approved: nothing to vouch for.
    """
    if application.admin_approval != "APPROVED":
        return []
    from app.admin.service import REQUIRED_DOCUMENT_TYPES

    uploaded = dict.fromkeys(d.document_type for d in application.documents if d.status == "UPLOADED")
    return [*REQUIRED_DOCUMENT_TYPES, *(t for t in uploaded if t not in REQUIRED_DOCUMENT_TYPES)]



async def activate_project_if_approved(
    db: AsyncSession, application_id: UUID, *, actor_id: UUID | None = None
) -> bool:
    """Put a DRAFT project on the marketplace once its application has BOTH
    approvals: the operator's (admin_approval == APPROVED) and the engine's
    (a LOCKED score run). Returns True when it flipped the project to ACTIVE.

    Called from both places a half of the gate lands (admin decide_application
    and underwriting approve_score_run), because either can come second.

    Before this, nothing ever set a project ACTIVE: creation leaves it DRAFT,
    ACTIVE was only the column's server default, so a genuinely approved SME
    never reached /projects/public while seeded rows did whatever their state.

    Only DRAFT moves. A SUSPENDED, CLOSED or ARCHIVED project was taken off the
    marketplace on purpose, and an approval must not quietly put it back.
    Staged on the caller's transaction; the caller commits.
    """
    from app.lending.models import ScoreRun
    from app.utils.audit import append_audit

    application = await db.get(LoanApplication, application_id)
    if application is None or application.admin_approval != "APPROVED":
        return False
    locked = (
        await db.execute(
            select(ScoreRun.id)
            .where(ScoreRun.application_id == application_id, ScoreRun.status == "LOCKED")
            .limit(1)
        )
    ).scalar_one_or_none()
    if locked is None:
        return False

    project = (
        await db.execute(
            select(Project).where(Project.id == application.project_id).with_for_update()
        )
    ).scalar_one_or_none()
    if project is None or project.status != "DRAFT":
        return False

    project.status = "ACTIVE"
    db.add(project)
    append_audit(
        db,
        entity_type="PROJECT",
        entity_id=project.id,
        action="PROJECT_ACTIVATED",
        actor_id=actor_id,
        before_state={"status": "DRAFT"},
        after_state={
            "status": "ACTIVE",
            "application_id": str(application_id),
            "score_run_id": str(locked),
        },
    )
    await db.flush()
    return True


async def approval_for(db: AsyncSession, application):
    """The applicant-facing summary of an operator approval, or None.

    Uses the score run the decision rests on: a LOCKED run when one exists,
    otherwise the latest run that produced a grade. Re-runs are refused once
    a request is decided, so that run is final. An approval made without any
    graded run still returns, with the score fields empty.
    """
    from decimal import ROUND_HALF_UP, Decimal

    from app.lending.models import ScoreRun
    from app.projects.engine_constraints import params
    from app.projects.schemas import LoanApprovalOut
    from app.underwriting.models import ScoreRunInput

    if application.admin_approval != "APPROVED":
        return None
    runs = (
        await db.execute(
            select(ScoreRun)
            .where(ScoreRun.application_id == application.id, ScoreRun.final_grade_precise.is_not(None))
            .order_by(ScoreRun.created_at.desc())
        )
    ).scalars().all()
    run = next((r for r in runs if r.status == "LOCKED"), runs[0] if runs else None)

    duration = application.duration_months
    total = daily = None
    if run is not None:
        snapshot = (
            await db.execute(select(ScoreRunInput).where(ScoreRunInput.score_run_id == run.id))
        ).scalar_one_or_none()
        # The term the rate was priced on.
        duration = snapshot.duration_months if snapshot else duration
    if run is not None and run.interest_rate_pct is not None and duration:
        # INV-2: flat interest on the principal, charged once.
        rate = Decimal(str(run.interest_rate_pct)) / Decimal("100")
        total_dec = Decimal(int(application.requested_amount)) * (1 + rate * Decimal(duration) / Decimal(12))
        days = Decimal(str(params().repayment["working_days_per_month"])) * Decimal(duration)
        total = int(total_dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        daily = int((total_dec / days).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return LoanApprovalOut(
        approved_at=application.decided_at,
        business_score=round(float(run.final_grade_precise), 2) if run else None,
        reference_rate_pct=round(float(run.interest_rate_pct), 2) if run and run.interest_rate_pct is not None else None,
        duration_months=duration,
        total_repayment_vnd=total,
        estimated_daily_repayment_vnd=daily,
        engine_version=run.engine_version if run else None,
        params_version=run.params_version if run else None,
    )
