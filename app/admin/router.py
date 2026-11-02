from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import funding as admin_funding
from app.admin import repayments as admin_repayments
from app.admin import service
from app.admin.access_log import audit_admin_read
from app.admin.schemas import (
    AdminFundingActivityOut,
    AdminFundingRoundOut,
    AdminInvestorTransferOut,
    AdminPaymentAccountDecisionRequest,
    AdminPaymentAccountOut,
    AdminReceiptDecisionRequest,
    AdminReceiptFieldsRequest,
    AdminReceiptUrlResponse,
    AdminRepaymentDecisionRequest,
    AdminRepaymentPreviewOut,
    AdminRepaymentReceiptOut,
    AdminTransferDecisionRequest,
    AdminDecisionRequest,
    AdminDocumentUrlResponse,
    AdminInviteRequest,
    AdminKybCertificateUrlResponse,
    AdminKybResolveRequest,
    AdminKybReviewOut,
    AdminKybVerificationOut,
    AdminKycImageUrlResponse,
    AdminKycResolveRequest,
    AdminKycVerificationOut,
    AdminLoanApplicationRow,
    AdminMode,
    AdminOverview,
    AdminProjectDetail,
    AdminUserDetail,
    AdminUserFullDetail,
    AdminUserStatusRequest,
    AuditLogOut,
    Page,
)
from app.core.database import get_db
from app.direct_transfer.schemas import FundingWindowOut
from app.loans import grading_inputs
from app.loans.schemas import GradingInputsOut
from app.users.models import Role, User
from app.utils.rbac import require_roles

router = APIRouter(prefix="/admin", tags=["admin"])

require_admin = require_roles(Role.ADMIN, Role.SYSTEM_ADMIN)
# Acting on admin-level accounts (creating one) is SYSTEM_ADMIN only.
require_system_admin = require_roles(Role.SYSTEM_ADMIN)


@router.get(
    "/overview",
    response_model=AdminOverview,
    dependencies=[Depends(audit_admin_read("ADMIN_OVERVIEW"))],
)
async def get_overview(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    mode: AdminMode = Query(default=AdminMode.users),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=14, ge=1, le=100),
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    role: str | None = Query(default=None, description="users mode only"),
    industry: str | None = Query(default=None, description="projects mode only"),
):
    return await service.get_overview(
        db,
        mode=mode,
        page=page,
        page_size=page_size,
        search=search,
        status=status,
        role=role,
        industry=industry,
    )


@router.get(
    "/projects/{project_id}",
    response_model=AdminProjectDetail,
    dependencies=[Depends(audit_admin_read("PROJECT", id_param="project_id"))],
)
async def get_project_detail(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The admin project preview: one company plus every funding request it has
    made. Admin-only, like every route on this router — SMEs read their own
    applications through /loans, never through here."""
    return await service.get_project_detail(db, project_id)


@router.post("/projects/{project_id}/funding-window", response_model=FundingWindowOut)
async def open_project_for_funding(
    project_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Open a live project for investment. It normally opens on its own once the project is live and its SME's
    bank account is approved; this is for one that did not (the error says why it still cannot)."""
    return await service.open_project_for_funding(
        db,
        project_id,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.post(
    "/kyb-verifications/{verification_id}/resolve",
    response_model=AdminKybVerificationOut,
)
async def resolve_kyb_verification(
    verification_id: UUID,
    body: AdminKybResolveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Settle a KYB attempt parked in MANUAL_REVIEW. Admin-only. Approving a
    manual-mode attempt needs the business name and tax code from the
    certificate."""
    return await service.resolve_kyb_verification(
        db,
        verification_id,
        decision=body.decision,
        note=body.note,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
        business_name=body.business_name,
        tax_code=body.tax_code,
        representative_id_numbers=body.representative_id_numbers,
    )


# ALL = in review, approved and rejected together (service.REVIEW_LIST_STATUSES).
ReviewListStatus = Literal["ALL", "MANUAL_REVIEW", "APPROVED", "REJECTED", "FAILED"]


@router.get(
    "/kyb-verifications",
    response_model=Page[AdminKybReviewOut],
    dependencies=[Depends(audit_admin_read("GVERIFY_KYB_VERIFICATION"))],
)
async def list_kyb_verifications(
    status: ReviewListStatus = "MANUAL_REVIEW",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """KYB attempts, a page at a time: those waiting for a reviewer first and
    oldest first, decided ones after, newest first. Defaults to the review
    queue. Admin-only."""
    return await service.list_kyb_verifications(db, status=status, page=page, page_size=page_size)


@router.get(
    "/kyb-verifications/{verification_id}",
    response_model=AdminKybReviewOut,
    dependencies=[Depends(audit_admin_read("GVERIFY_KYB_VERIFICATION", id_param="verification_id"))],
)
async def get_kyb_verification(
    verification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """One KYB attempt with the SME's own identity check beside it. Admin-only."""
    return await service.get_kyb_verification(db, verification_id)


@router.get(
    "/kyb-verifications/{verification_id}/certificate",
    response_model=AdminKybCertificateUrlResponse,
    dependencies=[Depends(audit_admin_read("GVERIFY_KYB_VERIFICATION", id_param="verification_id"))],
)
async def get_kyb_certificate_url(
    verification_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """A 10-minute read URL for the submitted certificate. Admin-only."""
    return await service.get_kyb_certificate_url(
        db,
        verification_id,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.get(
    "/kyc-verifications",
    response_model=Page[AdminKycVerificationOut],
    dependencies=[Depends(audit_admin_read("GVERIFY_KYC_VERIFICATION"))],
)
async def list_kyc_verifications(
    status: ReviewListStatus = "MANUAL_REVIEW",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """KYC attempts, a page at a time, in the same order as the KYB list.
    Defaults to the review queue. Admin-only."""
    return await service.list_kyc_verifications(db, status=status, page=page, page_size=page_size)


@router.get(
    "/kyc-verifications/{verification_id}",
    response_model=AdminKycVerificationOut,
    dependencies=[Depends(audit_admin_read("GVERIFY_KYC_VERIFICATION", id_param="verification_id"))],
)
async def get_kyc_verification(
    verification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """One KYC attempt with the accounts that share its ID number. Admin-only."""
    return await service.get_kyc_verification(db, verification_id)


@router.get(
    "/kyc-verifications/{verification_id}/images/{name}",
    response_model=AdminKycImageUrlResponse,
    dependencies=[Depends(audit_admin_read("GVERIFY_KYC_VERIFICATION", id_param="verification_id"))],
)
async def get_kyc_image_url(
    verification_id: UUID,
    name: Literal["id_front", "id_back", "portrait"],
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """A 10-minute read URL for one submitted image. Admin-only."""
    return await service.get_kyc_image_url(
        db,
        verification_id,
        name,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.post(
    "/kyc-verifications/{verification_id}/resolve",
    response_model=AdminKycVerificationOut,
)
async def resolve_kyc_verification(
    verification_id: UUID,
    body: AdminKycResolveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Settle a KYC attempt parked in MANUAL_REVIEW. Admin-only. Approving a
    manual-mode attempt requires the ID number and name from the card. If that
    ID is already verified on other accounts the approval is refused (409,
    listing them) until the reviewer acknowledges them and adds a note."""
    return await service.resolve_kyc_verification(
        db,
        verification_id,
        decision=body.decision,
        note=body.note,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
        person_number=body.person_number,
        full_name=body.full_name,
        date_of_birth=body.date_of_birth,
        acknowledged_conflict_ids=body.acknowledged_conflict_ids,
    )


@router.post(
    "/applications/{application_id}/decision",
    response_model=AdminLoanApplicationRow,
)
async def decide_application(
    application_id: UUID,
    body: AdminDecisionRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Record the operator half of the two-approval gate. Admin-only.

    An application needs BOTH this and an approved verification before
    create_contract will let it become a facility.
    """
    return await service.decide_application(
        db,
        application_id,
        decision=body.decision,
        note=body.note,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
        background_tasks=background_tasks,
    )


@router.get(
    "/documents/{document_id}/url",
    response_model=AdminDocumentUrlResponse,
    dependencies=[Depends(audit_admin_read("LOAN_APPLICATION_DOCUMENT", id_param="document_id"))],
)
async def get_document_url(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """A 10-minute read URL for an uploaded application document. Admin-only."""
    return await service.get_document_url(db, document_id)


@router.get(
    "/users/{user_id}",
    response_model=AdminUserFullDetail,
    dependencies=[Depends(audit_admin_read("USER", id_param="user_id"))],
)
async def get_user_detail(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The admin user preview: one account's full profile plus the projects it
    owns, its investor activity and its security posture. Admin-only and
    read-only — changing what the account may do lives on the status route."""
    return await service.get_user_detail(db, user_id)


@router.get(
    "/payment-accounts",
    response_model=list[AdminPaymentAccountOut],
    dependencies=[Depends(audit_admin_read("PAYMENT_ACCOUNT"))],
)
async def list_payment_accounts(
    status: Literal["PENDING", "APPROVED", "REJECTED", "ALL"] = "PENDING",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The bank-account review queue: saved accounts with their owner, in full,
    oldest first. Defaults to those waiting for a decision. Reads are logged
    (full account numbers)."""
    return await service.list_payment_accounts_for_review(db, status)


@router.post("/payment-accounts/{account_id}/decision", response_model=AdminPaymentAccountOut)
async def decide_payment_account(
    account_id: UUID,
    body: AdminPaymentAccountDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Approve or reject a saved bank account. Only an approved account lists an
    SME's project or lets an investor invest. Rejecting needs a note, which the
    user sees. The decision is final and lands in the audit log with the admin
    who made it."""
    return await service.decide_payment_account_review(
        db,
        account_id,
        decision=body.decision.value,
        note=body.note,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.get(
    "/funding/rounds",
    response_model=list[AdminFundingRoundOut],
    dependencies=[Depends(audit_admin_read("FUNDING_WINDOW"))],
)
async def list_funding_rounds(
    status: Literal["OPEN", "ALL"] = "OPEN",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The Transactions tab's rounds: by default the ones taking or settling investment (OPEN, FULL, CLOSING),
    closing soonest first, with their totals and how many tickets wait on staff."""
    return await admin_funding.list_rounds(db, open_only=status == "OPEN")


@router.post("/funding/rounds/{window_id}/advance", response_model=AdminFundingRoundOut)
async def advance_funding_round(
    window_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Do what is due on a round now: start its close when it is due, and once nothing is pending, form the loan
    and create the daily repayment days. Stands in for the scheduled job, which is not deployed. Audited."""
    return await admin_funding.advance_round(
        db, window_id, actor=current_user, ip_address=request.client.host if request.client else None
    )


@router.get(
    "/funding/transfers",
    response_model=Page[AdminInvestorTransferOut],
    dependencies=[Depends(audit_admin_read("TRANCHE"))],
)
async def list_investor_transfers(
    status: Literal["ALL", "NEEDS_ACTION", "AWAITING_TRANSFER", "RECEIVED", "ENDED"] = "ALL",
    window_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Investor tickets and the transfers reported for them, those waiting on staff first. Reads are logged
    (investor names and the business's account)."""
    return await admin_funding.list_transfers(
        db, status=status, window_id=window_id, page=page, page_size=page_size
    )


@router.get(
    "/funding/activity",
    response_model=Page[AdminFundingActivityOut],
    dependencies=[Depends(audit_admin_read("AUDIT_LOG"))],
)
async def list_funding_activity(
    group: Literal["DECISIONS", "INVESTOR", "BUSINESS", "VIEWS", "ALL"] = "DECISIONS",
    tranche_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The Transactions tab's audit trail, newest first. DECISIONS (the default): receipts checked, rejected, and
    reviews settled as received or not, investors' and businesses' alike. INVESTOR: reservations and uploads.
    BUSINESS: repayments marked paid. VIEWS: who opened which receipt.
    ``tranche_id`` narrows it to one ticket (the review panel's history)."""
    return await admin_funding.list_activity(db, group=group, tranche_id=tranche_id, page=page, page_size=page_size)


@router.get(
    "/funding/transfers/{tranche_id}",
    response_model=AdminInvestorTransferOut,
    dependencies=[Depends(audit_admin_read("TRANCHE", id_param="tranche_id"))],
)
async def get_investor_transfer(
    tranche_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """One investor ticket for the review panel: the transfer reported for it, what a matching receipt shows,
    and any open review."""
    return await admin_funding.get_transfer(db, tranche_id)


@router.get(
    "/funding/transfers/{tranche_id}/repayment",
    response_model=AdminRepaymentPreviewOut,
    dependencies=[Depends(audit_admin_read("TRANCHE", id_param="tranche_id"))],
)
async def get_investment_repayment(
    tranche_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The repayment an approval starts, worked out step by step for the review panel: the days if approved today
    (repayment starts the next working day), or the days it got once repaying."""
    return await admin_funding.repayment_preview(db, tranche_id)


@router.get(
    "/funding/transfers/{transfer_id}/receipt-url",
    response_model=AdminReceiptUrlResponse,
    dependencies=[Depends(audit_admin_read("TRANSFER", id_param="transfer_id"))],
)
async def get_transfer_receipt_url(
    transfer_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """A 10-minute link to the receipt the investor uploaded. Each view is audited."""
    return await admin_funding.get_receipt_url(
        db, transfer_id, actor=current_user, ip_address=request.client.host if request.client else None
    )


@router.post("/funding/transfers/{transfer_id}/fields", response_model=AdminInvestorTransferOut)
async def enter_transfer_receipt_fields(
    transfer_id: UUID,
    body: AdminReceiptFieldsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Record what the receipt shows. If it all matches the QR FundLok issued, the ticket is attested and the
    business has two working days to dispute; otherwise it goes to review. Once per transfer."""
    return await admin_funding.enter_receipt_fields(
        db, transfer_id, body, actor=current_user, ip_address=request.client.host if request.client else None
    )


@router.post("/funding/transfers/{transfer_id}/decision", response_model=AdminInvestorTransferOut)
async def decide_investor_receipt(
    transfer_id: UUID,
    body: AdminReceiptDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Approve or deny a transfer after looking at the investor's receipt; nothing is typed in or compared.
    Approving a receipt records the investment as paid (ATTESTED: the business has two working days to say the
    money never arrived) and rings the business's bell; denying it rejects the receipt, and the investor can
    upload the right one while the hold lasts. A ticket under review is settled as received or not. Final, and
    audited with the admin who made it."""
    return await admin_funding.decide_receipt(
        db,
        transfer_id,
        outcome=body.outcome,
        reason=body.reason,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.post("/funding/reviews/{dispute_id}/decision", response_model=AdminInvestorTransferOut)
async def decide_investor_transfer(
    dispute_id: UUID,
    body: AdminTransferDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Settle a ticket under review: RECEIVED (it counts toward the round) or NOT_RECEIVED (void, its place
    freed). Needs a reason; final, and audited with the admin who made it."""
    return await admin_funding.decide_transfer(
        db,
        dispute_id,
        outcome=body.outcome,
        reason=body.reason,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.get(
    "/funding/repayments",
    response_model=Page[AdminRepaymentReceiptOut],
    dependencies=[Depends(audit_admin_read("TRANSFER"))],
)
async def list_repayment_receipts(
    status: Literal["NEEDS_ACTION", "DECIDED", "ALL"] = "NEEDS_ACTION",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """The receipts businesses uploaded for repaying their investors, those waiting on staff first. Reads are
    logged (both parties' names and accounts)."""
    return await admin_repayments.list_receipts(db, status=status, page=page, page_size=page_size)


@router.get(
    "/funding/repayments/{receipt_id}",
    response_model=AdminRepaymentReceiptOut,
    dependencies=[Depends(audit_admin_read("EVIDENCE_FILE", id_param="receipt_id"))],
)
async def get_repayment_receipt(
    receipt_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """One repayment receipt for the review panel: the days it pays and the accounts the money moved between."""
    return await admin_repayments.get_receipt(db, receipt_id)


@router.get(
    "/funding/repayments/{receipt_id}/receipt-url",
    response_model=AdminReceiptUrlResponse,
    dependencies=[Depends(audit_admin_read("EVIDENCE_FILE", id_param="receipt_id"))],
)
async def get_repayment_receipt_url(
    receipt_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """A 10-minute link to the receipt the business uploaded. Each view is audited."""
    return await admin_repayments.get_receipt_url(
        db, receipt_id, actor=current_user, ip_address=request.client.host if request.client else None
    )


@router.post("/funding/repayments/{receipt_id}/decision", response_model=AdminRepaymentReceiptOut)
async def decide_repayment_receipt(
    receipt_id: UUID,
    body: AdminRepaymentDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Approve or deny a business's repayment receipt after looking at it. Approving records the investor as paid
    for each day it covers (matched and settled); denying, with a note, puts those days back to due and tells the
    business why, so it pays and uploads again. Final, and audited with the admin who made it."""
    return await admin_repayments.decide_receipt(
        db,
        receipt_id,
        outcome=body.outcome,
        reason=body.reason,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.patch("/users/{user_id}/status", response_model=AdminUserDetail)
async def set_user_status(
    user_id: UUID,
    body: AdminUserStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Change an account's status. Admin-only.

    SUSPENDED genuinely denies: existing sessions stop working and no new one
    can be minted. Two guards apply — nobody may change their own status, and
    only a SYSTEM_ADMIN may act on an admin-level account.
    """
    return await service.set_user_status(
        db,
        user_id,
        new_status=body.status,
        note=body.note,
        actor=current_user,
        ip_address=request.client.host if request.client else None,
    )


@router.post("/users/admins", response_model=AdminUserDetail, status_code=201)
async def invite_admin(
    body: AdminInviteRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_system_admin),
):
    """Create an ADMIN account and email the invitee a set-password link.

    SYSTEM_ADMIN only, matching set_user_status: only a system admin may act
    on admin-level accounts, and granting admin access is the most
    consequential such act. Always creates ADMIN, never SYSTEM_ADMIN. 409 if
    the email already has an account (no silent promotion of an existing
    member to admin).
    """
    return await service.invite_admin(
        db,
        email=body.email,
        full_name=body.full_name,
        actor=current_user,
        background_tasks=background_tasks,
        ip_address=request.client.host if request.client else None,
    )


@router.get(
    "/applications/{application_id}/grading-inputs",
    response_model=GradingInputsOut,
    dependencies=[Depends(audit_admin_read("LOAN_APPLICATION", id_param="application_id"))],
)
async def get_grading_inputs(
    application_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Every grading-engine input for an application: its value, where it came
    from (parsed e-invoices, KYB, the form, typed figures, the sector table)
    and what is still missing. Read-only; see app/loans/grading_inputs.py."""
    return await grading_inputs.assemble(db, application_id)


@router.get(
    "/audit-logs",
    response_model=list[AuditLogOut],
    dependencies=[Depends(audit_admin_read("AUDIT_LOG"))],
)
async def get_audit_logs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    entity_type: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    actor_id: UUID | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    return await service.list_audit_logs(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
    )
