"""The SME's signed Service Agreement (wizard step 4), checked with NEAC for FundLok staff.

The SME's upload is not held up by this: the confirm step still only requires a digital signature to be present
(app/uploads/service.py). Staff see the result in the admin project panel, where it runs the first time someone
opens an application whose agreement has not been checked, and again on demand ("Kiểm tra lại với NEAC").

Three things are checked and shown:
  right document   the PDF's text carries this application's reference and the agreement's version, as FundLok's
                   own download prints them on every page (it is this application's Contract 2, not some other PDF)
  NEAC             NEAC's own verification (app/market/investor_agreement/neac.py): document unchanged, signature
                   valid, certificate not revoked
  right signer     the signer's MST on the certificate is the company's: its approved KYB enterprise code, or the
                   project's tax ID when there is no approved KYB; the name is compared too

Each check is an audit event (entity LOAN_APPLICATION_DOCUMENT, action NEAC_SIGNATURE_CHECKED), the verdict in
after_state; the newest one is what the panel shows. No new table: the audit log is append-only evidence already.
"""
from __future__ import annotations

import datetime as dt
import io
from uuid import UUID

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.gverify.models import GVerifyKybVerification
from app.lending.models import AuditLog, LoanApplication, LoanApplicationDocument, Project, ProjectOwnership
from app.loans.agreement import text
from app.market.investor_agreement import neac  # the one NEAC client, shared with the investor agreement
from app.utils.audit import append_audit
from app.utils.r2 import get_bytes

DOCUMENT_TYPE = "sme_service_agreement"
AUDIT_ENTITY = "LOAN_APPLICATION_DOCUMENT"
AUDIT_ACTION = "NEAC_SIGNATURE_CHECKED"
MAX_BYTES = 25 * 1024 * 1024  # the upload's own limit (uploads.schemas.DOCUMENT_TYPE_RULES)

# verdict["status"]
VERIFIED = "VERIFIED"  # NEAC accepts the signature
INVALID = "INVALID"  # NEAC found the signature, and does not accept it
NO_SIGNATURE = "NO_SIGNATURE"  # NEAC found no signature it could check
UNAVAILABLE = "UNAVAILABLE"  # NEAC could not be reached: check again later


def application_ref(application_id: UUID) -> str:
    """As the SME agreement PDF prints it: the application id's first block, upper-case."""
    return str(application_id).split("-")[0].upper()


def _text_of(content: bytes) -> str:
    try:
        return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(content)).pages)
    except Exception:  # not a readable PDF: not this agreement
        return ""


async def _company(db: AsyncSession, project: Project) -> tuple[str | None, str | None, str]:
    """(enterprise code, name, source) the signer should match: the owner's newest KYB if approved, else the
    project's own tax ID and legal name."""
    kyb = (
        await db.execute(
            select(GVerifyKybVerification)
            .join(ProjectOwnership, ProjectOwnership.user_id == GVerifyKybVerification.user_id)
            .where(ProjectOwnership.project_id == project.id)
            .order_by(GVerifyKybVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if kyb is not None and kyb.is_approved and kyb.tax_code:
        return kyb.tax_code, kyb.business_name or project.legal_name, "KYB"
    return project.tax_id, project.legal_name, "PROJECT"


async def check_sme_agreement(
    db: AsyncSession, *, document: LoanApplicationDocument, actor_id: UUID | None, now: dt.datetime
) -> dict:
    """Check the stored signed agreement and record the verdict (audit). Never raises for NEAC being down: the
    verdict says UNAVAILABLE and staff check again later."""
    application = await db.get(LoanApplication, document.loan_application_id)
    project = await db.get(Project, application.project_id)
    content = await run_in_threadpool(get_bytes, document.file_key, max_bytes=MAX_BYTES)

    ref = application_ref(application.id)
    body = _text_of(content)
    expected_id, expected_name, expected_source = await _company(db, project)
    verdict: dict = {
        "checked_at": now.isoformat(),
        "verified_by": "NEAC",
        "file_name": document.original_filename,
        "application_ref": ref,
        "agreement_version": text.VERSION,
        "right_document": ref in body and text.VERSION in body,
        "expected_tax_code": expected_id,
        "expected_name": expected_name,
        "expected_source": expected_source,
        "signer": None,
        "issuer": None,
        "signed_time": None,
        "certificate_validity": None,
        "ocsp": None,
        "crl": None,
        "problems": [],
        "signer_matches": False,
        "name_matches": False,
        "neac_message": None,
        "signatures": [],
    }
    try:
        result = await neac.verify_document(content, document.original_filename or "hop-dong-dich-vu.pdf")
    except neac.NeacUnavailable as exc:
        verdict.update(status=UNAVAILABLE, neac_message=str(exc))
    else:
        verdict["neac_message"] = result.message
        verdict["signatures"] = [s.as_dict() for s in result.signatures]
        if not result.signatures:
            verdict.update(status=NO_SIGNATURE, problems=[result.message or "Không tìm thấy chữ ký số"])
        else:
            # The signature that best answers "did this company validly sign it": valid and the company's first.
            ranked = sorted(
                result.signatures,
                key=lambda s: (bool(neac.problems(s)), not neac.same_id(s, expected_id)),
            )
            sig = ranked[0]
            issues = neac.problems(sig)
            verdict.update(
                status=INVALID if issues else VERIFIED,
                problems=issues,
                signer=sig.signer,
                issuer=sig.issuer,
                signed_time=sig.signed_time,
                certificate_validity=sig.certificate_validity,
                ocsp=sig.ocsp,
                crl=sig.crl,
                signer_matches=neac.same_id(sig, expected_id),
                name_matches=neac.same_name(sig, expected_name),
            )
    append_audit(
        db,
        entity_type=AUDIT_ENTITY,
        entity_id=document.id,
        action=AUDIT_ACTION,
        actor_id=actor_id,
        after_state=verdict,
    )
    await db.flush()
    return verdict


async def latest_checks(db: AsyncSession, document_ids: list[UUID]) -> dict[UUID, dict]:
    """The newest verdict per document, for the given documents."""
    if not document_ids:
        return {}
    rows = (
        await db.execute(
            select(AuditLog.entity_id, AuditLog.after_state)
            .where(
                AuditLog.entity_type == AUDIT_ENTITY,
                AuditLog.action == AUDIT_ACTION,
                AuditLog.entity_id.in_(document_ids),
            )
            .order_by(AuditLog.created_at.desc())
        )
    ).all()
    out: dict[UUID, dict] = {}
    for entity_id, state in rows:
        out.setdefault(entity_id, state)
    return out
