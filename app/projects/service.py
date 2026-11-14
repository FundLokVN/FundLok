from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status

from app.direct_transfer.models import PaymentAccount
from app.lending.models import LoanApplication, Project, ProjectOwnership
from app.projects import borrower
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
    # One borrower, one project in progress, one business (app/projects/borrower.py): a valid tax code, the business
    # the SME verified, not already on FundLok, and no other project of the SME's still going.
    try:
        tax_id = await borrower.check_new_project(db, user_id=current_user.id, tax_id=project_data.tax_id)
    except borrower.BorrowerRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code}) from exc
    try:
        # employee_count is stored; company_size is not — it is derived from
        # the count at grading time, so a change to the bands in the params
        # file cannot leave a stale size on the row.
        new_project = Project(
            **project_data.model_dump(
                exclude={"loan_application", "company_size", "tax_id"}
            ),
            tax_id=tax_id,
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
        # The tax code's business is already on FundLok, however its code was written (uq_projects_tax_business).
        if "tax_id" in err_msg or "tax_business" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This business already has a project on FundLok. Contact support if you believe this is an error",
                headers={"X-Error-Code": borrower.DUPLICATE_TAX_CODE},
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


# Only an account an admin approved counts (POST /admin/payment-accounts/{id}/decision): a PENDING one is
# still waiting, a REJECTED one never will. Accounts saved while there was no review were approved on save.
PAYMENT_ACCOUNT_LISTABLE_STATUSES = ("APPROVED",)


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
    """Each application's listing for the marketplace card, in a few queries
    for the whole list. An application has at most one contract
    (create_contract refuses a second) and a contract at most one listing
    (listings.contract_id is unique), so the mapping is one to one.

    A listing investors join through a funding window (ADR-009) is measured by
    the window, not by the legacy orders it never gets: raised is what staff
    confirmed received (CONFIRMED or ACTIVE tickets), investors are the people
    behind those tickets, and the minimum ticket and closing date are the
    window's.
    """
    from sqlalchemy import func

    from app.direct_transfer.models import FundingWindow, Tranche
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
    listing_ids = [listing.id for _, listing in rows]
    windows = {
        window.listing_id: window
        for window in (
            await db.execute(select(FundingWindow).where(FundingWindow.listing_id.in_(listing_ids)))
        ).scalars()
    }
    confirmed = {
        window_id: (int(raised), int(backers))
        for window_id, raised, backers in (
            await db.execute(
                select(
                    Tranche.window_id,
                    func.coalesce(func.sum(Tranche.amount_vnd), 0),
                    func.count(func.distinct(Tranche.investor_id)),
                )
                .where(
                    Tranche.window_id.in_([w.id for w in windows.values()]),
                    Tranche.status.in_(("CONFIRMED", "ACTIVE")),
                )
                .group_by(Tranche.window_id)
            )
        ).all()
    } if windows else {}

    def listing_out(listing: Listing) -> MarketplaceListingOut:
        window = windows.get(listing.id)
        if window is None:
            return MarketplaceListingOut(
                id=listing.id,
                status=listing.status,
                target_amount=int(listing.target_amount),
                funded_amount=int(listing.funded_amount or 0),
                min_ticket=int(listing.min_ticket) if listing.min_ticket is not None else None,
                investor_count=investors.get(listing.id, 0),
                open_at=listing.open_at,
                close_at=listing.close_at,
            )
        raised, backers = confirmed.get(window.id, (0, 0))
        return MarketplaceListingOut(
            id=listing.id,
            status=listing.status,
            target_amount=int(window.target_vnd),
            funded_amount=raised,
            min_ticket=int(window.min_ticket_vnd),
            investor_count=backers,
            open_at=window.window_start,
            close_at=window.window_end,
        )

    return {application_id: listing_out(listing) for application_id, listing in rows}


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

    from app.uploads.schemas import SIGNED_DOCUMENT_TYPES

    # The documents the SME signs after approval are not evidence about the
    # business: the Service Agreement is between the SME and FundLok (Contract
    # 2 Art. 8), and the signed Standing Offer carries the Guarantor's
    # personal details. Investors are not shown them.
    uploaded = dict.fromkeys(
        d.document_type
        for d in application.documents
        if d.status == "UPLOADED" and d.document_type not in SIGNED_DOCUMENT_TYPES
    )
    return [*REQUIRED_DOCUMENT_TYPES, *(t for t in uploaded if t not in REQUIRED_DOCUMENT_TYPES)]



async def activate_project_if_approved(
    db: AsyncSession, application_id: UUID, *, actor_id: UUID | None = None
) -> bool:
    """Put a DRAFT project on the marketplace once its application has BOTH
    approvals -- the operator's (admin_approval == APPROVED) and the engine's
    (a LOCKED score run) -- AND the SME has signed the three documents those
    approved terms go into: the Service Agreement, the Standing Offer and
    the Loan Contract template
    (app/loans/signing.py; Contract 3: "signing publishes the Listing").
    Returns True when it flipped the project to ACTIVE.

    Called from every place a part of the gate lands (admin
    decide_application, underwriting approve_score_run, and the SME's second
    signed upload), because any of them can come last.

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
    # Approved but not yet signed: the SME is asked to sign (the dashboard's
    # signing step), and its second signed upload calls this again.
    from app.loans.signing import all_signed

    if not await all_signed(db, application_id):
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
    # Live with an approved bank account: open it for investment too (best effort; when the account is approved
    # later, that approval opens it instead). Imported here: app.market imports this module.
    from app.market.funding import publish_if_ready

    await publish_if_ready(
        db, project_id=project.id, now=datetime.now(timezone.utc), actor_id=actor_id, trigger="PROJECT_ACTIVATED"
    )
    return True


async def approval_for(db: AsyncSession, application):
    """The applicant-facing summary of an operator approval, or None.

    Uses the score run the decision rests on: a LOCKED run when one exists,
    otherwise the latest run that produced a grade. Re-runs are refused once
    a request is decided, so that run is final. An approval made without any
    graded run still returns, with the score fields empty.
    """
    from decimal import Decimal

    from app.lending.models import ScoreRun
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
        # INV-2: flat interest on the principal, charged once. The same estimate the Standing Offer prints and the
        # repayment schedule charges (agreement/terms.repayment_estimate: 21 working days a month, the daily
        # instalment floored), so the figures on this card are the ones the SME then signs.
        from app.loans.agreement.terms import repayment_estimate

        try:
            estimate = repayment_estimate(
                int(application.requested_amount),
                Decimal(str(run.interest_rate_pct)).quantize(Decimal("0.001")),
                duration,
            )
        except ValueError:  # a rate the schedule refuses (above the cap): no estimate rather than a wrong one
            estimate = None
        if estimate is not None:
            total, daily = estimate.total_vnd, estimate.daily_vnd

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
