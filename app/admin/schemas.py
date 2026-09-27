from datetime import date, datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel

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
    note: str | None = None


class AdminUserStatusRequest(BaseModel):
    """Set an account's status. PENDING_KYC is included because an operator
    may need to put an account back into onboarding, not only suspend it."""

    status: Literal["ACTIVE", "PENDING_KYC", "SUSPENDED"]
    note: str | None = None


class AdminUserDetail(BaseModel):
    id: UUID
    email: str
    full_name: str | None
    role: str | None
    status: str
    email_verified: bool
    created_at: datetime | None

    model_config = {"from_attributes": True}


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
    # The wizard's uploads (legal charter, business registration, e-invoice
    # data, CIC report). Empty when the SME has not reached those steps.
    documents: list[AdminApplicationDocument]
    # The most recent run, so the operator sees the engine's answer before
    # giving theirs. None until a score run exists.
    score_run: AdminScoreRunOut | None = None

    model_config = {"from_attributes": True}


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
