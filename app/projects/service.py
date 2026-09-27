from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status

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


async def display_projects(db: AsyncSession, current_user: User):
    if current_user.role != Role.INVESTOR.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only investors can view projects",
        )
    # The investor marketplace card shows the deal terms (asking amount), not
    # just the company, so the application rides along. Eager-loaded for the
    # same reason as get_my_projects: a lazy load during response serialisation
    # raises MissingGreenlet on an AsyncSession.
    result = await db.execute(
        select(Project)
        .where(Project.status == "ACTIVE")
        .options(
            selectinload(Project.loan_applications).selectinload(
                LoanApplication.documents
            )
        )
    )
    return result.scalars().all()



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
