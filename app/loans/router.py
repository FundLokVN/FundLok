from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.loans.schemas import (
    LoanApplicationCreate,
    LoanApplicationFiguresIn,
    LoanApplicationFiguresOut,
    LoanApplicationOut,
    PublicRateEstimateIn,
    PublicRateEstimateOut,
)
from app.loans.service import (
    create_application,
    public_rate_estimate,
    save_figures,
    submit_application,
)
from uuid import UUID

from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.rbac import require_roles

router = APIRouter(prefix="/loans", tags=["loans"])

require_sme = require_roles(Role.SME)


@router.post("/applications", response_model=LoanApplicationOut, status_code=201)
async def create_loan_application(
    body: LoanApplicationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    row = await create_application(db, body, current_user)
    append_audit(
        db,
        entity_type="LOAN_APPLICATION",
        entity_id=row.id,
        action="CREATE",
        actor_id=current_user.id,
        after_state={"project_id": str(row.project_id), "status": row.status},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return row


@router.put(
    "/applications/{application_id}/figures",
    response_model=LoanApplicationFiguresOut,
)
async def save_loan_application_figures(
    application_id: UUID,
    body: LoanApplicationFiguresIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Store the figures the applicant typed on the revenue and costs steps.

    PUT rather than PATCH: the wizard owns the whole set and replaces it each
    time, so the request is idempotent and a retry after a dropped connection
    cannot half-apply.
    """
    row = await save_figures(db, application_id, body, current_user)

    # The figures themselves stay out of the audit trail -- they are the
    # applicant's commercial data, and the row already holds them. What matters
    # for an audit is that they changed, by whom, and when.
    append_audit(
        db,
        entity_type="LOAN_APPLICATION",
        entity_id=row.id,
        action="SAVE_FIGURES",
        actor_id=current_user.id,
        after_state={"figures_updated_at": row.figures_updated_at.isoformat()},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return LoanApplicationFiguresOut(
        **row.self_reported_figures,
        figures_updated_at=row.figures_updated_at,
    )


@router.post("/applications/{application_id}/submit", response_model=LoanApplicationOut)
async def submit_loan_application(
    application_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    row = await submit_application(db, application_id, current_user)
    append_audit(
        db,
        entity_type="LOAN_APPLICATION",
        entity_id=row.id,
        action="SUBMIT",
        actor_id=current_user.id,
        after_state={"status": row.status},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return row


@router.post(
    "/rate-estimate",
    response_model=PublicRateEstimateOut,
    # 200, not 201: nothing is created. POST rather than GET only because the
    # answers are a body, and a business's revenue has no place in a URL, a
    # proxy log or a browser history entry.
    status_code=200,
)
async def post_public_rate_estimate(body: PublicRateEstimateIn, db: AsyncSession = Depends(get_db)):
    """Indicative rate for a visitor with no account (landing page calculator).

    Deliberately unauthenticated and deliberately stateless -- it reads nothing
    and writes nothing, so being open costs no data and creates no rows.

    The engine runs here rather than in the browser because the reference rate
    and the rating formula are internal pricing inputs. The response carries a
    band, a score band and the assumptions behind them; nothing it contains can
    reconstruct the pricing.
    """
    return await public_rate_estimate(db, body)
