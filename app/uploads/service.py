import uuid
from datetime import date, datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.lending.models import Document, LoanApplication, LoanApplicationDocument, Project
from app.projects.service import user_owns_project
from app.uploads.models import CicSummary, EInvoiceSummary, TaxFilingSummary
from app.uploads.parsers.cic import CicParseError, parse_cic_pdf
from app.uploads.parsers.einvoice import EInvoiceParseError, parse_einvoice_zip
from app.uploads.parsers.tax_filings import PARSER_VERSION as TAX_FILINGS_PARSER_VERSION
from app.uploads.parsers.tax_filings import TaxFilingsParseError, parse_tax_filings
from app.uploads.schemas import (
    DOCUMENT_TYPE_RULES,
    EXTENSION_CONTENT_TYPES,
    FileCommitRequest,
    FilePresignRequest,
    UploadConfirmRequest,
    UploadPresignRequest,
    safe_object_filename,
)
from app.users.models import Role, User
from app.utils.r2 import delete_object, get_bytes, head_object, presign_put


async def _get_owned_draft_application(db: AsyncSession, application_id: UUID, current_user: User) -> LoanApplication:
    result = await db.execute(select(LoanApplication).where(LoanApplication.id == application_id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")
    if not await user_owns_project(db, current_user.id, row.project_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if row.status != "DRAFT":
        raise HTTPException(status_code=400, detail="Application is not in DRAFT status")
    return row


def _validate_file(body: UploadPresignRequest) -> str:
    rules = DOCUMENT_TYPE_RULES.get(body.document_type)
    if rules is None:
        raise HTTPException(status_code=400, detail="Invalid document_type")
    if "." not in body.filename:
        raise HTTPException(status_code=400, detail="Filename must have an extension")
    ext = body.filename.rsplit(".", 1)[1].lower()
    if ext not in rules["extensions"]:
        allowed = ", ".join(sorted(rules["extensions"]))
        raise HTTPException(
            status_code=400,
            detail=f"Extension .{ext} not allowed for {body.document_type} (allowed: {allowed})",
        )
    if body.content_type not in EXTENSION_CONTENT_TYPES[ext]:
        raise HTTPException(status_code=400, detail=f"content_type not allowed for .{ext}")
    if body.size > rules["max_size"]:
        raise HTTPException(
            status_code=400,
            detail=f"File too large for {body.document_type} (max {rules['max_size']} bytes)",
        )
    return ext


async def create_upload_presign(
    db: AsyncSession,
    body: UploadPresignRequest,
    current_user: User,
) -> tuple[LoanApplicationDocument, str, int]:
    app_row = await _get_owned_draft_application(db, body.loan_application_id, current_user)
    ext = _validate_file(body)

    # Key is generated server-side; the client never picks it. The name is
    # deterministic per document type, so a re-upload overwrites the old object
    # and each application holds at most one file per type.
    file_key = f"application_documents/{current_user.id}/{app_row.id}/{body.document_type}.{ext}"
    result = await db.execute(
        select(LoanApplicationDocument).where(
            LoanApplicationDocument.loan_application_id == app_row.id,
            LoanApplicationDocument.document_type == body.document_type,
        )
    )
    doc = result.scalar_one_or_none()
    if doc:
        if doc.file_key != file_key:
            # Extension changed (e.g. e_invoice_data .zip -> .xlsx): the old
            # object would be orphaned, so remove it.
            delete_object(doc.file_key)
            doc.file_key = file_key
        doc.original_filename = body.filename
        doc.content_type = body.content_type
        doc.file_size_bytes = body.size
        doc.status = "PENDING"
        doc.uploaded_at = None
    else:
        doc = LoanApplicationDocument(
            loan_application_id=app_row.id,
            document_type=body.document_type,
            file_key=file_key,
            original_filename=body.filename,
            content_type=body.content_type,
            file_size_bytes=body.size,
            status="PENDING",
        )
        db.add(doc)
    await db.flush()
    upload_url = presign_put(file_key, body.content_type)
    return doc, upload_url, settings.R2_PRESIGN_EXPIRE_SECONDS


async def confirm_uploads(
    db: AsyncSession,
    body: UploadConfirmRequest,
    current_user: User,
) -> list[LoanApplicationDocument]:
    app_row = await _get_owned_draft_application(db, body.loan_application_id, current_user)

    file_keys = list(dict.fromkeys(body.file_keys))
    result = await db.execute(
        select(LoanApplicationDocument).where(
            LoanApplicationDocument.loan_application_id == app_row.id,
            LoanApplicationDocument.file_key.in_(file_keys),
        )
    )
    docs = result.scalars().all()
    by_key = {d.file_key: d for d in docs}
    missing = [k for k in file_keys if k not in by_key]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown file_key(s): {', '.join(missing)}")

    now = datetime.now(timezone.utc)
    for key in file_keys:
        doc = by_key[key]
        if doc.status == "UPLOADED":
            continue
        head = head_object(key)
        if head is None:
            raise HTTPException(status_code=400, detail=f"Object not found in storage: {key}")
        max_size = DOCUMENT_TYPE_RULES[doc.document_type]["max_size"]
        if head["size"] and head["size"] > max_size:
            raise HTTPException(status_code=400, detail=f"Uploaded object too large: {key}")
        doc.file_size_bytes = head["size"]
        if head["content_type"]:
            doc.content_type = head["content_type"]
        doc.status = "UPLOADED"
        doc.uploaded_at = now
        db.add(doc)
        if doc.document_type == "e_invoice_data":
            await _ingest_einvoice(db, app_row, doc)
        elif doc.document_type == "tax_filings":
            await _ingest_tax_filings(db, app_row, doc)
        elif doc.document_type == "cic_report":
            await _ingest_cic(db, app_row, doc)
    await db.flush()
    return [by_key[k] for k in file_keys]


def _einvoice_error(message: str, code: str) -> HTTPException:
    # 422 with a translatable code in X-Error-Code, the same contract the rate
    # calculator uses (exposed by CORS in app/main.py), so the wizard can tell
    # the SME in their language which month or file to fix.
    return HTTPException(
        status_code=422,
        detail=message,
        headers={"X-Error-Code": f"EINVOICE_{code}"},
    )


async def parse_and_check_einvoice(db: AsyncSession, app_row: LoanApplication, raw: bytes):
    """Parse an e-invoice archive and note whether it is this application's company.

    Shared by the wizard's preview (bytes from the browser) and by confirm
    (bytes from storage), so both refuse exactly the same things.
    """
    try:
        # Off the event loop: XML parsing is CPU-bound, and a large archive
        # would stall every other request.
        features = await run_in_threadpool(parse_einvoice_zip, raw)
    except EInvoiceParseError as exc:
        raise _einvoice_error(str(exc), exc.code) from exc

    # Whose sales these are. The project's tax id is filled from the verified
    # business registration (KYB). A listing under another tax code is NOT
    # refused — a product decision (Phat, 2026-09-27) — but it is recorded, so
    # underwriting can see the revenue may belong to a different company.
    project = (await db.execute(select(Project).where(Project.id == app_row.project_id))).scalar_one()
    expected = (project.tax_id or "").strip()
    if expected and features.seller_tax_code != expected:
        features.warnings.append({
            "code": "SELLER_MISMATCH",
            "detail": (
                f"invoices issued by tax code {features.seller_tax_code}, "
                f"application is for {expected}"
            ),
        })
    if not expected:
        features.warnings.append({
            "code": "SELLER_UNVERIFIED",
            "detail": f"the project has no tax id to compare with {features.seller_tax_code}",
        })
    return features


async def preview_einvoice(db: AsyncSession, application_id: UUID, raw: bytes, current_user: User):
    """What the wizard shows the moment the SME picks the zip. Stores nothing."""
    app_row = await _get_owned_draft_application(db, application_id, current_user)
    return await parse_and_check_einvoice(db, app_row, raw)


async def _ingest_einvoice(db: AsyncSession, app_row: LoanApplication, doc: LoanApplicationDocument) -> None:
    """Parse the uploaded listings and store what they say, or refuse them.

    Runs at confirm, before submit, so a broken, foreign or partial archive is
    fixed by the SME in the wizard instead of surfacing in underwriting. On any
    refusal the request fails and nothing is committed: the document stays
    PENDING and the SME re-uploads.
    """

    def fetch():
        return get_bytes(doc.file_key, max_bytes=DOCUMENT_TYPE_RULES["e_invoice_data"]["max_size"])

    try:
        raw = await run_in_threadpool(fetch)
    except ValueError as exc:
        raise _einvoice_error(str(exc), "ARCHIVE_TOO_LARGE") from exc

    # Parsed again here, from the STORED object, even when the wizard already
    # previewed it: the preview ran on bytes the browser sent, and what grades
    # the application must be what was actually submitted.
    features = await parse_and_check_einvoice(db, app_row, raw)

    # The SME's step-2 figures are prefilled from these invoices; if what was
    # saved no longer matches (edited, or a different file previewed), say so
    # rather than silently grading on either.
    figures = app_row.self_reported_figures or {}
    months = features.monthly_revenue[-12:]
    consecutive = not any(w["code"] == "MONTH_GAP" for w in features.warnings)
    if len(months) == 12 and consecutive and figures.get("revenue_last_12m") is not None:
        invoiced = sum(m["revenue_vnd"] for m in months)
        if int(figures["revenue_last_12m"]) != invoiced:
            features.warnings.append({
                "code": "FIGURES_DIFFER",
                "detail": f"typed revenue_last_12m {int(figures['revenue_last_12m']):,} vs invoiced {invoiced:,}",
            })
    warnings = features.warnings

    summary = (
        await db.execute(select(EInvoiceSummary).where(EInvoiceSummary.loan_application_id == app_row.id))
    ).scalar_one_or_none()
    if summary is None:
        summary = EInvoiceSummary(loan_application_id=app_row.id)
    summary.document_id = doc.id
    summary.parser_version = features.parser_version
    summary.seller_tax_code = features.seller_tax_code
    summary.period_start = features.period_start
    summary.period_end = features.period_end
    summary.months_covered = features.months_covered
    summary.features = features.as_dict()
    summary.warnings = warnings
    db.add(summary)


# --------------------------------------------------------------------------- #
# Generic (business/project-level) document presign+commit, merged in from
# app/files/ (HANDOFF-02 Fix C, module cleanup). Behavior is unchanged from
# the original app/files/service.py -- only the import location moved.
# --------------------------------------------------------------------------- #

def _file_purpose_allowed(purpose: str) -> bool:
    return purpose in (
        "KYC_ID",
        "KYC_ADDRESS",
        "KYC_BUSINESS_REG",
        "BANK_STATEMENT",
        "INVOICE",
        "OTHER",
    )


async def create_file_presign(
    db: AsyncSession,
    body: FilePresignRequest,
    current_user: User,
) -> tuple[Document, str]:
    if current_user.role not in (Role.SME.value, Role.ADMIN.value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if not _file_purpose_allowed(body.purpose):
        raise HTTPException(status_code=400, detail="Invalid purpose")
    result = await db.execute(select(Project).where(Project.id == body.business_id))
    proj = result.scalar_one_or_none()
    if not proj:
        raise HTTPException(status_code=404, detail="Business not found")
    if current_user.role == Role.SME.value and not await user_owns_project(db, current_user.id, body.business_id):
        raise HTTPException(status_code=403, detail="Business does not belong to this user")

    # The filename is sanitised on the way into the key (path separators, URL
    # metacharacters and control characters all collapse), while `Document`
    # below keeps the value the user actually sent, for display.
    storage_key = (
        f"fundlok/{body.business_id}/{uuid.uuid4()}/"
        f"{safe_object_filename(body.filename)}"
    )
    doc = Document(
        entity_type="PROJECT",
        entity_id=body.business_id,
        purpose=body.purpose,
        filename=body.filename,
        mime_type=body.mime_type,
        status="PENDING",
        storage_key=storage_key,
    )
    db.add(doc)
    await db.flush()
    # quote() the key: it is a path-shaped value going into a query parameter,
    # so an unencoded "/" or "&" would split or truncate it.
    upload_url = (
        f"{settings.MOCK_UPLOAD_BASE_URL.rstrip('/')}/{doc.id}"
        f"?key={quote(storage_key, safe='')}"
    )
    return doc, upload_url


async def commit_file(
    db: AsyncSession,
    file_id: UUID,
    body: FileCommitRequest,
    current_user: User,
) -> Document:
    result = await db.execute(select(Document).where(Document.id == file_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")
    if doc.entity_type == "PROJECT":
        if current_user.role == Role.SME.value and not await user_owns_project(
            db, current_user.id, doc.entity_id
        ):
            raise HTTPException(status_code=403, detail="Not authorized")
        elif current_user.role not in (Role.SME.value, Role.ADMIN.value):
            raise HTTPException(status_code=403, detail="Not authorized")
    else:
        if current_user.role not in (Role.SME.value, Role.ADMIN.value):
            raise HTTPException(status_code=403, detail="Not authorized")

    if doc.status in ("APPROVED", "UPLOADED", "SCANNING") and doc.checksum_sha256 == body.checksum:
        return doc

    doc.checksum_sha256 = body.checksum
    doc.size_bytes = body.size
    doc.status = "SCANNING"
    db.add(doc)
    return doc


# --------------------------------------------------------------------------- #
# Step 3: tax filings (year-end statements + monthly VAT declarations)
# --------------------------------------------------------------------------- #


def _tax_filings_error(message: str, code: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message, headers={"X-Error-Code": f"TAXFILINGS_{code}"})


async def parse_and_record_tax_filings(db: AsyncSession, app_row: LoanApplication, raw: bytes):
    """Parse the filings; note (never refuse) a tax code that is not the
    application's company, consistent with the e-invoice rule."""
    try:
        filings = await run_in_threadpool(parse_tax_filings, raw)
    except TaxFilingsParseError as exc:
        raise _tax_filings_error(str(exc), exc.code) from exc
    project = (await db.execute(select(Project).where(Project.id == app_row.project_id))).scalar_one()
    expected = (project.tax_id or "").strip()
    seller = filings.statements["tax_code"]
    if expected and seller != expected:
        filings.warnings.append({
            "code": "SELLER_MISMATCH",
            "detail": f"statements filed under tax code {seller}, application is for {expected}",
        })
    return filings


async def preview_tax_filings(db: AsyncSession, application_id: UUID, raw: bytes, current_user: User):
    """What step 3 shows the moment the SME picks the file. Stores nothing."""
    app_row = await _get_owned_draft_application(db, application_id, current_user)
    return await parse_and_record_tax_filings(db, app_row, raw)


async def _ingest_tax_filings(db: AsyncSession, app_row: LoanApplication, doc: LoanApplicationDocument) -> None:
    """Re-parse the STORED file at confirm and keep what it says."""

    def fetch():
        return get_bytes(doc.file_key, max_bytes=DOCUMENT_TYPE_RULES["tax_filings"]["max_size"])

    try:
        raw = await run_in_threadpool(fetch)
    except ValueError as exc:
        raise _tax_filings_error(str(exc), "ARCHIVE_TOO_LARGE") from exc
    filings = await parse_and_record_tax_filings(db, app_row, raw)

    # The step-3 figures are prefilled from these statements. Cost of goods
    # sold is a statutory line, so a saved figure that no longer matches it is
    # worth underwriting's attention.
    figures = app_row.self_reported_figures or {}
    typed_cogs = figures.get("cogs_y1")
    if typed_cogs is not None and int(typed_cogs) != filings.statements["cogs_y1"]:
        filings.warnings.append({
            "code": "FIGURES_DIFFER",
            "detail": f"typed cogs_y1 {int(typed_cogs):,} vs statements {filings.statements['cogs_y1']:,}",
        })

    summary = (
        await db.execute(select(TaxFilingSummary).where(TaxFilingSummary.loan_application_id == app_row.id))
    ).scalar_one_or_none()
    if summary is None:
        summary = TaxFilingSummary(loan_application_id=app_row.id)
    summary.document_id = doc.id
    summary.parser_version = TAX_FILINGS_PARSER_VERSION
    summary.tax_code = filings.statements["tax_code"]
    summary.fiscal_year = filings.statements["fiscal_year"]
    summary.vat_months = filings.vat["months_covered"] if filings.vat else 0
    summary.statements = filings.statements
    summary.vat = filings.vat
    summary.warnings = filings.warnings
    db.add(summary)


# --------------------------------------------------------------------------- #
# Step 4: the CIC credit report
# --------------------------------------------------------------------------- #


def _cic_error(message: str, code: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message, headers={"X-Error-Code": f"CIC_{code}"})


async def parse_cic(raw: bytes):
    """Read a CIC report. Refuses only what is not a readable CIC report; a
    report whose score cannot be found is kept, with a warning, so a genuine
    report is never bounced because the parser has not seen its layout."""
    try:
        return await run_in_threadpool(parse_cic_pdf, raw)
    except CicParseError as exc:
        raise _cic_error(str(exc), exc.code) from exc


async def preview_cic(db: AsyncSession, application_id: UUID, raw: bytes, current_user: User):
    """What step 4 shows the moment the SME picks the PDF. Stores nothing."""
    await _get_owned_draft_application(db, application_id, current_user)
    return await parse_cic(raw)


async def _ingest_cic(db: AsyncSession, app_row: LoanApplication, doc: LoanApplicationDocument) -> None:
    """Re-parse the STORED report at confirm and keep what it says."""

    def fetch():
        return get_bytes(doc.file_key, max_bytes=DOCUMENT_TYPE_RULES["cic_report"]["max_size"])

    try:
        raw = await run_in_threadpool(fetch)
    except ValueError as exc:
        raise _cic_error(str(exc), "FILE_TOO_LARGE") from exc
    report = await parse_cic(raw)

    summary = (
        await db.execute(select(CicSummary).where(CicSummary.loan_application_id == app_row.id))
    ).scalar_one_or_none()
    if summary is None:
        summary = CicSummary(loan_application_id=app_row.id)
    summary.document_id = doc.id
    summary.parser_version = report.parser_version
    summary.subject_type = report.subject_type
    summary.score = report.score
    summary.rank = report.rank
    summary.scored_on = date.fromisoformat(report.scored_on) if report.scored_on else None
    summary.report = report.as_dict()
    summary.warnings = report.warnings
    db.add(summary)
