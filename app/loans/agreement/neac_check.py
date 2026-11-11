"""The SME's signed listing documents, checked with NEAC for FundLok staff: the Service Agreement (Contract 2) and
the Standing Offer (Contract 3) and the Loan Contract template (Contract 5), signed after approval
(app/loans/signing.py).

Each signed upload is already checked on the way in, and that verdict is recorded here in the same shape, so the
admin project panel shows it at once. Staff can check again on demand ("Kiểm tra lại với NEAC"), and the panel runs
the check by itself the first time it opens a document that has none (an agreement signed in the old wizard).

What is checked and shown:
  right document   the PDF's text carries this application's reference and the document's version, as FundLok's
                   own download prints them on every page (it is this application's document, not some other PDF)
  right numbers    its check code is the one the document would carry today (pdf.check_code): the approved terms,
                   the assessment and the parties' details are what was signed. None for a copy that carries no
                   check code at all (signed before codes were printed)
  NEAC             NEAC's own verification (app/market/investor_agreement/neac.py): document unchanged, signature
                   valid, certificate not revoked
  right signer     the signer's MST on the certificate is the company's: its approved KYB enterprise code, or the
                   project's tax ID when there is no approved KYB; the name is compared too
  guarantor        Standing Offer and Loan Contract template: a separate valid signature by the Personal Guarantor,
                   whose ID is the eKYC ID number of the user who signed and uploaded it

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
from app.users.models import User
from app.loans.agreement import loan_contract_text, standing_offer_text, text
from app.market.investor_agreement import neac  # the one NEAC client, shared with the investor agreement
from app.utils.audit import append_audit
from app.utils.r2 import get_bytes

DOCUMENT_TYPE = "sme_service_agreement"
STANDING_OFFER_TYPE = "sme_standing_offer"
LOAN_CONTRACT_TYPE = "sme_loan_contract"
# The documents this checks: document type -> (the version printed on it, whether a Personal Guarantor also signs).
DOCUMENT_TYPES = {
    DOCUMENT_TYPE: (text.VERSION, False),
    STANDING_OFFER_TYPE: (standing_offer_text.VERSION, True),
    LOAN_CONTRACT_TYPE: (loan_contract_text.VERSION, True),
}
# Earlier versions copies on file may carry (signed before the current one), by document type. Such a copy is
# still the right document and its signatures are checked the same; its check code was worked out over that
# version's text, so there is nothing to compare it with today (terms_match None).
EARLIER_VERSIONS = {
    DOCUMENT_TYPE: ("sme-agreement-v0.3",),
    STANDING_OFFER_TYPE: ("standing-offer-v0.3.1",),
    LOAN_CONTRACT_TYPE: (),
}
CHECK_CODE_LABEL = "Mã đối chiếu"  # printed before the check code on every page
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


async def _signing_user(db: AsyncSession, document: LoanApplicationDocument, project: Project) -> User | None:
    """Whose details the document was filled in with: the SME user who uploaded the signed copy (the actor of its
    upload check), else the project's first owner (an agreement uploaded in the old wizard)."""
    actor = (
        await db.execute(
            select(AuditLog.actor_id)
            .where(
                AuditLog.entity_type == AUDIT_ENTITY,
                AuditLog.action == AUDIT_ACTION,
                AuditLog.entity_id == document.id,
                AuditLog.after_state["trigger"].astext == "SME_SIGNED_UPLOAD",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if actor is None:
        actor = (
            await db.execute(
                select(ProjectOwnership.user_id)
                .where(ProjectOwnership.project_id == project.id, ProjectOwnership.role == "OWNER")
                .order_by(ProjectOwnership.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    return await db.get(User, actor) if actor is not None else None


async def check_signed_document(
    db: AsyncSession, *, document: LoanApplicationDocument, actor_id: UUID | None, now: dt.datetime
) -> dict:
    """Check a stored signed listing document and record the verdict (audit). Never raises for NEAC being down:
    the verdict says UNAVAILABLE and staff check again later."""
    from app.loans import signing  # imports this module
    from app.loans.agreement.standing_offer import guarantor_for

    version, needs_guarantor = DOCUMENT_TYPES[document.document_type]
    application = await db.get(LoanApplication, document.loan_application_id)
    project = await db.get(Project, application.project_id)
    content = await run_in_threadpool(get_bytes, document.file_key, max_bytes=MAX_BYTES)

    ref = application_ref(application.id)
    body = _text_of(content)
    expected_id, expected_name, expected_source = await _company(db, project)
    user = await _signing_user(db, document, project)
    expected_code = (
        await signing.expected_check_code(db, application, user, signing.KINDS_BY_TYPE[document.document_type])
        if user is not None
        else None
    )
    earlier = None
    if version not in body:
        earlier = next((v for v in EARLIER_VERSIONS[document.document_type] if v in body), None)
        version = earlier or version
    if CHECK_CODE_LABEL not in body or earlier is not None:
        terms_match = None  # a copy from before check codes were printed, or of an earlier text: nothing to compare
    else:
        terms_match = expected_code is not None and expected_code in body
    guarantor_person = await guarantor_for(db, user, application) if needs_guarantor and user is not None else None

    verdict: dict = {
        "checked_at": now.isoformat(),
        "verified_by": "NEAC",
        "document_type": document.document_type,
        "file_name": document.original_filename,
        "application_ref": ref,
        "agreement_version": version,
        "right_document": ref in body and version in body,
        "terms_match": terms_match,
        "check_code": expected_code,
        "signature_fields": signing.signature_fields(content),
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
        "guarantor_required": needs_guarantor,
        "guarantor": (
            {
                "expected_id": guarantor_person.id_number if guarantor_person else None,
                "expected_name": guarantor_person.name if guarantor_person else None,
                "matches": False,
                "name_matches": False,
                "signer": None,
                "issuer": None,
                "signed_time": None,
                "problems": [],
            }
            if needs_guarantor
            else None
        ),
        "neac_message": None,
        "signatures": [],
    }
    try:
        result = await neac.verify_document(content, document.original_filename or "van-ban-da-ky.pdf")
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
            if needs_guarantor:
                # A signature of the Guarantor's own besides the company's: valid ones first.
                wanted = verdict["guarantor"]["expected_id"]
                theirs = sorted(
                    (s for s in result.signatures if s is not sig and neac.same_id(s, wanted)),
                    key=lambda s: bool(neac.problems(s)),
                )
                if theirs:
                    g = theirs[0]
                    verdict["guarantor"].update(
                        matches=not neac.problems(g),
                        name_matches=neac.same_name(g, verdict["guarantor"]["expected_name"]),
                        signer=g.signer,
                        issuer=g.issuer,
                        signed_time=g.signed_time,
                        problems=neac.problems(g),
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


# The name the Service Agreement check was introduced under.
check_sme_agreement = check_signed_document


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
