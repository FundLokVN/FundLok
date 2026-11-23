import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.admin.fee_schemas import AdminRoundFeeOut
from app.direct_transfer.schemas import FundingWindowOut, PaymentAccountOut
from app.market.schemas import ProjectedScheduleOut, ProjectionBasisOut

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


# Whether a user accepted the current Terms & Services and data consent
# (app/consent): ACCEPTED, REQUIRED (not yet, or only an older version), or
# NOT_APPLICABLE for admins, who are never asked.
ConsentState = Literal["ACCEPTED", "REQUIRED", "NOT_APPLICABLE"]


class UserRow(BaseModel):
    id: UUID
    email: str
    full_name: str | None
    role: str | None
    status: str
    email_verified: bool
    created_at: datetime | None
    phone: str | None = None
    phone_verified_at: datetime | None = None
    # Filled in by list_users after the page is read (one query per page).
    consent_status: ConsentState = "REQUIRED"
    consent_accepted_at: datetime | None = None

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


class AdminUserRoleRequest(BaseModel):
    """Change an existing account's role (docs/specs/admin/user-role-change.md).

    INVESTOR and SME are here only as demotion targets for an admin account;
    the service refuses them for a member account. No "no role" value: a
    demoted admin is given a member role explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["ADMIN", "SYSTEM_ADMIN", "INVESTOR", "SME"]
    note: str | None = Field(default=None, max_length=500)


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
    """Approve or reject a saved bank account. A note is required either way (D-031): the user sees it as the
    reason for a rejection, and for an approval it records what was checked. To APPROVE, record the account
    holder name exactly as the bank shows it (``verified_holder_name``): an investor's must equal their KYC name;
    for an SME a difference needs ``accept_difference`` and a note saying why."""

    verified_holder_name: str | None = Field(default=None, max_length=200)
    accept_difference: bool = False


class AdminConsentRecordOut(BaseModel):
    """One acceptance record from user_consents, as stored."""

    id: UUID
    version: str
    # {"TERMS": true, "DATA_PROCESSING": true, "DATA_SHARING_TPV": true}
    consents: dict[str, bool]
    verified_by: str
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminUserConsent(BaseModel):
    """The user's Terms & data consent, for the admin user panel."""

    status: ConsentState
    # The version the platform asks for now.
    current_version: str
    # When the current version was accepted (null unless ACCEPTED).
    accepted_at: datetime | None = None
    # Every record, newest first; older versions included.
    records: list[AdminConsentRecordOut] = []


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

    # Terms & Services and data consent (Contract 1).
    consent: AdminUserConsent | None = None
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


class AdminSignatureCheck(BaseModel):
    """A signed listing document (Service Agreement or Standing Offer) as NEAC and FundLok's own checks found it
    (app/loans/agreement/neac_check).

    status: VERIFIED (NEAC accepts the company's signature) | INVALID (NEAC does not; `problems` in its words) |
    NO_SIGNATURE (NEAC found none) | UNAVAILABLE (NEAC could not be reached; check again).
    right_document: the PDF carries this application's reference and the document's version.
    signer_matches: the certificate's MST is the company's (`expected_tax_code`, from its KYB or the project).
    terms_match: the copy carries the check code the document would carry today (the approved terms, assessment
    and parties' details); None for a copy with no check code (signed before codes were printed).
    guarantor_*: Standing Offer only (guarantor_required): a separate valid signature by the Personal Guarantor,
    whose ID is the signing user's eKYC ID number.
    """

    status: str
    checked_at: datetime
    right_document: bool
    application_ref: str
    agreement_version: str
    signer_matches: bool
    name_matches: bool
    expected_tax_code: str | None = None
    expected_name: str | None = None
    expected_source: str | None = None  # KYB | PROJECT
    signer_name: str | None = None
    signer_id: str | None = None  # as on the certificate, e.g. "MST:1501167629"
    issuer_name: str | None = None
    signed_time: str | None = None
    certificate_validity: str | None = None
    ocsp: str | None = None
    crl: str | None = None
    problems: list[str] = []
    neac_message: str | None = None
    document_type: str | None = None
    terms_match: bool | None = None
    check_code: str | None = None
    guarantor_required: bool = False
    guarantor_matches: bool | None = None
    guarantor_expected_id: str | None = None
    guarantor_expected_name: str | None = None
    guarantor_name: str | None = None  # as on the certificate
    guarantor_id: str | None = None  # as on the certificate, e.g. "CCCD:079123456789"
    guarantor_name_matches: bool = False
    guarantor_problems: list[str] = []

    @classmethod
    def from_verdict(cls, verdict: dict) -> "AdminSignatureCheck":
        signer, issuer = verdict.get("signer") or {}, verdict.get("issuer") or {}
        guarantor = verdict.get("guarantor") or None
        g_signer = (guarantor or {}).get("signer") or {}
        required = bool(verdict.get("guarantor_required", guarantor is not None))
        return cls(
            document_type=verdict.get("document_type"),
            terms_match=verdict.get("terms_match"),
            check_code=verdict.get("check_code"),
            guarantor_required=required,
            # An upload's verdict is only recorded when its guarantor checked out.
            guarantor_matches=bool(guarantor.get("matches", True)) if guarantor else (False if required else None),
            guarantor_expected_id=(guarantor or {}).get("expected_id"),
            guarantor_expected_name=(guarantor or {}).get("expected_name"),
            guarantor_name=g_signer.get("cn"),
            guarantor_id=g_signer.get("cccd") or g_signer.get("mst"),
            guarantor_name_matches=bool((guarantor or {}).get("name_matches")),
            guarantor_problems=list((guarantor or {}).get("problems") or []),
            status=verdict["status"],
            checked_at=verdict["checked_at"],
            right_document=bool(verdict.get("right_document")),
            application_ref=verdict.get("application_ref") or "",
            agreement_version=verdict.get("agreement_version") or "",
            signer_matches=bool(verdict.get("signer_matches")),
            name_matches=bool(verdict.get("name_matches")),
            expected_tax_code=verdict.get("expected_tax_code"),
            expected_name=verdict.get("expected_name"),
            expected_source=verdict.get("expected_source"),
            signer_name=signer.get("cn"),
            signer_id=signer.get("mst") or signer.get("cccd"),
            issuer_name=issuer.get("cn"),
            signed_time=verdict.get("signed_time"),
            certificate_validity=verdict.get("certificate_validity"),
            ocsp=verdict.get("ocsp"),
            crl=verdict.get("crl"),
            problems=list(verdict.get("problems") or []),
            neac_message=verdict.get("neac_message"),
        )


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
    # The signed Service Agreement only: the newest NEAC check, null until one has run.
    signature_check: AdminSignatureCheck | None = None

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
    # The project's latest funding window (live or ended), or null if it never opened one. A live project with an
    # approved SME bank account opens on its own; the panel offers to open it when that did not happen.
    funding_window: FundingWindowOut | None = None


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


# --------------------------------------------------------------------------- #
# Transactions tab: funding rounds and investor transfers (app/admin/funding.py)
# --------------------------------------------------------------------------- #
class AdminFundingRoundOut(FundingWindowOut):
    """A funding round as staff see it: its progress plus whose project it is, and how many of its tickets wait
    on staff (a receipt to read, or a review to decide)."""

    project_id: UUID
    project_name: str
    sme_email: str
    sme_full_name: str | None = None
    needs_action: int
    # The round's service fee (3% + VAT), once it closed and the invoice was
    # issued. Null before (app/admin/fees.py).
    fee_invoice: AdminRoundFeeOut | None = None


class AdminReceiverCheckOut(BaseModel):
    """The receiver on the receipt against the business's verified account: the bank as printed and as identified
    in the NAPAS list, the account number, and the holder name against the one the bank showed when the account was
    verified (``expected_holder`` null: never verified at the bank)."""

    bank_read: str | None = None
    bank_identified: str | None = None
    bank_identified_bin: str | None = None
    expected_bank: str | None = None
    expected_bank_bin: str | None = None
    account_read: str | None = None
    expected_account: str | None = None
    holder_read: str | None = None
    expected_holder: str | None = None


class AdminPayerCheckOut(BaseModel):
    """A repayment receipt's payer against the business's account: the holder name as read and as the bank showed it
    when the account was verified (``expected_holder`` null: never verified at the bank)."""

    holder_read: str | None = None
    expected_holder: str | None = None
    expected_account: str | None = None


class AdminSystemCheckOut(BaseModel):
    """What the system made of the receipt (docs/specs/direct_transfer/receipt-ocr.md).

    On a business's repayment receipt (section 10) nothing is ever applied: ACCEPTED means it matches and the
    investor still confirms, NEEDS_REVIEW is left to the investor, and ``payer`` is the business as payer. A
    repayment receipt the system refused was never kept, so it has no row here (the Activity tab shows it).

    ``verdict``: ACCEPTED (read and matched: the ticket was confirmed, its loan waits for staff), DENIED (read and
    did not match: the receipt was rejected) or NEEDS_REVIEW (left for staff). ``applied`` is false when the verdict
    is only a suggestion (``suggested`` says what a confident read would decide). ``failure_codes`` are the checks the
    reading failed (the receiver's: PAYEE_MISMATCH, PAYEE_BANK_MISMATCH, PAYEE_NAME_MISMATCH); ``review_reasons`` say
    why it was left for staff (the reading's notes, PAYEE_BANK_NOT_SHOWN, PAYEE_BANK_UNRECOGNIZED,
    PAYEE_NAME_NOT_SHOWN, PAYEE_NAME_UNVERIFIED, LATE_RECEIPT, AUTO_VERDICT_OFF); ``receiver`` is what was compared. ``fields`` and ``field_confidence`` (0-100,
    null for exact PDF text) are the OCR reading. ``awaiting_review``: accepted, confirmed, and no staff member has
    looked yet (its loan has not started). ``reviewable``: staff may still approve or deny it. ``reviewed_*``: staff's
    review, final."""

    verdict: Literal["ACCEPTED", "DENIED", "NEEDS_REVIEW"]
    applied: bool
    suggested: Literal["ACCEPTED", "DENIED"] | None = None
    summary: str | None = None
    failure_codes: list[str] = []
    review_reasons: list[str] = []
    receiver: AdminReceiverCheckOut | None = None
    payer: AdminPayerCheckOut | None = None
    decided_at: datetime
    engine: str | None = None
    confidence: float | None = None
    fields: dict[str, Any] | None = None
    field_confidence: dict[str, float | None] = {}
    notes: list[str] = []
    awaiting_review: bool = False
    reviewable: bool = False
    reviewed_at: datetime | None = None
    reviewed_by: ActorOut | None = None
    review_outcome: Literal["APPROVED", "DENIED"] | None = None
    review_reason: str | None = None
    overrides_system: bool | None = None


class AdminTransferReceiptOut(BaseModel):
    """The investor's reported transfer. ``parsed_fields`` is what staff (or the system, when it decided) read off
    the receipt (null until then); ``failure_codes`` are the checks it failed against the QR FundLok issued;
    ``system_check`` is the automatic reading and verdict, null when OCR was off for this receipt."""

    id: UUID
    check_status: str
    failure_codes: list[str]
    parsed_fields: dict[str, Any] | None = None
    has_file: bool
    original_filename: str | None = None
    content_type: str | None = None
    created_at: datetime | None = None
    system_check: AdminSystemCheckOut | None = None


class AdminTransferDisputeOut(BaseModel):
    id: UUID
    cause: str
    status: str
    outcome: str | None = None
    statement: str | None = None
    created_at: datetime | None = None


class AdminTransferExpectedOut(BaseModel):
    """What a matching receipt shows: the business's account the QR paid into, and the investor's KYC name."""

    payee_bank_name: str | None = None
    payee_account_number: str | None = None
    payee_account_name: str | None = None
    payee_holder_name: str | None = None  # as the bank showed it when the account was verified
    payer_kyc_name: str


class AdminInvestorTransferOut(BaseModel):
    """One investor ticket and the transfer reported for it. ``needs_action``: a receipt waits to be read, or a
    review waits for a decision."""

    id: UUID
    window_id: UUID
    project_id: UUID
    project_name: str
    investor_id: UUID
    investor_email: str
    investor_full_name: str | None = None
    amount_vnd: int
    reference_code: str
    status: str
    review_cause: str | None = None
    void_reason: str | None = None
    reserved_at: datetime
    hold_expires_at: datetime
    receipt_submitted_at: datetime | None = None
    attested_at: datetime | None = None
    confirmed_at: datetime | None = None
    needs_action: bool
    # Late money (UNDER_REVIEW, LATE_MONEY) staff can still approve into the round, and until when (the round's 15:30).
    can_approve_late_money: bool = False
    approve_late_money_until: datetime | None = None
    transfer: AdminTransferReceiptOut | None = None
    dispute: AdminTransferDisputeOut | None = None
    expected: AdminTransferExpectedOut


class AdminReceiptUrlResponse(BaseModel):
    """A short-lived read URL for an investor's transfer receipt."""

    url: str
    expires_in: int
    content_type: str
    original_filename: str | None = None


class AdminReceiptFieldsRequest(BaseModel):
    """What the receipt shows. Leave a field blank when the receipt does not show it: the checks then fail and
    the ticket goes to review. ``reason`` is recorded with the entry."""

    amount_vnd: int = Field(gt=0, le=10**13)
    transferred_at: datetime
    payer_name: str | None = Field(default=None, max_length=200)
    payer_account: str | None = Field(default=None, max_length=40)
    payee_account: str | None = Field(default=None, max_length=40)
    memo: str | None = Field(default=None, max_length=300)
    bank_txn_id: str | None = Field(default=None, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("transferred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("transferred_at needs a timezone")
        return value


class AdminTransferDecisionRequest(BaseModel):
    """Settle a ticket under review: whether the money reached the business's account."""

    outcome: Literal["RECEIVED", "NOT_RECEIVED"]
    reason: str = Field(min_length=1, max_length=1000)


class AdminLateMoneyApprovalRequest(BaseModel):
    """Approve a Day-14 late-money ticket into the round before 15:30 (late-money-approval.md). A reason is required."""

    reason: str = Field(min_length=1, max_length=1000)


class AdminReceiptDecisionRequest(BaseModel):
    """Approve or deny an investor's transfer after looking at the receipt. The note is optional; an empty one
    records that the decision was made by viewing the receipt."""

    outcome: Literal["APPROVED", "DENIED"]
    reason: str | None = Field(default=None, max_length=1000)


class AdminFundingActivityOut(BaseModel):
    """One entry of the transfers' audit trail: who did what to which ticket, and why. ``check_status`` and
    ``failure_codes`` come with a receipt check (MATCHED: recorded as paid; MISMATCH: sent to review; REJECTED);
    ``reason`` is the note staff gave. ``actor`` is null for an action no signed-in person took."""

    id: UUID
    action: str
    created_at: datetime | None = None
    ip_address: str | None = None
    actor: ActorOut | None = None
    tranche_id: UUID | None = None
    reference_code: str | None = None
    amount_vnd: int | None = None
    project_name: str | None = None
    investor: ActorOut | None = None
    check_status: str | None = None
    failure_codes: list[str] = []
    tranche_status: str | None = None
    reason: str | None = None


class AdminRepaymentAccountOut(BaseModel):
    """A bank account as staff check a repayment receipt against it."""

    bank_name: str
    account_number: str
    account_name: str


class AdminRepaymentDayOut(BaseModel):
    """One repayment day a business's receipt pays, and the transfer recorded for it."""

    repayment_day_id: UUID
    transfer_id: UUID
    kind: Literal["INSTALMENT", "STUB"]
    day_index: int = 0
    due_date: date
    principal_vnd: int
    interest_vnd: int
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int
    status: Literal["DUE", "LATE", "PRESUMED_PAID", "REPORTED_MISSED", "MATCHED"]
    check_status: str


class AdminRepaymentReceiptOut(BaseModel):
    """A receipt the business uploaded for a repayment to one investor, and the days it pays. ``check_status``:
    AWAITING_FIELDS (waits for the investor to confirm it arrived; ``needs_action``), MATCHED (confirmed by the
    investor, or approved by staff before investors confirmed: the days are paid and settled) or REJECTED (denied
    by staff before investors confirmed: the days were due again). Read-only for staff. ``payee`` is the investor's
    account the money should reach, ``payer`` the business's. ``open_report``: the investor reported one of these
    days as never arriving."""

    id: UUID
    tranche_id: UUID
    window_id: UUID
    project_name: str
    reference_code: str
    tranche_amount_vnd: int = 0
    tranche_paid_days: int = 0
    tranche_total_days: int = 0
    business: ActorOut
    investor: ActorOut
    submitted_at: datetime
    check_status: Literal["AWAITING_FIELDS", "MATCHED", "REJECTED"]
    needs_action: bool
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int
    days: list[AdminRepaymentDayOut]
    payee: AdminRepaymentAccountOut | None = None
    payer: AdminRepaymentAccountOut | None = None
    original_filename: str | None = None
    content_type: str | None = None
    open_report: bool = False
    # What the system read off the receipt and how it compares (receipt-ocr.md section 10); null when not read.
    system_check: AdminSystemCheckOut | None = None
    # The ticket's late days (app/admin/repayments.py `overdue_summary`): days the business has not marked paid
    # after their due date. ``tranche_overdue_days`` counts due dates; the amount is what it owes for them (net).
    tranche_overdue_days: int = 0
    tranche_overdue_net_vnd: int = 0
    tranche_oldest_overdue_date: date | None = None
    tranche_working_days_late: int = 0


class AdminOverdueRepaymentOut(BaseModel):
    """One investor ticket the business is late repaying: days still DUE (not marked paid, no receipt) after
    their due date. ``overdue_days`` counts due dates; ``working_days_late`` counts the working days since the
    oldest one (that day included, today not: a day due Monday and unpaid on Tuesday is 1 working day late);
    ``overdue_net_vnd`` is what the business owes the investor for them."""

    tranche_id: UUID
    window_id: UUID
    project_name: str
    reference_code: str
    business: ActorOut
    investor: ActorOut
    overdue_days: int
    oldest_due_date: date
    working_days_late: int
    overdue_net_vnd: int


class AdminOverdueRepaymentsOut(BaseModel):
    """Every late repayment on the platform as of ``as_of`` (today, ICT), most working days late first."""

    as_of: date
    tickets: int
    businesses: int
    overdue_days: int
    overdue_net_vnd: int
    items: list[AdminOverdueRepaymentOut]


class AdminMonthAmountOut(BaseModel):
    month: str  # "YYYY-MM"
    amount_vnd: int


class AdminInvestmentAnalyticsOut(BaseModel):
    """Investments the businesses have received (CONFIRMED or ACTIVE), and those still on their way."""

    invested_vnd: int
    investments: int
    investors: int
    businesses: int
    in_progress_vnd: int  # reserved, signed, or sent and waiting for FundLok's check
    in_progress: int
    monthly: list[AdminMonthAmountOut]  # received per month (ICT), the last 12 months


class AdminRepaymentAnalyticsOut(BaseModel):
    """What the businesses have repaid their investors. A day is one due date of one ticket."""

    repaid_vnd: int  # paid days, principal and interest, the investors' tax included
    principal_repaid_vnd: int
    outstanding_principal_vnd: int
    days_due: int  # days whose due date has passed
    days_on_time: int  # of those, paid and never marked late
    late_days: int  # not marked paid after their due date (the late-repayments list's rule)
    late_tickets: int
    monthly: list[AdminMonthAmountOut]  # paid, by due date, the last 12 months


class AdminFundingAnalyticsOut(BaseModel):
    """Platform-wide totals for the admin overview, as of ``as_of`` (today, ICT). No names or accounts."""

    as_of: date
    investments: AdminInvestmentAnalyticsOut
    repayments: AdminRepaymentAnalyticsOut


class AdminRepaymentPreviewOut(ProjectedScheduleOut):
    """How an investment's repayment is worked out, for the approval panel. PREVIEW: the days it gets if staff
    approve it today (repayment starts the next working day). ACTUAL: the days it got when it was approved.
    ``basis`` walks through each step of the arithmetic."""

    kind: Literal["PREVIEW", "ACTUAL"]
    basis: ProjectionBasisOut
