from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from uuid import UUID
from app.core.database import get_db
from app.utils.jwt import get_current_user
from app.projects.schemas import (
    LoanApplicationCreateInline,
    MarketplaceLoanApplicationOut,
    MarketplaceProjectOut,
    ProjectCreate,
    ProjectLoanApplicationOut,
    ProjectOut,
    ProjectWithApplicationOut,
)
from app.projects.service import (
    approval_for,
    bank_rate_in_force,
    create_project,
    get_my_projects,
    display_projects,
    locked_business_scores,
    marketplace_listings,
    repeat_project,
    verified_document_types,
)
from app.users.models import User


router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectWithApplicationOut, status_code=201)
async def create_project_endpoint(
    project: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_project, loan_app = await create_project(db, project, current_user)
    out = ProjectWithApplicationOut.model_validate(new_project)
    if loan_app is not None:
        out.loan_application = ProjectLoanApplicationOut.model_validate(loan_app)
    await db.commit()
    return out


@router.post("/repeat", response_model=ProjectWithApplicationOut, status_code=201)
async def repeat_project_endpoint(
    loan: LoanApplicationCreateInline,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """A business whose last request has ended borrows again: only the amount and term are asked; the company, its
    documents and figures come from its last approved request, and the request is submitted for review (409
    NO_APPROVED_REQUEST when there is none to copy, SME_HAS_OPEN_PROJECT while one is in progress)."""
    project, application = await repeat_project(db, loan, current_user)
    out = ProjectWithApplicationOut.model_validate(project)
    out.loan_application = ProjectLoanApplicationOut.model_validate(application)
    await db.commit()
    return out


@router.get("", response_model=List[ProjectWithApplicationOut])
async def list_my_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    projects = await get_my_projects(db, current_user)
    results = []
    for project in projects:
        out = ProjectWithApplicationOut.model_validate(project)
        if project.loan_applications:
            application = project.loan_applications[0]
            out.loan_application = ProjectLoanApplicationOut.model_validate(application)
            out.loan_application.approval = await approval_for(db, application)
        results.append(out)
    return results


@router.get("/public", response_model=List[MarketplaceProjectOut])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Marketplace listing. Carries the DRAFT loan application alongside the
    company so an investor can see the deal terms on the card itself, rather
    than opening every project to find the asking amount.

    The investor schema, not the applicant's: no operator decision_note, no
    documents (see MarketplaceLoanApplicationOut) — only the types the team
    reviewed."""
    projects = await display_projects(db, current_user)
    application_ids = [p.loan_applications[0].id for p in projects if p.loan_applications]
    scores = await locked_business_scores(db, application_ids)
    listings = await marketplace_listings(db, application_ids)
    bank_rate_pct = await bank_rate_in_force(db)
    results = []
    for project in projects:
        out = MarketplaceProjectOut.model_validate(project)
        if project.loan_applications:
            application = project.loan_applications[0]
            out.loan_application = MarketplaceLoanApplicationOut.model_validate(application)
            out.loan_application.verified_documents = verified_document_types(application)
            out.loan_application.business_score = scores.get(application.id)
            out.loan_application.listing = listings.get(application.id)
            out.loan_application.bank_rate_pct = bank_rate_pct
        results.append(out)
    return results
