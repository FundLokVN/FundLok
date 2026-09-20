from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.lending.models import Contract, LoanApplication, ScoreRun
from app.contracts.schemas import ContractCreate


async def create_contract(db: AsyncSession, body: ContractCreate) -> Contract:
    result = await db.execute(select(ScoreRun).where(ScoreRun.id == body.score_run_id))
    sr = result.scalar_one_or_none()
    if not sr or sr.status != "LOCKED":
        raise HTTPException(
            status_code=400,
            detail="Score run must exist and be LOCKED",
        )
    result = await db.execute(select(LoanApplication).where(LoanApplication.id == body.application_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if sr.application_id != app.id:
        raise HTTPException(status_code=400, detail="Score run does not match application")
    # The operator half of the two-approval gate. A LOCKED score run is the
    # ENGINE saying the numbers work; this is a human saying the request should
    # proceed. Both are required, and PENDING is not a yes — an unreviewed
    # application must not be able to turn itself into a facility.
    if app.admin_approval != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail=(
                "Application requires admin approval before a contract can be "
                f"created (currently {app.admin_approval})"
            ),
        )
    terms = dict(sr.recommended_terms or {})
    if body.template_version:
        terms["template_version"] = body.template_version
    c = Contract(
        application_id=app.id,
        score_run_id=sr.id,
        status="ACTIVE_PENDING_FUNDING",
        final_terms=terms,
        target_amount=app.requested_amount,
        funded_amount=0,
    )
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return c
