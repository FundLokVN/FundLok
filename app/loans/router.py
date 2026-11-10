from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.loans.schemas import (
    LoanApplicationCreate,
    LoanApplicationFiguresIn,
    LoanApplicationFiguresOut,
    ListingDocumentChecksOut,
    ListingDocumentsOut,
    LoanApplicationOut,
    PublicRateEstimateIn,
    PublicRateEstimateOut,
    SignedListingDocumentOut,
    SignerCheckOut,
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


@router.get(
    "/applications/{application_id}/service-agreement",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_service_agreement(
    application_id: UUID,
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The SME Service Agreement (Contract 2) as a PDF, filled in with this
    SME's details and the application's terms (the approved rate once FundLok
    has assessed it), for the SME to review, sign with its enterprise digital
    signature and upload back with the Standing Offer
    (POST .../listing-documents/service-agreement). Only the SME that owns the
    application. Generated each time, never stored: the signed upload is the
    record."""
    from app.loans.agreement.service import build_pdf

    pdf, filename = await build_pdf(db, application_id, current_user)
    return _pdf(pdf, filename, download)


def _pdf(content: bytes, filename: str, download: bool) -> Response:
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"', "Cache-Control": "no-store"},
    )


async def _listing_documents(db: AsyncSession, application, user: User) -> ListingDocumentsOut:
    from app.loans import signing

    state = await signing.status(db, application, user)
    checks = await signing.checks_for(db, list(state.signed.values()))

    def one(kind: signing.Kind) -> SignedListingDocumentOut:
        doc = state.signed.get(kind.document_type)
        if doc is None:
            return SignedListingDocumentOut(signed=False)
        verdict = checks.get(doc.id)
        return SignedListingDocumentOut(
            signed=True,
            signed_at=doc.uploaded_at,
            file_name=doc.original_filename,
            checks=ListingDocumentChecksOut(
                checked_at=verdict.get("checked_at"),
                verified_by=verdict.get("verified_by"),
                terms_match=bool(verdict.get("terms_match")),
                signers=[
                    SignerCheckOut(role=s["role"], name=s.get("name"))
                    for s in verdict.get("signers") or []
                    if s.get("role") in ("SME", "GUARANTOR")
                ],
            )
            if verdict and verdict.get("status") == "VERIFIED"
            else None,
        )

    return ListingDocumentsOut(
        ready=state.ready,
        published=state.published,
        guarantor_verified=state.guarantor_verified,
        service_agreement=one(signing.SERVICE_AGREEMENT),
        standing_offer=one(signing.STANDING_OFFER),
    )


@router.get("/applications/{application_id}/listing-documents", response_model=ListingDocumentsOut)
async def get_listing_documents(
    application_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Where signing the two listing documents stands (docs/specs/loans/listing-signing.md). Owner only."""
    from app.loans.agreement.service import owned_application

    application = await owned_application(db, application_id, current_user)
    return await _listing_documents(db, application, current_user)


@router.get(
    "/applications/{application_id}/standing-offer",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_standing_offer(
    application_id: UUID,
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The Standing Offer to Conclude Loan Contracts (Contract 3) as a PDF, filled in with the SME, its Personal
    Guarantor and the approved terms, for the SME and the Guarantor to sign and upload back. Only once FundLok has
    approved the request (409 SIGNING_NOT_OPEN before that: it has no terms to carry). Generated each time, never
    stored: the signed upload is the record. Owner only."""
    from app.loans.agreement import standing_offer
    from app.loans.agreement.service import owned_application
    from app.loans.agreement.terms import locked_terms

    application = await owned_application(db, application_id, current_user)
    if await locked_terms(db, application) is None:
        raise HTTPException(
            status_code=409,
            detail="The Standing Offer is ready once FundLok approves the request",
            headers={"X-Error-Code": "SIGNING_NOT_OPEN"},
        )
    pdf, filename = await standing_offer.build_pdf(db, application, current_user)
    return _pdf(pdf, filename, download)


@router.post("/applications/{application_id}/listing-documents/{kind}", response_model=ListingDocumentsOut)
async def upload_signed_listing_document(
    application_id: UUID,
    kind: str,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Upload the signed copy of a listing document: `kind` is "service-agreement" or "standing-offer". Checked
    with NEAC before it counts (app/loans/signing.record_signed lists the refusals); the second of the two
    publishes the project. Returns where signing stands afterwards. Owner only."""
    from app.loans import signing
    from app.loans.agreement.service import owned_application

    signing_kind = signing.kind_for(kind)
    application = await owned_application(db, application_id, current_user)
    content = await file.read(signing.MAX_BYTES + 1)
    await signing.record_signed(
        db,
        application=application,
        user=current_user,
        kind=signing_kind,
        content=content,
        file_name=file.filename,
        now=datetime.now(timezone.utc),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return await _listing_documents(db, application, current_user)


@router.get(
    "/applications/{application_id}/listing-documents/{kind}/signed",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_signed_listing_document(
    application_id: UUID,
    kind: str,
    download: bool = Query(False, description="true: as an attachment, to save"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """The signed copy on file, exactly as uploaded. 404 when there is none. Owner only."""
    from app.loans import signing
    from app.loans.agreement.service import owned_application

    signing_kind = signing.kind_for(kind)
    application = await owned_application(db, application_id, current_user)
    content, filename = await signing.signed_copy(db, application, signing_kind)
    return _pdf(content, filename, download)


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
