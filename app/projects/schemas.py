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
    # `admin_approval` is NOT exposed: it is the internal half of the
    # two-approval gate, and `status` already carries what the applicant is
    # entitled to know about the outcome.
    decision_note: str | None = None
    decided_at: datetime | None = None
    documents: list[LoanApplicationDocumentOut] = []

    model_config = {"from_attributes": True}


class ProjectWithApplicationOut(ProjectOut):
    loan_application: ProjectLoanApplicationOut | None = None
