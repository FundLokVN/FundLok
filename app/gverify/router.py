"""HTTP router for GVerify (Datatrust) eKYC — direct-API KYC verification.

Spec: docs/specs/gverify/ekyc-kyc-verification.md

Parallel to (not replacing) the Didit `/kyc/*` flow in app/verification/.
GVerify has no hosted UI and no webhook: the investor submits ID images +
portrait in one request and the response carries the final verdict.

Two ways to submit:
  - POST /gverify/kyc/verify          — cookie/Bearer session (same device)
  - POST /gverify/kyc/handoff         — mint a 10-minute handoff token (QR)
    POST /gverify/kyc/handoff/verify  — submit from the phone with that token
"""
from uuid import UUID

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.consent import service as consent_service
from app.core.config import settings
from app.core.database import get_db
from app.gverify import kyb_service, service
from app.gverify import storage
from app.system import service as system_service
from app.gverify.client import GVerifyError
from app.gverify.models import STATUS_APPROVED, STATUS_MANUAL_REVIEW
from app.notifications.models import VERIFICATION_CHECK_KYB, VERIFICATION_CHECK_KYC
from app.notifications.service import resolve_verification_reminder
from app.gverify.schemas import (
    HandoffResponse,
    KybRepresentative,
    KybStatusResponse,
    KybVerifyRequest,
    KybCertificateResponse,
    KybVerifyResponse,
    KycStatusResponse,
    KycVerifyRequest,
    KycVerifyResponse,
)
from app.gverify.service import ImageValidationError
from app.gverify.tokens import (
    HANDOFF_TTL,
    INVALID_HANDOFF_DETAIL,
    HandoffClaims,
    create_handoff_token,
    verify_handoff_token,
)
from app.users.models import USER_STATUS_SUSPENDED, Role, User
from app.utils.audit import append_audit
from app.utils.rbac import require_roles
from app.direct_transfer import standing as lender_standing

logger = logging.getLogger(__name__)

# Returned with every 502. Deliberately generic: the underlying message can
# carry provider error bodies or resolver details ("[Errno 8] ...").
PROVIDER_FAILED_DETAIL = "The verification provider could not process the request. Please try again."
# Manual verification mode, when the documents could not be stored: nothing
# would be left for an admin to review, so the submission is not accepted.
STORAGE_UNAVAILABLE_DETAIL = "Verification is temporarily unavailable. Please try again later."
ATTEMPT_LIMIT_DETAIL = (
    "Too many verification attempts in the last 24 hours. "
    "Please try again tomorrow or contact support."
)

# A submission that ends here has done what the "verify first" notification
# (written at role selection) asked: approved, or with the review team and out
# of the user's hands. It is marked read so the bell stops asking. A rejection
# or failure leaves it: they still have something to do.
_REMINDER_DONE_STATUSES = (STATUS_APPROVED, STATUS_MANUAL_REVIEW)


async def _enforce_attempt_limit(db: AsyncSession, user_id, kind: str) -> None:
    """429 once the user has used today's attempts. Caller holds the per-user lock."""
    if await service.attempts_in_last_day(db, user_id, kind) >= settings.GVERIFY_MAX_ATTEMPTS_PER_DAY:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=ATTEMPT_LIMIT_DETAIL)


router = APIRouter(prefix="/gverify/kyc", tags=["gverify-kyc"])

# Investors verify themselves here; SMEs do the same KYC as a step of KYB
# (their CCCD is matched against the certificate's legal representatives).
_authorized = require_roles(Role.INVESTOR, Role.SME)

# KYB — business verification for SMEs (spec: ekyb-kyb-verification.md).
kyb_router = APIRouter(prefix="/gverify/kyb", tags=["gverify-kyb"])

_sme_authorized = require_roles(Role.SME)


async def _run_verification(
    request: Request,
    body: KycVerifyRequest,
    db: AsyncSession,
    user: User,
    handoff: HandoffClaims | None = None,
) -> KycVerifyResponse:
    await consent_service.require_consent(db, user)
    await service.lock_user_verification(db, user.id, "KYC")
    latest = await service.latest_for_user(db, user.id)
    # A handoff token is spent once any attempt has been made since it was
    # minted. Checked under the lock, so two submissions racing on one token
    # cannot both find it unspent.
    if handoff is not None and handoff.previous_attempt_id != (str(latest.id) if latest else ""):
        raise HTTPException(status_code=401, detail=INVALID_HANDOFF_DETAIL)
    # An approved investor verifies again only to unlock after a Day-14 lock (Contract 4 Art. 5.4).
    if latest is not None and latest.is_approved and not await lender_standing.needs_fresh_ekyc(db, user.id):
        raise HTTPException(status_code=409, detail="KYC is already approved")
    # A parked attempt waits for a person. Resubmitting would only park again,
    # and every attempt is two billable provider calls.
    if latest is not None and latest.status == STATUS_MANUAL_REVIEW:
        raise HTTPException(status_code=409, detail="KYC is under manual review")
    await _enforce_attempt_limit(db, user.id, "KYC")

    # Read per submission, uncached: a switch to MANUAL must stop provider
    # calls from the very next request.
    current_mode_state = await system_service.get_verification_mode(db)
    current_mode = current_mode_state.mode
    if body.expected_mode and body.expected_mode.upper() != current_mode.upper():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VERIFICATION_MODE_CHANGED",
                "message": f"Verification mode changed to {current_mode}",
                "mode": current_mode,
            },
        )
    manual = current_mode.upper() == "MANUAL"
    try:
        attempt = await service.run_kyc_verification(
            db,
            user,
            id_front_b64=body.id_front_b64,
            id_back_b64=body.id_back_b64,
            portrait_b64=body.portrait_b64,
            manual=manual,
        )
    except ImageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except storage.StorageUnavailable as exc:
        user_id = user.id  # read before the rollback expires the instance
        await db.rollback()
        logger.error("KYC documents for user %s could not be stored in manual mode: %s", user_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORAGE_UNAVAILABLE_DETAIL)
    except GVerifyError as exc:
        # The FAILED attempt row is already flushed — commit so it's recorded.
        await db.commit()
        # The provider's own error text (and httpx/DNS details) goes to the
        # log and the attempt row, never to the client.
        logger.warning("GVerify call failed for user %s: %s", user.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=PROVIDER_FAILED_DETAIL)

    append_audit(
        db,
        entity_type="GVERIFY_KYC_VERIFICATION",
        entity_id=attempt.id,
        action="VERIFY",
        actor_id=user.id,
        after_state={
            "status": attempt.status,
            "person_number": attempt.person_number,
            # Internal only (never shown to the user): tells a reviewer why
            # the attempt was parked.
            **(
                {"review_cause": service.manual_review_cause(attempt, manual=manual)}
                if attempt.status == STATUS_MANUAL_REVIEW
                else {}
            ),
        },
        ip_address=request.client.host if request.client else None,
    )
    if attempt.status in _REMINDER_DONE_STATUSES:
        await resolve_verification_reminder(
            db, user_id=user.id, check=VERIFICATION_CHECK_KYC
        )
    await db.commit()
    await db.refresh(attempt)

    return KycVerifyResponse(
        verification_id=attempt.id,
        status=attempt.status,
        is_approved=attempt.is_approved,
        rejection_reason=attempt.rejection_reason,
        person_number=attempt.person_number,
        full_name=attempt.full_name,
        date_of_birth=attempt.date_of_birth,
        face_match_score=service.face_match_score(attempt),
        created_at=attempt.created_at,
        mode=current_mode,
    )


@router.post("/verify", response_model=KycVerifyResponse, status_code=201)
async def verify(
    request: Request,
    body: KycVerifyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_authorized),
):
    """One-shot KYC: OCR the ID card, face-match the portrait, return the verdict.

    A REJECTED outcome is still 201 — the business verdict is in the body.
    Only provider failures surface as 502 (the attempt is persisted FAILED).
    """
    return await _run_verification(request, body, db, current_user)


@router.post("/handoff", response_model=HandoffResponse, status_code=201)
async def create_handoff(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_authorized),
):
    """Mint a short-lived token so the user can continue on their phone.

    The frontend encodes it in a QR; the phone submits the images to
    /gverify/kyc/handoff/verify with `Authorization: Bearer <token>`. The
    token authorizes ONLY that submission, ONE attempt (a retry after a
    rejection needs a new QR), and expires after 10 minutes.
    """
    current_mode_state = await system_service.get_verification_mode(db)
    latest = await service.latest_for_user(db, current_user.id)
    return HandoffResponse(
        token=create_handoff_token(current_user.id, latest.id if latest else None),
        expires_in_seconds=int(HANDOFF_TTL.total_seconds()),
        mode=current_mode_state.mode,
    )


@router.post("/handoff/verify", response_model=KycVerifyResponse, status_code=201)
async def verify_via_handoff(
    request: Request,
    body: KycVerifyRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
):
    """Same as /verify, authenticated by a handoff token instead of a session.

    Used by the phone after scanning the desktop QR — the phone has no login
    cookie, only the token minted by /handoff.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing handoff token")
    handoff = verify_handoff_token(authorization.split(" ", 1)[1].strip())

    result = await db.execute(select(User).where(User.id == UUID(handoff.user_id)))
    user = result.scalar_one_or_none()
    if user is None or user.role not in (Role.INVESTOR.value, Role.SME.value):
        raise HTTPException(status_code=401, detail=INVALID_HANDOFF_DETAIL)
    # This route authenticates by handoff token, so it never passes through
    # get_current_user and does NOT inherit the suspension check from there.
    # Without this, a suspended investor's phone could still submit a
    # verification using a token minted before the suspension.
    if user.status == USER_STATUS_SUSPENDED:
        raise HTTPException(status_code=403, detail="This account has been suspended")

    return await _run_verification(request, body, db, user, handoff=handoff)


@router.get("/status", response_model=KycStatusResponse)
async def get_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_authorized),
):
    """Return the caller's latest GVerify KYC attempt, or NOT_STARTED if none exists."""
    attempt = await service.latest_for_user(db, current_user.id)
    if attempt is None:
        return KycStatusResponse(
            verification_id=None,
            status="NOT_STARTED",
            is_terminal=False,
            is_approved=False,
        )
    return KycStatusResponse(
        verification_id=attempt.id,
        status=attempt.status,
        is_terminal=attempt.is_terminal,
        is_approved=attempt.is_approved,
        rejection_reason=attempt.rejection_reason,
        person_number=attempt.person_number,
        full_name=attempt.full_name,
        updated_at=attempt.updated_at,
        reverify_required=attempt.is_approved and await lender_standing.needs_fresh_ekyc(db, current_user.id),
    )


# --------------------------------------------------------------------------- #
# KYB — business verification (SME)
# --------------------------------------------------------------------------- #


@kyb_router.post("/verify", response_model=KybVerifyResponse, status_code=201)
async def kyb_verify(
    request: Request,
    body: KybVerifyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_sme_authorized),
):
    """One-shot KYB: OCR the registration certificate, cross-check the tax
    code against the state registry, return the verdict.

    A REJECTED outcome is still 201 — the business verdict is in the body.
    Only provider failures surface as 502 (the attempt is persisted FAILED).
    """
    await consent_service.require_consent(db, current_user)
    await service.lock_user_verification(db, current_user.id, "KYB")
    latest = await kyb_service.latest_for_user(db, current_user.id)
    if latest is not None and latest.is_approved:
        raise HTTPException(status_code=409, detail="KYB is already approved")
    # As for KYC: a parked attempt waits for a person, and in manual mode every
    # resubmission would add another row to the admin queue.
    if latest is not None and latest.status == STATUS_MANUAL_REVIEW:
        raise HTTPException(status_code=409, detail="KYB is under manual review")
    await _enforce_attempt_limit(db, current_user.id, "KYB")
    # Rule 7 needs the SME's own verified identity. Refuse up front rather than
    # after the (billable) certificate OCR, which could only end in rejection.
    if settings.GVERIFY_KYB_REQUIRE_REP_MATCH and not await kyb_service.approved_kyc_person_number(
        db, current_user.id
    ):
        raise HTTPException(
            status_code=409,
            detail=kyb_service.KYC_REQUIRED_REASON,
            headers={"X-Error-Code": "KYC_REQUIRED"},
        )

    current_mode_state = await system_service.get_verification_mode(db)
    current_mode = current_mode_state.mode
    if body.expected_mode and body.expected_mode.upper() != current_mode.upper():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VERIFICATION_MODE_CHANGED",
                "message": f"Verification mode changed to {current_mode}",
                "mode": current_mode,
            },
        )
    manual = current_mode.upper() == "MANUAL"
    try:
        attempt = await kyb_service.run_kyb_verification(
            db,
            current_user,
            document_b64=body.document_b64,
            document_type=body.document_type,
            declared_tax_code=body.tax_code,
            declared_license_code=body.license_code,
            manual=manual,
        )
    except ImageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except storage.StorageUnavailable as exc:
        user_id = current_user.id  # read before the rollback expires the instance
        await db.rollback()
        logger.error("KYB certificate for user %s could not be stored in manual mode: %s", user_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORAGE_UNAVAILABLE_DETAIL)
    except GVerifyError as exc:
        # The FAILED attempt row is already flushed — commit so it's recorded.
        await db.commit()
        # The provider's own error text (and httpx/DNS details) goes to the
        # log and the attempt row, never to the client.
        logger.warning("GVerify call failed for user %s: %s", current_user.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=PROVIDER_FAILED_DETAIL)

    append_audit(
        db,
        entity_type="GVERIFY_KYB_VERIFICATION",
        entity_id=attempt.id,
        action="VERIFY",
        actor_id=current_user.id,
        after_state={
            "status": attempt.status,
            "tax_code": attempt.tax_code,
            **({"review_cause": "MANUAL_VERIFICATION_MODE"} if manual else {}),
        },
        ip_address=request.client.host if request.client else None,
    )
    if attempt.status in _REMINDER_DONE_STATUSES:
        await resolve_verification_reminder(
            db, user_id=current_user.id, check=VERIFICATION_CHECK_KYB
        )
    await db.commit()
    await db.refresh(attempt)

    return KybVerifyResponse(
        verification_id=attempt.id,
        status=attempt.status,
        is_approved=attempt.is_approved,
        rejection_reason=attempt.rejection_reason,
        rejection_code=kyb_service.rejection_code(attempt),
        tax_code=attempt.tax_code,
        license_code=attempt.license_code,
        business_name=attempt.business_name,
        business_type=attempt.business_type,
        business_status=kyb_service.business_status(attempt),
        representatives=[
            KybRepresentative(
                name=rep.get("name"),
                # Live payloads use identity_number/position; the API doc said
                # id_number/title — accept both.
                id_number=rep.get("identity_number") or rep.get("id_number"),
                title=rep.get("position") or rep.get("title"),
            )
            for rep in (attempt.representatives or [])
        ],
        created_at=attempt.created_at,
        mode=current_mode,
    )


@kyb_router.get("/certificate", response_model=KybCertificateResponse)
async def kyb_certificate(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_sme_authorized),
):
    """A 10-minute read URL for the caller's OWN business registration
    certificate, as submitted for KYB.

    Scoped to the caller by construction: the attempt is looked up by
    current_user.id, never by an id from the request, so there is no key to
    tamper with. The object stays private — a presigned URL is the only way to
    reach it, and it expires.
    """
    attempt = await kyb_service.latest_for_user(db, current_user.id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="No KYB verification found")

    located = storage.find_kyb_certificate(attempt.id)
    if located is None:
        # Retention is best-effort, so an attempt can legitimately have no
        # stored file. 404 lets the client fall back to asking for an upload
        # instead of rendering a broken preview.
        raise HTTPException(status_code=404, detail="No stored certificate for this verification")

    key, content_type = located
    return KybCertificateResponse(
        url=storage.presign_kyb_certificate(key),
        expires_in=storage.CERTIFICATE_URL_TTL_SECONDS,
        content_type=content_type,
        verification_id=attempt.id,
    )


@kyb_router.get("/status", response_model=KybStatusResponse)
async def kyb_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_sme_authorized),
):
    """Return the caller's latest GVerify KYB attempt, or NOT_STARTED if none exists."""
    attempt = await kyb_service.latest_for_user(db, current_user.id)
    if attempt is None:
        return KybStatusResponse(
            verification_id=None,
            status="NOT_STARTED",
            is_terminal=False,
            is_approved=False,
        )
    return KybStatusResponse(
        verification_id=attempt.id,
        status=attempt.status,
        is_terminal=attempt.is_terminal,
        is_approved=attempt.is_approved,
        rejection_reason=attempt.rejection_reason,
        rejection_code=kyb_service.rejection_code(attempt),
        tax_code=attempt.tax_code,
        business_name=attempt.business_name,
        company_address=attempt.company_address,
        date_of_establishment=attempt.date_of_establishment,
        updated_at=attempt.updated_at,
    )
