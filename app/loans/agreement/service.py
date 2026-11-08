"""The SME Service Agreement for one loan application: filled from what the
platform already knows about the SME, rendered as the PDF the SME signs.

Where each blank comes from:
  name, enterprise code, address, legal representative
      the SME's approved business verification (KYB) when there is one, as the
      state registry confirmed it; else the project's own name and tax id
  registered bank account
      the SME's approved payment account (Settings > bank account)
  Annex amount and term
      the application's requested amount and duration
  Annex rate
      not known until FundLok's assessment, and printed as such

The signed copy comes back as an upload of document_type
SERVICE_AGREEMENT_DOCUMENT_TYPE and is required to submit (loans.service).
"""
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.accounts import list_payment_accounts
from app.direct_transfer.calendar_utils import ict_date
from app.gverify import kyb_service
from app.lending.models import LoanApplication, Project
from app.loans.agreement import text
from app.loans.agreement.pdf import AgreementData, render
from app.projects.service import user_owns_project
from app.users.models import Role, User

# The upload type the signed agreement comes back as (app/uploads/schemas.py).
SERVICE_AGREEMENT_DOCUMENT_TYPE = "sme_service_agreement"


def _representative(reps) -> str | None:
    """'Name (position)' of the first legal representative on the certificate."""
    if not isinstance(reps, list) or not reps or not isinstance(reps[0], dict):
        return None
    first = reps[0]
    name = (first.get("name") or "").strip()
    if not name:
        return None
    position = (first.get("position") or first.get("title") or "").strip()
    return f"{name} ({position})" if position else name


async def agreement_data(db: AsyncSession, application: LoanApplication, user: User) -> AgreementData:
    project = await db.get(Project, application.project_id)
    kyb = await kyb_service.latest_for_user(db, user.id)
    verified = kyb if kyb is not None and kyb.is_approved else None

    accounts = [
        a
        for a in await list_payment_accounts(db, user_id=user.id)
        if a.holder_role == Role.SME.value and a.status == "APPROVED"
    ]
    account = accounts[0] if accounts else None

    return AgreementData(
        application_ref=str(application.id).split("-")[0].upper(),
        prepared_on=ict_date(datetime.now(timezone.utc)),
        sme_name=(verified.business_name if verified else None) or (project.legal_name if project else None),
        enterprise_code=(verified.tax_code if verified else None) or (project.tax_id if project else None),
        address=verified.company_address if verified else None,
        legal_representative=_representative(verified.representatives) if verified else None,
        bank_account=f"{account.account_number} ({account.account_name})" if account else None,
        bank_name=account.bank_name if account else None,
        amount_vnd=int(application.requested_amount) if application.requested_amount is not None else None,
        term_months=application.duration_months,
    )


async def build_pdf(db: AsyncSession, application_id, user: User) -> tuple[bytes, str]:
    """(pdf bytes, file name) for the SME who owns the application."""
    if user.role != Role.SME.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only SMEs have a service agreement")
    application = (
        await db.execute(select(LoanApplication).where(LoanApplication.id == application_id))
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if not await user_owns_project(db, user.id, application.project_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    data = await agreement_data(db, application, user)
    name_part = (data.enterprise_code or data.application_ref).replace("/", "-")
    return render(data), f"FundLok-hop-dong-dich-vu-{name_part}-{text.VERSION}.pdf"
