from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.uploads.schemas import (
    DOCUMENT_TYPE_RULES,
    CicPreviewOut,
    EInvoicePreviewOut,
    TaxFilingsPreviewOut,
    FileCommitRequest,
    FileCommitResponse,
    FilePresignRequest,
    FilePresignResponse,
    LoanApplicationDocumentOut,
    UploadConfirmRequest,
    UploadConfirmResponse,
    UploadPresignRequest,
    UploadPresignResponse,
)
from app.uploads.service import (
    commit_file,
    confirm_uploads,
    create_file_presign,
    create_upload_presign,
    preview_cic,
    preview_einvoice,
    preview_tax_filings,
)
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.jwt import get_current_user
from app.utils.rbac import require_roles

router = APIRouter(prefix="/uploads", tags=["uploads"])

require_sme = require_roles(Role.SME)


@router.post("/init-upload", response_model=UploadPresignResponse, status_code=201)
async def presign_upload(
    body: UploadPresignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    doc, upload_url, expires_in = await create_upload_presign(db, body, current_user)
    append_audit(
        db,
        entity_type="LOAN_APPLICATION_DOCUMENT",
        entity_id=doc.id,
        action="UPLOAD_INITIATED",
        actor_id=current_user.id,
        after_state={
            "loan_application_id": str(doc.loan_application_id),
            "document_type": doc.document_type,
            "file_key": doc.file_key,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return UploadPresignResponse(
        document_id=doc.id,
        file_key=doc.file_key,
        upload_url=upload_url,
        expires_in=expires_in,
    )


async def _read_capped_body(request: Request, limit: int, code: str) -> bytes:
    """The raw request body, refusing anything over `limit` while reading —
    the Content-Length header alone is the client's claim, not a guarantee."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="File too large", headers={"X-Error-Code": code})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(status_code=413, detail="File too large", headers={"X-Error-Code": code})
    return bytes(raw)


@router.post("/tax-filings-preview", response_model=TaxFilingsPreviewOut)
async def tax_filings_preview(
    request: Request,
    loan_application_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Read the tax filings as soon as the SME picks them, to prefill step 3.
    Body: the .zip of the filings folder, or the statement .xml, raw."""
    raw = await _read_capped_body(
        request, DOCUMENT_TYPE_RULES["tax_filings"]["max_size"], "TAXFILINGS_ARCHIVE_TOO_LARGE"
    )
    filings = await preview_tax_filings(db, loan_application_id, raw, current_user)
    s, v = filings.statements, filings.vat
    return TaxFilingsPreviewOut(
        fiscal_year=s["fiscal_year"],
        regime=s["regime"],
        signed=s["signed"],
        tax_code=s["tax_code"],
        cogs_y1=s["cogs_y1"],
        owner_withdrawal_pct=s["owner_withdrawal_pct"],
        admin_expense_vnd=s["admin_expense_vnd"],
        selling_expense_vnd=s["selling_expense_vnd"],
        revenue_net_vnd=s["revenue_net_vnd"],
        net_profit_vnd=s["net_profit_vnd"],
        interest_expense_vnd=s["interest_expense_vnd"],
        vat_months=v["months_covered"] if v else 0,
        vat_period=f"{v['period_start']}–{v['period_end']}" if v else None,
        warnings=filings.warnings,
    )


@router.post("/cic-preview", response_model=CicPreviewOut)
async def cic_preview(
    request: Request,
    loan_application_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Read the CIC report as soon as the SME picks it, to show step 4 what
    was found. Body: the PDF, raw (Content-Type: application/pdf)."""
    raw = await _read_capped_body(request, DOCUMENT_TYPE_RULES["cic_report"]["max_size"], "CIC_FILE_TOO_LARGE")
    report = await preview_cic(db, loan_application_id, raw, current_user)
    return CicPreviewOut(
        subject_type=report.subject_type,
        subject_name=report.subject_name,
        score=report.score,
        rank=report.rank,
        rank_band=report.rank_band,
        rank_label=report.rank_label,
        percentile=report.percentile,
        scored_on=report.scored_on,
        queried_on=report.queried_on,
        age_days=report.age_days(date.today()),
        lenders=report.lenders,
        debt_total_vnd_million=report.debt_total_vnd_million,
        debt_attention_vnd_million=report.debt_attention_vnd_million,
        debt_bad_vnd_million=report.debt_bad_vnd_million,
        negative_history=report.negative_history,
        signed=report.signed,
        warnings=report.warnings,
    )


@router.post("/einvoice-preview", response_model=EInvoicePreviewOut)
async def einvoice_preview(
    request: Request,
    loan_application_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    """Read the e-invoice zip as soon as the SME picks it, to prefill step 2.

    The body is the raw .zip (Content-Type: application/zip), not multipart:
    no extra dependency, and nothing is written to storage or the database.
    """
    raw = await _read_capped_body(
        request, DOCUMENT_TYPE_RULES["e_invoice_data"]["max_size"], "EINVOICE_ARCHIVE_TOO_LARGE"
    )
    features = await preview_einvoice(db, loan_application_id, raw, current_user)
    months = features.monthly_revenue[-12:]
    consecutive = not any(w["code"] == "MONTH_GAP" for w in features.warnings)
    amounts = [m["revenue_vnd"] for m in months] if consecutive else []
    return EInvoicePreviewOut(
        seller_tax_code=features.seller_tax_code,
        period_start=features.period_start,
        period_end=features.period_end,
        months_covered=features.months_covered,
        monthly_revenue=features.monthly_revenue,
        revenue_last_12m=sum(amounts) if len(amounts) == 12 else None,
        revenue_best_month=max(amounts) if amounts else None,
        revenue_worst_month=min(amounts) if amounts else None,
        conc_top1_pct=features.conc_top1_pct,
        conc_top3_pct=features.conc_top3_pct,
        warnings=features.warnings,
    )


@router.post("/confirm", response_model=UploadConfirmResponse)
async def confirm_upload(
    body: UploadConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sme),
):
    docs = await confirm_uploads(db, body, current_user)
    append_audit(
        db,
        entity_type="LOAN_APPLICATION",
        entity_id=body.loan_application_id,
        action="CONFIRM_UPLOADS",
        actor_id=current_user.id,
        after_state={"file_keys": [d.file_key for d in docs]},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return UploadConfirmResponse(
        documents=[LoanApplicationDocumentOut.model_validate(d) for d in docs]
    )


# --------------------------------------------------------------------------- #
# Generic (business/project-level) document presign+commit, merged in from
# app/files/ (HANDOFF-02 Fix C, module cleanup -- see docs/handoffs/HANDOFF-02-
# structural-fixes.md). Kept as its own router/prefix rather than folded into
# `router` above: route paths (/files/presign, /files/{file_id}/commit) and
# behavior are unchanged, since Phat confirmed the frontend doesn't currently
# call these, and whether this flow is planned-but-unbuilt or dead is still
# an open question with him -- this commit only relocates the code, it
# doesn't touch routes, schemas-on-the-wire, or behavior.
# --------------------------------------------------------------------------- #

files_router = APIRouter(prefix="/files", tags=["files"])


@files_router.post("/presign", response_model=FilePresignResponse, status_code=201)
async def presign(
    body: FilePresignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc, upload_url = await create_file_presign(db, body, current_user)
    append_audit(
        db,
        entity_type="DOCUMENT",
        entity_id=doc.id,
        action="PRESIGN",
        actor_id=current_user.id,
        after_state={"purpose": doc.purpose, "business_id": str(body.business_id)},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return FilePresignResponse(file_id=doc.id, upload_url=upload_url)


@files_router.post("/{file_id}/commit", response_model=FileCommitResponse)
async def commit(
    file_id: UUID,
    body: FileCommitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc = await commit_file(db, file_id, body, current_user)
    append_audit(
        db,
        entity_type="DOCUMENT",
        entity_id=doc.id,
        action="COMMIT",
        actor_id=current_user.id,
        after_state={"status": doc.status, "checksum": body.checksum},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return FileCommitResponse(file_id=doc.id, status=doc.status)
