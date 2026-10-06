from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# 10 MiB raw is ~13,981,013 chars in base64. 14,000,000 provides a safe outer bound
# to reject multi-megabyte / gigabyte payloads before base64 decoding into memory.
MAX_B64_CHARS = 14_000_000


class KycVerifyRequest(BaseModel):
    """Body for POST /gverify/kyc/verify.

    Images are base64 strings (JPEG or PNG, decoded size <= 10MB each),
    mirroring GVerify's own base64 endpoints.
    """

    id_front_b64: str = Field(..., max_length=MAX_B64_CHARS)
    id_back_b64: str = Field(..., max_length=MAX_B64_CHARS)
    portrait_b64: str = Field(..., max_length=MAX_B64_CHARS)
    expected_mode: str | None = Field(default=None, max_length=20)


class KycVerifyResponse(BaseModel):
    """Synchronous verdict for a verification attempt (APPROVED or REJECTED)."""

    verification_id: UUID
    status: str
    is_approved: bool
    rejection_reason: str | None = None
    person_number: str | None = None
    full_name: str | None = None
    date_of_birth: str | None = None
    face_match_score: float | None = None
    created_at: datetime | None = None
    mode: str | None = None


class HandoffResponse(BaseModel):
    """Short-lived token for the phone capture flow (encoded in a QR)."""

    token: str
    expires_in_seconds: int
    mode: str | None = None


class KycStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    verification_id: UUID
    status: str
    is_terminal: bool
    is_approved: bool
    rejection_reason: str | None = None
    person_number: str | None = None
    full_name: str | None = None
    updated_at: datetime | None = None


class KybRepresentative(BaseModel):
    """Slimmed legal-representative entry from the OCR X payload."""

    name: str | None = None
    id_number: str | None = None
    title: str | None = None


class KybVerifyRequest(BaseModel):
    """Body for POST /gverify/kyb/verify.

    The business registration certificate as base64 (JPEG, PNG or PDF,
    decoded size <= 10MB). document_type selects the OCR X variant:
    COMPANY | COMPANY_BRANCH | HOUSEHOLD (validated in the service so an
    unknown value returns the spec'd 400, not a 422).
    """

    document_b64: str = Field(..., max_length=MAX_B64_CHARS)
    document_type: str = Field(..., max_length=50)
    # Optional declared MST — fallback when OCR fails to extract the tax code
    # from the certificate (seen live 2026-07-15: legible "Mã số doanh nghiệp"
    # line returned as tax_code:""). Untrusted, but bound by the registry
    # name cross-check: the registry's official name for this MST must still
    # match the OCR'd certificate name.
    tax_code: str | None = Field(default=None, max_length=32)
    # Optional declared business-registration / licence code — part of the
    # tax-verify working contract (KYB plan §7.3). OCR rarely returns it, so
    # the SME may supply it; falls back to the tax code when absent.
    license_code: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9./_-]{0,31}$",
    )
    expected_mode: str | None = Field(default=None, max_length=20)


class KybVerifyResponse(BaseModel):
    """Synchronous verdict for a KYB attempt (APPROVED or REJECTED)."""

    verification_id: UUID
    status: str
    is_approved: bool
    rejection_reason: str | None = None
    # Machine-readable cause, when defined (e.g. NOT_A_REPRESENTATIVE).
    rejection_code: str | None = None
    tax_code: str | None = None
    license_code: str | None = None
    business_name: str | None = None
    business_type: str | None = None
    business_status: str | None = None  # from the tax registry, when reached
    representatives: list[KybRepresentative] = []
    created_at: datetime | None = None
    mode: str | None = None


class KybStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    verification_id: UUID
    status: str
    is_terminal: bool
    is_approved: bool
    rejection_reason: str | None = None
    # Machine-readable cause, when defined (e.g. NOT_A_REPRESENTATIVE).
    rejection_code: str | None = None
    tax_code: str | None = None
    business_name: str | None = None
    # Verbatim from the certificate OCR (see spec §2: the 63->34 Decode Address
    # API is out of scope, so this is one free-text line, not structured
    # parts). Exposed so the SME's project application can prefill the details
    # already read off the certificate instead of asking them to retype.
    company_address: str | None = None
    date_of_establishment: str | None = None
    updated_at: datetime | None = None


class KybCertificateResponse(BaseModel):
    """A short-lived read URL for the certificate the SME already submitted
    for KYB, so the loan application does not ask for the same file twice."""

    url: str
    # Seconds the URL stays valid. Surfaced so the client can say so rather
    # than handing the user a link that silently dies.
    expires_in: int
    content_type: str
    verification_id: UUID
