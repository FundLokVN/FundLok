from pydantic import BaseModel, field_validator, model_validator
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.projects import engine_constraints
from app.uploads.schemas import LoanApplicationDocumentOut


class LoanApplicationCreateInline(BaseModel):
    # VND has no sub-unit (T1, HANDOFF-03) and `loan_applications.requested_amount`
    # is Numeric(20,0) at the DB layer -- `int` end to end, never Decimal. A
    # Decimal here used to round-trip through Postgres NUMERIC and come back
    # as e.g. Decimal('8E+8') for a round amount like 800,000,000 (Postgres
    # doesn't guarantee a non-exponential internal form, and asyncpg preserves
    # whatever form it sends), so the API could silently serialize a clean
    # amount as scientific notation. `int` sidesteps the whole class of bug.
    requested_amount: int
    # Loan term in months. Optional so an existing client that predates the
    # field keeps working; validated against the engine's
    # allowed_durations_months when present, and stored on
    # `loan_applications.duration_months`.
    duration_months: int | None = None
    purpose: str | None = None
    repayment_preference: str | None = None

    @field_validator("requested_amount")
    @classmethod
    def _amount_within_engine_bounds(cls, value: int) -> int:
        return engine_constraints.validate_loan_size(value)

    @field_validator("duration_months")
    @classmethod
    def _duration_is_allowed(cls, value: int | None) -> int | None:
        return engine_constraints.validate_duration_months(value)


class ProjectCreate(BaseModel):
    legal_name: str
    tax_id: str | None = None
    industry: str | None = None
    # Headcount as entered by the SME, stored on `projects.employee_count`.
    # `company_size` is derived from it below rather than trusted from the
    # client, and is not stored (it is re-derived at grading time).
    employee_count: int | None = None
    company_size: str | None = None
    address: dict[str, Any] | None = None
    incorporation_date: date | None = None
    # Optional: the create-project form also collects loan info, so a DRAFT
    # loan application can be created in the same request.
    loan_application: LoanApplicationCreateInline | None = None

    @field_validator("industry")
    @classmethod
    def _industry_is_known_to_the_engine(cls, value: str | None) -> str | None:
        return engine_constraints.validate_industry(value)

    @field_validator("employee_count")
    @classmethod
    def _headcount_falls_in_a_band(cls, value: int | None) -> int | None:
        return engine_constraints.validate_employee_count(value)

    @model_validator(mode="after")
    def _derive_company_size(self) -> "ProjectCreate":
        """Recompute `company_size` from `employee_count`, overwriting whatever
        the client sent. The bands live in the params file, so the mapping is
        the server's to make (input-sources spec §6.3); a client-supplied band
        that contradicts the headcount would put a mis-scored application in
        the queue."""
        if self.employee_count is not None:
            object.__setattr__(
                self,
                "company_size",
                engine_constraints.company_size_for_headcount(self.employee_count),
            )
        return self


class ProjectOut(BaseModel):
    id: UUID
    legal_name: str
    tax_id: str | None
    industry: str | None
    address: dict[str, Any] | None
    incorporation_date: date | None
    employee_count: int | None = None
    status: str
    created_at: datetime | None
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class LoanApprovalOut(BaseModel):
    """What the applicant is shown once an operator has approved the request.

    The business score and reference rate are the STORED result of the score
    run the operator decided on (INV-5: never recomputed here). A decided
    request cannot be re-scored, so these do not change after approval. The
    repayment figures follow INV-2 (flat interest, charged once) and are the
    repayment schedule's own (21 working days a month, the daily instalment
    floored; agreement/terms.repayment_estimate), the same the Standing Offer
    prints. The documents the SME signs are the binding version.

    Deliberately absent: the bank reference rate, factor scores and every
    other pricing input — internal, never on an applicant surface.
    """

    approved_at: datetime | None
    business_score: float | None
    reference_rate_pct: float | None
    duration_months: int | None
    total_repayment_vnd: int | None
    estimated_daily_repayment_vnd: int | None
    engine_version: str | None
    params_version: str | None


class ProjectLoanApplicationOut(BaseModel):
    id: UUID
    project_id: UUID
    requested_amount: int
    duration_months: int | None = None
    purpose: str | None
    repayment_preference: str | None
    status: str
    submitted_at: datetime | None
    created_at: datetime | None
    # The operator's reason, shown to the applicant on a decided application.
    # It was internal-only until the SME dashboard started rendering it, so
    # the admin panel now says out loud that the applicant reads it -- an
    # operator must never be surprised by where their words end up.
    #
    # `admin_approval` itself is NOT exposed. An approval does not move
    # `status` (it is one half of the two-approval gate), so the applicant
    # learns of it through `approval` below, which carries the score and rate
    # the decision was made on.
    decision_note: str | None = None
    decided_at: datetime | None = None
    documents: list[LoanApplicationDocumentOut] = []
    approval: LoanApprovalOut | None = None

    model_config = {"from_attributes": True}


class ProjectWithApplicationOut(ProjectOut):
    loan_application: ProjectLoanApplicationOut | None = None


class MarketplaceListingOut(BaseModel):
    """The funding round on a marketplace card: the listing an investor joins.

    Real figures only, replacing the samples the card used to derive from a
    hash of the project id. Absent until the request has a contract and a
    listing; a DRAFT listing is not open, so it never appears.
    """

    id: UUID
    status: str  # OPEN | FUNDED | CLOSED
    target_amount: int
    funded_amount: int
    min_ticket: int | None
    investor_count: int
    open_at: datetime | None
    close_at: datetime | None


class MarketplaceLoanApplicationOut(BaseModel):
    """The deal terms an investor sees on a marketplace card.

    Deliberately not ProjectLoanApplicationOut, which is the applicant's own
    view: it carries the operator's decision_note (written for the SME) and
    the document list with storage keys and original filenames. A field
    added there must not reach every investor by default, so this one lists
    what the card shows and nothing else.
    """

    id: UUID
    project_id: UUID
    requested_amount: int
    duration_months: int | None = None
    purpose: str | None
    repayment_preference: str | None
    status: str
    submitted_at: datetime | None
    created_at: datetime | None
    # The document types the FundLok team reviewed before approving this
    # request (service.verified_document_types). Types only: the files, their
    # names and storage keys never reach an investor.
    verified_documents: list[str] = []
    # The 0-100 business score of the LOCKED score run this request was
    # approved on (service.locked_business_scores): the same figure the SME
    # sees in its approval summary. Never a credit rating. Null until a run is
    # locked.
    business_score: float | None = None
    listing: MarketplaceListingOut | None = None
    # The rate the marketplace shows as the investor's target return: FundLok's
    # bank rate in force (bank_rate_config.rate_pct), the same on every card.
    # A target, never a promise; the card says so beside it.
    bank_rate_pct: float | None = None
    # DRR, the LOCKED run's daily repayment rate: the daily instalment over the
    # average daily revenue the assessment used, as a fraction (0.085 = 8.5%).
    # Null until a run is locked, or on a run scored before it was stored.
    daily_repayment_rate: float | None = None
    # s, the revenue share for extensions that the Standing Offer prints: the
    # share of a month's revenue the automatic extension tests the payments
    # against. Today the same figure as DRR (D-044; Loc, 16 Nov, D-060), kept
    # as its own field so the screen never has to know that.
    revenue_share: float | None = None
    # The Personal Guarantor the signed Standing Offer names: their name only,
    # never the ID number or address it also prints. Null until the Offer is on
    # file.
    guarantor_name: str | None = None

    model_config = {"from_attributes": True}


class MarketplaceProjectOut(ProjectOut):
    loan_application: MarketplaceLoanApplicationOut | None = None
