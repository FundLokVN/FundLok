import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.direct_transfer.schemas import PaymentAccountOut

from app.gverify.normalize import normalize_identifier

T = TypeVar("T")


class AdminMode(str, Enum):
    """Which table the overview screen should load alongside the stats."""

    users = "users"
    projects = "projects"


class Page(BaseModel, Generic[T]):
    """Generic paginated envelope shared by every admin list endpoint."""

    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int


# The bucket users with no role yet are counted under. A real key rather than
# `None`, because JSON object keys cannot be null and `dict[str, int]` rejects
# one outright.
UNASSIGNED_ROLE = "UNASSIGNED"


class AdminStats(BaseModel):
    total_users: int
    total_projects: int
    # Keys are Role values plus UNASSIGNED_ROLE — see get_stats().
    users_by_role: dict[str, int]
    users_by_status: dict[str, int]
    projects_by_status: dict[str, int]


class UserRow(BaseModel):
    id: UUID
    email: str
    full_name: str | None
    # Nullable since f2b3c4d5e6f7: a user has no role until they pick one at
    # /select-role. `str` here made every admin list containing such a user
    # fail validation, which is exactly the state a fresh signup is in.
    role: str | None
    status: str
    email_verified: bool
    created_at: datetime | None

    model_config = {"from_attributes": True}


class ProjectRow(BaseModel):
    id: UUID
    legal_name: str
    tax_id: str | None
    industry: str | None
    status: str
    created_at: datetime | None

    model_config = {"from_attributes": True}


class AdminOverview(BaseModel):
    """BFF payload: the whole admin overview screen in one response."""

    stats: AdminStats
    mode: AdminMode
    table: Page[UserRow] | Page[ProjectRow]


class AdminDecision(str, Enum):
    """What an operator can decide. There is deliberately no way back to
    PENDING — a decision is a recorded event, not a toggle."""

    approved = "APPROVED"
    rejected = "REJECTED"


class AdminDecisionRequest(BaseModel):
    decision: AdminDecision
    # The note lands in the audit log, so it is bounded.
    note: str | None = Field(default=None, max_length=1000)


_PERSON_NUMBER_RE = re.compile(r"^[0-9]{9}([0-9]{3})?$")  # CMND (9) or CCCD (12)
_TAX_CODE_RE = re.compile(r"^[0-9]{10}(-[0-9]{3})?$")  # MST, optionally a branch suffix


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


OVERRIDE_NOTE_MIN_LENGTH = 20


class AdminKycResolveRequest(AdminDecisionRequest):
    """Settle a KYC attempt. The ID fields are for attempts submitted in
    manual verification mode, which have no OCR: approving one needs the ID
    number and name typed from the card, so the one-ID-one-account check and
    later KYB matching have something to compare. They are refused on an
    attempt the provider read, whose details must stay the provider's.

    ``acknowledged_conflict_ids`` is the reviewer's explicit acknowledgement
    that this ID number is already verified on other accounts. It names the
    conflicting verifications (as listed by a refused first approval), and is
    accepted only with a substantive note — enforced here, not by convention.
    """

    person_number: str | None = Field(default=None, max_length=32)
    full_name: str | None = Field(default=None, max_length=120)
    date_of_birth: str | None = Field(default=None, max_length=20)
    acknowledged_conflict_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @field_validator("person_number")
    @classmethod
    def _check_person_number(cls, value: str | None) -> str | None:
        value = _clean_optional(value)
        if value is None:
            return None
        value = normalize_identifier(value)
        if not _PERSON_NUMBER_RE.match(value):
            raise ValueError("person_number must be a 9-digit CMND or 12-digit CCCD number")
        return value

    @field_validator("full_name", "date_of_birth")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _clean_optional(value)

    @model_validator(mode="after")
    def _acknowledgement_needs_a_note(self) -> "AdminKycResolveRequest":
        if self.acknowledged_conflict_ids:
            if self.decision is not AdminDecision.approved:
                raise ValueError("acknowledged_conflict_ids only applies to an approval")
            if len((self.note or "").strip()) < OVERRIDE_NOTE_MIN_LENGTH:
                raise ValueError(
                    "acknowledging an ID conflict needs a note of at least "
                    f"{OVERRIDE_NOTE_MIN_LENGTH} characters explaining the override"
                )
        return self


class AdminKybResolveRequest(AdminDecisionRequest):
    """Settle a KYB attempt. For a manual-mode attempt (no OCR, no registry
    check) approving needs the business name and tax code as printed on the
    certificate; the SME's own declaration is only a starting point."""

    business_name: str | None = Field(default=None, max_length=300)
    tax_code: str | None = Field(default=None, max_length=32)
    representative_id_numbers: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("representative_id_numbers")
    @classmethod
    def _clean_rep_ids(cls, values: list[str]) -> list[str]:
        cleaned = []
        for v in values:
            norm = normalize_identifier(v)
            if norm:
                cleaned.append(norm)
        return cleaned

    @field_validator("tax_code")
    @classmethod
    def _check_tax_code(cls, value: str | None) -> str | None:
        value = _clean_optional(value)
        if value is None:
            return None
        value = normalize_identifier(value)
        if not _TAX_CODE_RE.match(value):
            raise ValueError("tax_code must be a 10-digit MST, optionally -NNN for a branch")
        return value

    @field_validator("business_name")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _clean_optional(value)


class AdminUserStatusRequest(BaseModel):
    """Set an account's status. PENDING_KYC is included because an operator
    may need to put an account back into onboarding, not only suspend it."""

    status: Literal["ACTIVE", "PENDING_KYC", "SUSPENDED"]
    note: str | None = None


class AdminInviteRequest(BaseModel):
    """Create an ADMIN account and email the invitee a set-password link.

    There is deliberately no `role` field: the endpoint only ever creates
    ADMIN accounts (a SYSTEM_ADMIN is provisioned out of band), and
    extra="forbid" turns an attempt to send one into a 422 rather than a
    silently ignored field. No password either: the invitee chooses their own,
    so nobody else ever knows it.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)

    @field_validator("full_name")
    @classmethod
    def _strip_full_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("full_name must not be blank")
        return value


class AdminUserDetail(BaseModel):
    id: UUID
    email: str
    full_name: str | None
    role: str | None
    status: str
    email_verified: bool
    created_at: datetime | None

    model_config = {"from_attributes": True}


class AdminUserProjectRef(BaseModel):
    """One company this user owns or co-owns, via project_ownerships."""

    id: UUID
    legal_name: str
    tax_id: str | None = None
    industry: str | None = None
    status: str
    # The role this user holds on the company (OWNER today; the column is free
    # text, so it is passed through rather than assumed).
    ownership_role: str
    employee_count: int | None = None
    applications_count: int = 0
    total_requested_amount: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AdminUserInvestmentSummary(BaseModel):
    """The investor half of an account, aggregated.

    Amounts are int VND for the same reason AdminLoanApplicationRow.
    requested_amount is one: the column is Numeric(20, 0) and a float would
    start losing whole dong somewhere above 2^53. Zero counts are real answers
    (an SME account has no orders), not missing data.
    """

    orders_count: int
    orders_amount: int
    holdings_count: int
    holdings_principal: int


class AdminPaymentAccountOut(PaymentAccountOut):
    """A saved bank account as an admin reviews it: whose it is, in full, and
    who decided it. ``approved_by_email`` is the member of staff who approved
    or rejected it; None while PENDING and for accounts approved on save while
    there was no review step (approved_by NULL)."""

    user_id: UUID
    user_email: str
    user_full_name: str | None = None
    approved_by_email: str | None = None


class AdminPaymentAccountDecisionRequest(AdminDecisionRequest):
    """Approve or reject a saved bank account. A rejection needs a note (the
    user sees it as the reason); an approval may have one."""


class AdminUserFullDetail(AdminUserDetail):
    """The user preview panel's payload, in the spirit of AdminProjectDetail:
    the whole panel in one response instead of the client stitching a list row
    together with per-section calls.

    Everything sensitive that a panel does not need stays out — no
    password_hash, no totp_secret, no recovery codes; only their existence.
    ``has_password`` is derived rather than stored: Google-OAuth users and
    freshly invited admins have NO password at all, and an operator about to
    suspend an account should know whether its owner could ever sign back in.
    """

    phone: str | None = None
    bio: str | None = None
    avatar_key: str | None = None
    email_signature: str | None = None
    has_password: bool
    # "A new device signed in" emails, on by default — see migration 5e1f0b7c9a24.
    signin_alerts_enabled: bool
    totp_enabled: bool
    totp_confirmed_at: datetime | None = None
    onboarding_tour_completed_at: datetime | None = None
    updated_at: datetime | None = None

    # Security & session insights
    passkeys_count: int = 0
    active_sessions_count: int = 0
    last_sign_in_at: datetime | None = None
    unused_recovery_codes_count: int = 0

    # Verification & banking insights
    kyc_status: str | None = None
    # The mock app/banking table (linked_accounts), kept for older clients.
    linked_accounts_count: int = 0
    # The bank accounts the user saved on their profile (payment_accounts),
    # oldest first: what investors pay into and repayments go back to. Admin
    # only, so the full number is shown. Empty when they have saved none.
    payment_accounts: list[AdminPaymentAccountOut] = []

    # Empty for a user with no project ownerships, not an error.
    projects: list[AdminUserProjectRef] = []
    investments: AdminUserInvestmentSummary


class AdminKybVerificationOut(BaseModel):
    """The verification ENGINE's half of the two-approval gate.

    `status` is the provider's own verdict. MANUAL_REVIEW is the one an
    operator can act on: borderline OCR confidence or a minor registry
    mismatch parks here instead of hard-rejecting.
    """

    id: UUID
    status: str
    is_approved: bool
    rejection_reason: str | None
    business_name: str | None
    tax_code: str | None
    updated_at: datetime | None

    model_config = {"from_attributes": True}


# --- KYC manual review ------------------------------------------------------ #


class AdminKycAccountRef(BaseModel):
    """The account an attempt belongs to, as a reviewer needs to recognise it."""

    id: UUID
    email: str
    full_name: str | None
    status: str
    created_at: datetime | None


class AdminKycConflict(BaseModel):
    """Another account already APPROVED with the same ID number: the reason an
    attempt is parked. A reviewer compares the two before deciding."""

    verification_id: UUID
    user: AdminKycAccountRef
    full_name: str | None  # the name OCR read on THAT account's card
    date_of_birth: str | None
    approved_at: datetime | None


class AdminKycVerificationOut(BaseModel):
    """One GVerify KYC attempt, with everything needed to review it.

    `rejection_reason` is what the investor sees. `conflicts` is recomputed on
    every read rather than stored, so it reflects the accounts as they are now.
    """

    id: UUID
    status: str
    is_approved: bool
    rejection_reason: str | None
    person_number: str | None
    full_name: str | None
    date_of_birth: str | None
    face_match_score: float | None
    created_at: datetime | None
    user: AdminKycAccountRef
    conflicts: list[AdminKycConflict]
    # False when the attempt was submitted in manual verification mode: no
    # OCR or face match ran, and the reviewer must read the card themselves.
    provider_checked: bool = True
    # False while the attempt has no ID number yet (manual mode, before the
    # reviewer types it): `conflicts` is then empty because nothing was
    # checked, NOT because the ID is clear. The reviewer's first approval
    # runs the check and is refused with the conflicts if there are any.
    conflicts_evaluated: bool = True


class AdminKybReviewOut(BaseModel):
    """One KYB attempt with what a reviewer needs beside the certificate.

    `kyc_full_name` / `kyc_person_number` come from the SME's own approved
    identity check (if any), so the reviewer can confirm that person appears
    on the certificate as a legal representative. `projects` names the
    companies this account has registered on FundLok.
    """

    id: UUID
    status: str
    is_approved: bool
    rejection_reason: str | None
    document_type: str
    tax_code: str | None
    license_code: str | None
    business_name: str | None
    business_type: str | None
    company_address: str | None
    provider_checked: bool
    created_at: datetime | None
    updated_at: datetime | None
    user: AdminKycAccountRef
    kyc_full_name: str | None
    kyc_person_number: str | None
    projects: list[str]


class AdminKybCertificateUrlResponse(BaseModel):
    """A short-lived read URL for a submitted business registration certificate."""

    url: str
    expires_in: int
    content_type: str


class AdminKycImageUrlResponse(BaseModel):
    """A short-lived read URL for one submitted KYC image."""

    url: str
    expires_in: int
    content_type: str


class AdminApplicationDocument(BaseModel):
    """One file the SME uploaded on the loan-application wizard.

    Metadata only — there is no download endpoint yet, so an operator can see
    WHAT was supplied (and that it actually landed in storage) without the
    panel pretending to offer the file itself.

    `status` is PENDING while only a presign has been issued and UPLOADED once
    the object was HEAD-checked in R2; a stuck PENDING is a real signal that
    the upload never completed.
    """

    id: UUID
    document_type: str
    original_filename: str
    content_type: str | None
    file_size_bytes: int | None
    status: str
    uploaded_at: datetime | None

    model_config = {"from_attributes": True}


class AdminDocumentUrlResponse(BaseModel):
    """A short-lived read URL for one uploaded application document."""

    url: str
    expires_in: int
    content_type: str | None
    original_filename: str


class AdminScoreRunOut(BaseModel):
    """What the grading engine produced for one application.

    Read from `score_runs`, which already records the whole result — no new
    columns. `final_grade_precise` is the real value; `overall_score` is a
    legacy 2dp display column (D24) and is deliberately not surfaced.

    `decision` is the grading OUTCOME (APPROVED | REVIEW | REJECT |
    INSUFFICIENT_DATA | AI_PENDING), distinct from `status`, which is the run's
    own lifecycle (RUNNING | READY | LOCKED | FAILED). An operator needs both:
    a LOCKED run that decided REVIEW is not an approval.
    """

    id: UUID
    status: str
    decision: str | None
    final_grade: float | None
    interest_rate_pct: float | None
    engine_version: str | None
    params_version: str | None
    created_at: datetime | None
    # On an INSUFFICIENT_DATA run: the grading inputs still missing now
    # (app/loans/grading_inputs.py blockers), so the operator sees why there
    # is no score and what a re-run needs. Empty otherwise.
    missing_inputs: list[str] = []

    model_config = {"from_attributes": True}


class AdminLoanApplicationRow(BaseModel):
    """One funding request a company has made, as the admin preview shows it.

    `requested_amount` is an int for the same reason LoanApplicationOut makes it
    one: the column is Numeric(20, 0) VND, and a float would start losing whole
    dong somewhere above 2^53.
    """

    id: UUID
    requested_amount: int
    purpose: str | None
    repayment_preference: str | None
    status: str
    # The operator half of the gate: PENDING until someone decides.
    admin_approval: str
    submitted_at: datetime | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime | None
    documents: list[AdminApplicationDocument]
    score_run: AdminScoreRunOut | None = None
    # Required documents this request does not have yet (document_type keys),
    # in wizard order. Approval is refused while any is listed.
    missing_documents: list[str] = []

    model_config = {"from_attributes": True}


class AdminProjectOwnerOut(BaseModel):
    """The account that owns the company, so the operator can open it and its
    identity check from the project preview instead of searching for it."""

    id: UUID
    email: str
    full_name: str | None
    phone: str | None
    role: str | None
    status: str
    created_at: datetime | None
    # The owner's latest personal KYC attempt, or null if they never started one.
    kyc_verification_id: UUID | None = None
    kyc_status: str | None = None


class AdminProjectDetail(BaseModel):
    """BFF payload for the admin's project preview, in the same spirit as
    AdminOverview: the whole panel in one response rather than the client
    stitching a project row together with a separate applications call.

    `address` is the JSONB the SME filled in on the project application, passed
    through as-is — it has no server-side schema, so shaping it here would only
    invent one.
    """

    id: UUID
    legal_name: str
    tax_id: str | None
    industry: str | None
    status: str
    address: dict[str, Any] | None
    incorporation_date: date | None
    created_at: datetime | None
    applications: list[AdminLoanApplicationRow]
    # The owner's latest KYB attempt, or null if they never started one. The
    # preview shows both halves of the gate side by side, so the operator can
    # see WHY an application is stuck without leaving the panel.
    kyb: AdminKybVerificationOut | None
    # Null only for a project with no ownership row, which creation never makes.
    owner: AdminProjectOwnerOut | None = None


class ActorOut(BaseModel):
    """Minimal user info attached to an audit log (actor / USER entity)."""

    id: UUID
    full_name: str | None
    email: str | None

    model_config = {"from_attributes": True}


class AuditLogOut(BaseModel):
    id: UUID
    entity_type: str
    entity_id: UUID | None
    action: str
    actor_id: UUID | None
    actor: ActorOut | None = None
    entity_user: ActorOut | None = None  # populated only when entity_type == 'USER'
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    ip_address: str | None
    created_at: datetime | None

    model_config = {"from_attributes": True}
