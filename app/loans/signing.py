"""Signing the listing documents after approval. Spec: docs/specs/loans/listing-signing.md

Once FundLok has approved a request (the operator's approval and a LOCKED score run, which fixes the rate), the SME
downloads two documents filled in with the approved terms, signs them and uploads the signed copies here:

  service-agreement   Contract 2, SME Service Agreement      the SME's enterprise digital signature
  standing-offer      Contract 3, Standing Offer             the SME's enterprise digital signature AND the
                                                             Personal Guarantor's personal digital signature

Nobody reviews them before they take effect, so each upload is checked automatically on the way in, the way the
investor's signed agreement is (app/market/investor_agreement):

  right document   this application's document of this kind and version (both printed on every page)
  right numbers    its check code (pdf.check_code, on every page) is the one the document would carry today: the
                   approved terms, the assessment and estimate, and the parties' details are what was signed. A copy
                   downloaded before something changed, or edited, is refused (SIGNING_DOCUMENT_OUTDATED)
  every signature  each signer signed: at least as many signed signature fields as signers (the documents carry
                   one empty field per signer: SME_Signature, and Guarantor_Signature on the Standing Offer)
  NEAC             the national authority's own verification accepts the signatures (unchanged, valid, not revoked)
  right signers    the company (its approved KYB enterprise code, else the project's tax ID) and, on the Standing
                   Offer, the Guarantor (the signing user's eKYC ID number) with a signature of their own

Nothing is accepted unchecked: NEAC down is a 503 and the SME tries again. The verdict is kept as the document's
NEAC_SIGNATURE_CHECKED audit event, which is what the admin panel shows (app/loans/agreement/neac_check.py) and what
the SME's signing card reads back (`checks_for`).

When both are on file the project goes live (projects.service.activate_project_if_approved), which also opens its
funding window if the SME's bank account is approved: signing publishes the Listing (Contract 3, "Ký kết").
"""
from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.gverify.models import GVerifyKybVerification
from app.lending.models import LoanApplication, LoanApplicationDocument, Project, ProjectOwnership
from app.loans.agreement import standing_offer_text
from app.loans.agreement import text as agreement_text
from app.loans.agreement.neac_check import AUDIT_ACTION, AUDIT_ENTITY, VERIFIED, application_ref, latest_checks
from app.loans.agreement.pdf import GUARANTOR_SIGNATURE_FIELD, SME_SIGNATURE_FIELD
from app.loans.agreement.service import SERVICE_AGREEMENT_DOCUMENT_TYPE, agreement_data
from app.loans.agreement.signature import has_digital_signature, is_pdf
from app.loans.agreement.standing_offer import guarantor_for, offer_data
from app.loans.agreement.terms import locked_terms
from app.market.investor_agreement import neac
from app.users.models import User
from app.utils.audit import append_audit
from app.utils.r2 import get_bytes, put_bytes

STANDING_OFFER_DOCUMENT_TYPE = "sme_standing_offer"
MAX_BYTES = 25 * 1024 * 1024  # the type's own limit (uploads.schemas.DOCUMENT_TYPE_RULES)


@dataclass(frozen=True)
class Kind:
    slug: str  # in the URL
    document_type: str
    version: str
    needs_guarantor: bool
    # The empty signature fields the document carries, one per signer.
    signature_fields: tuple[str, ...]


SERVICE_AGREEMENT = Kind(
    "service-agreement", SERVICE_AGREEMENT_DOCUMENT_TYPE, agreement_text.VERSION, False, (SME_SIGNATURE_FIELD,)
)
STANDING_OFFER = Kind(
    "standing-offer",
    STANDING_OFFER_DOCUMENT_TYPE,
    standing_offer_text.VERSION,
    True,
    (SME_SIGNATURE_FIELD, GUARANTOR_SIGNATURE_FIELD),
)
KINDS = {k.slug: k for k in (SERVICE_AGREEMENT, STANDING_OFFER)}
KINDS_BY_TYPE = {k.document_type: k for k in KINDS.values()}


def _refuse(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


def kind_for(slug: str) -> Kind:
    kind = KINDS.get(slug)
    if kind is None:
        raise HTTPException(status_code=404, detail="Unknown document")
    return kind


async def signed_documents(db: AsyncSession, application_id: UUID) -> dict[str, LoanApplicationDocument]:
    """The application's signed listing documents on file, by document type."""
    rows = (
        await db.execute(
            select(LoanApplicationDocument).where(
                LoanApplicationDocument.loan_application_id == application_id,
                LoanApplicationDocument.document_type.in_([k.document_type for k in KINDS.values()]),
                LoanApplicationDocument.status == "UPLOADED",
            )
        )
    ).scalars().all()
    return {row.document_type: row for row in rows}


async def both_signed(db: AsyncSession, application_id: UUID) -> bool:
    return len(await signed_documents(db, application_id)) == len(KINDS)


@dataclass(frozen=True)
class SigningStatus:
    # Approved with locked terms: there is something to sign (or already signed).
    ready: bool
    # The project is live on the marketplace.
    published: bool
    # The signing user has an approved eKYC with an ID number: the Standing Offer's Guarantor can be checked.
    guarantor_verified: bool
    signed: dict[str, LoanApplicationDocument]


async def status(db: AsyncSession, application: LoanApplication, user: User) -> SigningStatus:
    project = await db.get(Project, application.project_id)
    guarantor = await guarantor_for(db, user)
    return SigningStatus(
        ready=await locked_terms(db, application) is not None,
        published=project is not None and project.status == "ACTIVE",
        guarantor_verified=guarantor.id_number is not None,
        signed=await signed_documents(db, application.id),
    )


async def _company(db: AsyncSession, project: Project) -> tuple[str | None, str | None, str]:
    """(enterprise code, name, source) the company's signature must carry: the owner's newest KYB if approved, else
    the project's own tax ID and legal name (as neac_check._company)."""
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


def _text_of(content: bytes) -> str:
    """The PDF's text. Signing adds to a PDF; it keeps the original text, the check code included."""
    try:
        return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(content)).pages)
    except Exception:  # an unreadable PDF is not this document
        return ""


def signature_fields(content: bytes) -> dict[str, bool]:
    """Every signature field in the PDF and whether it is signed (has a value), by field name. A digital signature
    in a PDF is always the value of a signature field: the empty ones FundLok puts in, or one the signing tool adds.
    """
    try:
        fields = PdfReader(io.BytesIO(content)).get_fields() or {}
    except Exception:
        return {}
    return {name: field.get("/V") is not None for name, field in fields.items() if field.get("/FT") == "/Sig"}


async def expected_check_code(db: AsyncSession, application: LoanApplication, user: User, kind: Kind) -> str:
    """The check code the document of `kind` carries when generated now, for this user."""
    if kind is STANDING_OFFER:
        return (await offer_data(db, application, user)).check_code
    return (await agreement_data(db, application, user)).check_code


def _who(sig: neac.NeacSignature) -> str:
    return f"{sig.signer.get('cn') or 'someone else'} ({sig.signer.get('mst') or sig.signer.get('cccd') or 'no ID'})"


async def _neac_verdict(
    content: bytes,
    file_name: str | None,
    *,
    kind: Kind,
    company_id: str | None,
    company_name: str | None,
    company_source: str,
    guarantor_id: str | None,
    guarantor_name: str | None,
) -> dict:
    """NEAC's verdict, required to be valid and signed by the right people; what is kept with the document."""
    try:
        result = await neac.verify_document(content, file_name or f"{kind.slug}.pdf")
    except neac.NeacUnavailable as exc:
        raise _refuse(
            503,
            "NEAC_UNAVAILABLE",
            "The digital signature could not be checked with NEAC right now. Please try again in a few minutes.",
        ) from exc
    if not result.signatures:
        raise _refuse(422, "SIGNING_SIGNATURE_INVALID", f"NEAC: {result.message or 'no digital signature found'}")
    valid = [sig for sig in result.signatures if not neac.problems(sig)]
    if not valid:
        reasons = "; ".join(neac.problems(result.signatures[0]))
        raise _refuse(422, "SIGNING_SIGNATURE_INVALID", f"NEAC: {reasons}")
    company = [sig for sig in valid if neac.same_id(sig, company_id)]
    if not company:
        raise _refuse(
            422,
            "SIGNING_SIGNER_MISMATCH",
            f"The document is signed by {_who(valid[0])}, not with the company's enterprise digital signature "
            f"({company_id or 'no enterprise code on file'}).",
        )
    sig = company[0]
    guarantor = None
    if kind.needs_guarantor:
        # A signature of the Guarantor's own, besides the company's: one certificate carrying both IDs is one
        # signature, and the Offer needs two.
        personal = [g for g in valid if neac.same_id(g, guarantor_id) and g is not sig]
        if not personal:
            raise _refuse(
                422,
                "SIGNING_GUARANTOR_MISSING",
                f"The Standing Offer also needs the Personal Guarantor's personal digital signature "
                f"({guarantor_name or 'the guarantor'}, ID {guarantor_id}).",
            )
        g = personal[0]
        guarantor = {
            "role": "GUARANTOR",
            "matches": True,
            "problems": [],
            "signer": g.signer,
            "issuer": g.issuer,
            "signed_time": g.signed_time,
            "expected_id": guarantor_id,
            "expected_name": guarantor_name,
            "name_matches": neac.same_name(g, guarantor_name),
        }
    return {
        "status": VERIFIED,
        "verified_by": "NEAC",
        "verify_url": neac.VERIFY_URL,
        "document_type": kind.document_type,
        "right_document": True,
        "agreement_version": kind.version,
        "expected_tax_code": company_id,
        "expected_name": company_name,
        "expected_source": company_source,
        "signer": sig.signer,
        "issuer": sig.issuer,
        "signed_time": sig.signed_time,
        "certificate_validity": sig.certificate_validity,
        "ocsp": sig.ocsp,
        "crl": sig.crl,
        "problems": [],
        "signer_matches": True,
        "name_matches": neac.same_name(sig, company_name),
        "guarantor_required": kind.needs_guarantor,
        "guarantor": guarantor,
        # Who signed, by role, as the SME's signing card shows it.
        "signers": [
            {"role": "SME", "name": sig.signer.get("cn"), "id": company_id},
            *([{"role": "GUARANTOR", "name": guarantor["signer"].get("cn"), "id": guarantor_id}] if guarantor else []),
        ],
        "neac_message": result.message,
        "signatures": [s.as_dict() for s in result.signatures],
    }


async def record_signed(
    db: AsyncSession,
    *,
    application: LoanApplication,
    user: User,
    kind: Kind,
    content: bytes,
    file_name: str | None,
    now: dt.datetime,
    ip_address: str | None = None,
) -> LoanApplicationDocument:
    """Check and keep the SME's signed copy of `kind`; when it is the second of the two, publish the project.

    409 SIGNING_NOT_OPEN before FundLok has approved the request with locked terms; 409 SIGNING_ALREADY_SIGNED when
    this document is already on file; 413 SIGNING_FILE_TOO_LARGE; 415 SIGNING_NOT_PDF; 422 SIGNING_UNSIGNED for a
    PDF with no digital signature in it; 422 SIGNING_WRONG_DOCUMENT when it is not this application's document of
    this kind and version; 422 SIGNING_DOCUMENT_OUTDATED when it is, but its check code is not the one it would
    carry today (terms, assessment or a party's details changed since it was downloaded, or it was edited); 422
    SIGNING_GUARANTOR_MISSING before NEAC too, when the Standing Offer has fewer signed signature fields than
    signers; 422 SIGNING_GUARANTOR_UNVERIFIED (Standing Offer) when the signing user has no approved
    eKYC with an ID number to check the Guarantor against; 422 SIGNING_SIGNATURE_INVALID when NEAC does not accept
    the signature (its reasons in the detail); 422 SIGNING_SIGNER_MISMATCH when no valid signature is the
    company's; 422 SIGNING_GUARANTOR_MISSING when the Standing Offer has no valid personal signature of the
    Guarantor's; 503 NEAC_UNAVAILABLE when NEAC cannot be reached.
    """
    if await locked_terms(db, application) is None:
        raise _refuse(409, "SIGNING_NOT_OPEN", "There is nothing to sign until FundLok approves the request")
    if kind.document_type in await signed_documents(db, application.id):
        raise _refuse(409, "SIGNING_ALREADY_SIGNED", "This document is already signed and on file")
    if len(content) > MAX_BYTES:
        raise _refuse(413, "SIGNING_FILE_TOO_LARGE", "The signed PDF is larger than 25 MB")
    if not is_pdf(content):
        raise _refuse(415, "SIGNING_NOT_PDF", "Upload the signed document as a PDF")
    if not has_digital_signature(content):
        raise _refuse(
            422,
            "SIGNING_UNSIGNED",
            "The PDF has no digital signature. Sign it with the enterprise's digital signature and upload the "
            "signed copy.",
        )
    ref = application_ref(application.id)
    body = _text_of(content)
    if ref not in body or kind.version not in body:
        raise _refuse(
            422,
            "SIGNING_WRONG_DOCUMENT",
            f"This is not the current {kind.slug.replace('-', ' ')} for application {ref}. Download it from this "
            "step, sign it, and upload that file.",
        )
    code = await expected_check_code(db, application, user, kind)
    if code not in body:
        raise _refuse(
            422,
            "SIGNING_DOCUMENT_OUTDATED",
            "The signed copy does not match the approved terms and details as they stand now (check code "
            f"{code}): something changed since it was downloaded, or it was edited. Download it again, sign it, "
            "and upload that file.",
        )
    fields = signature_fields(content)
    signed_fields = [name for name, signed in fields.items() if signed]
    if kind.needs_guarantor and len(signed_fields) < len(kind.signature_fields):
        raise _refuse(
            422,
            "SIGNING_GUARANTOR_MISSING",
            f"The Standing Offer carries {len(signed_fields)} signature; it needs the company's and the Personal "
            "Guarantor's, one in each signature box.",
        )
    guarantor = await guarantor_for(db, user) if kind.needs_guarantor else None
    if guarantor is not None and guarantor.id_number is None:
        raise _refuse(
            422,
            "SIGNING_GUARANTOR_UNVERIFIED",
            "The Personal Guarantor's identity is not verified yet. Complete identity verification (eKYC) first.",
        )

    project = await db.get(Project, application.project_id)
    company_id, company_name, company_source = await _company(db, project)
    verdict = await _neac_verdict(
        content,
        file_name,
        kind=kind,
        company_id=company_id,
        company_name=company_name,
        company_source=company_source,
        guarantor_id=guarantor.id_number if guarantor else None,
        guarantor_name=guarantor.name if guarantor else None,
    )

    # Serialised per application, after the slow NEAC call: two uploads at once (the two documents, or the same
    # one twice) would otherwise each miss the other, and the second of the two documents would not publish.
    await db.execute(select(LoanApplication.id).where(LoanApplication.id == application.id).with_for_update())
    existing = (
        await db.execute(
            select(LoanApplicationDocument).where(
                LoanApplicationDocument.loan_application_id == application.id,
                LoanApplicationDocument.document_type == kind.document_type,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == "UPLOADED":
        raise _refuse(409, "SIGNING_ALREADY_SIGNED", "This document is already signed and on file")

    # The same key scheme as the wizard's uploads: one object per type per application.
    file_key = f"application_documents/{user.id}/{application.id}/{kind.document_type}.pdf"
    await run_in_threadpool(put_bytes, file_key, content, "application/pdf")
    shown_name = (file_name or f"{kind.slug}-{ref}.pdf")[:255]
    if existing is None:
        existing = LoanApplicationDocument(
            loan_application_id=application.id,
            document_type=kind.document_type,
            file_key=file_key,
            original_filename=shown_name,
        )
    existing.file_key = file_key
    existing.original_filename = shown_name
    existing.content_type = "application/pdf"
    existing.file_size_bytes = len(content)
    existing.status = "UPLOADED"
    existing.uploaded_at = now
    db.add(existing)
    await db.flush()

    append_audit(
        db,
        entity_type=AUDIT_ENTITY,
        entity_id=existing.id,
        action=AUDIT_ACTION,
        actor_id=user.id,
        after_state={
            **verdict,
            "terms_match": True,
            "check_code": code,
            "signature_fields": fields,
            "checked_at": now.isoformat(),
            "file_name": shown_name,
            "application_ref": ref,
            "trigger": "SME_SIGNED_UPLOAD",
        },
        ip_address=ip_address,
    )
    await db.flush()

    if await both_signed(db, application.id):
        # Imported here: app.projects.service imports this module for its gate.
        from app.projects.service import activate_project_if_approved

        await activate_project_if_approved(db, application.id, actor_id=user.id)
    return existing


async def checks_for(db: AsyncSession, documents: list[LoanApplicationDocument]) -> dict[UUID, dict]:
    """The newest automatic check of each signed document, by document id (none for a document never checked,
    such as an agreement signed in the old wizard and not yet opened by staff)."""
    return await latest_checks(db, [d.id for d in documents])


async def signed_copy(db: AsyncSession, application: LoanApplication, kind: Kind) -> tuple[bytes, str]:
    """The signed copy on file, exactly as uploaded, and its file name. 404 when there is none."""
    doc = (await signed_documents(db, application.id)).get(kind.document_type)
    if doc is None:
        raise HTTPException(status_code=404, detail="No signed copy on file")
    content = await run_in_threadpool(get_bytes, doc.file_key, max_bytes=MAX_BYTES)
    # Not the uploaded name: it may hold characters a header cannot carry.
    return content, f"FundLok-{kind.slug}-{application_ref(application.id)}-da-ky.pdf"
