"""Who the borrower is, so its limits hold across every project it has (docs/specs/projects/one-borrower.md).

D-016: at most 100M outstanding per borrower on FundLok, and one listing per SME until its loan is repaid in full.
It used to be checked per project, so an SME repaying 100M opened a second 50M listing under another project and
owed 150M (Appendix M 2.6, QA 2026-11-14).

The database holds the two rules a column can (migrations 6664b74ae183 and d5c1e8a7f302):

  * one open project per business: `projects.tax_business` (a branch "0123456789-001" is its company; a 12-digit
    personal number is a household business) and `projects.tax_id` are unique among projects that are not CLOSED or
    ARCHIVED; a clash is DUPLICATE_TAX_CODE. A project is CLOSED when its request ends (close_project_if_finished),
    so a business that has repaid in full, or whose round failed or request was refused, can borrow again (v1.1);
  * one live listing per SME account: `uq_funding_windows_one_live_per_sme`; open_funding_window reports a clash as
    SME_HAS_LIVE_LISTING.

This module holds the rest (Phat, 2026-11-14), which depend on other tables:

  * a project needs a valid tax code, kept in one form so the database can compare it;
  * once the SME's business is verified (KYB), a project must be that business, when it is created and again when
    its listing opens (it may have been created before the verification);
  * an SME cannot start a new project while another of its projects is still in progress: not yet decided,
    approved and not yet ended, listed, or repaying. It can once that one is rejected, closed, its round failed or
    was cancelled, or its loan is repaid in full.
"""
from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.models import WINDOW_LIVE_STATES, FundingWindow
from app.gverify.models import STATUS_APPROVED, GVerifyKybVerification
from app.lending.models import LoanApplication, Project, ProjectOwnership
from app.utils.audit import append_audit

LIVE_WINDOW_STATUSES = WINDOW_LIVE_STATES
ENDED_WINDOW_STATUSES = ("FAILED", "REPAID", "CANCELLED")
ENDED_PROJECT_STATUSES = ("CLOSED", "ARCHIVED")

# Why a request was refused (X-Error-Code).
INVALID_TAX_CODE = "INVALID_TAX_CODE"
TAX_CODE_NOT_VERIFIED_BUSINESS = "TAX_CODE_NOT_VERIFIED_BUSINESS"
DUPLICATE_TAX_CODE = "DUPLICATE_TAX_CODE"  # raised by the database (uq_projects_one_open_per_business / _tax_id)
SME_HAS_OPEN_PROJECT = "SME_HAS_OPEN_PROJECT"
OPEN_PROJECT_DETAIL = (
    "You already have a project in progress. You can start another once it ends or its loan is repaid in full"
)

_COMPANY = re.compile(r"^(\d{10})(?:-?(\d{3}))?$")
_PERSONAL = re.compile(r"^\d{12}$")


class BorrowerRefused(Exception):
    """A borrower rule refused the request: `code` is the X-Error-Code, `status_code` the HTTP status."""

    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def normalize_tax_code(raw: str | None) -> str:
    """A Vietnamese tax code in the one form `projects.tax_business` reads: "0123456789", a branch
    "0123456789-001", or a 12-digit personal number. Spaces and dots as people type them are ignored. ValueError
    otherwise."""
    compact = re.sub(r"[\s.]", "", raw or "")
    if _PERSONAL.match(compact):
        return compact
    match = _COMPANY.match(compact)
    if match is None:
        raise ValueError("not a tax code")
    company, branch = match.groups()
    return f"{company}-{branch}" if branch else company


def tax_base(code: str | None) -> str | None:
    """The business a tax code belongs to, as `projects.tax_business` computes it; None for another shape."""
    try:
        normalized = normalize_tax_code(code)
    except ValueError:
        return None
    return normalized if _PERSONAL.match(normalized) else normalized[:10]


async def verified_tax_base(db: AsyncSession, user_id: UUID) -> str | None:
    """The business the SME verified (its latest approved KYB), as a tax base; None before it is verified."""
    code = (
        await db.execute(
            select(GVerifyKybVerification.tax_code)
            .where(
                GVerifyKybVerification.user_id == user_id,
                GVerifyKybVerification.status == STATUS_APPROVED,
                GVerifyKybVerification.tax_code.is_not(None),
            )
            .order_by(GVerifyKybVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return tax_base(code)


async def _request_ended(db: AsyncSession, project_id: UUID) -> bool:
    """The project's request has ended: its last round failed, was cancelled or was repaid in full (no round live),
    or every application on it was rejected. The one definition of "ended" (spec rule 7)."""
    rounds = (
        await db.execute(select(FundingWindow.status).where(FundingWindow.project_id == project_id))
    ).scalars().all()
    if any(status in LIVE_WINDOW_STATUSES for status in rounds):
        return False
    if rounds and all(status in ENDED_WINDOW_STATUSES for status in rounds):
        return True
    applications = (
        await db.execute(
            select(LoanApplication.status, LoanApplication.admin_approval).where(
                LoanApplication.project_id == project_id
            )
        )
    ).all()
    return bool(applications) and all(status == "REJECTED" or approval == "REJECTED" for status, approval in applications)


async def unfinished_project(db: AsyncSession, user_id: UUID) -> UUID | None:
    """One of the SME's projects still in progress, if any; None when every one has ended: closed or archived, its
    last round failed, was cancelled or was repaid in full, or every application on it was rejected."""
    project_ids = (
        await db.execute(select(ProjectOwnership.project_id).where(ProjectOwnership.user_id == user_id))
    ).scalars().all()
    for project_id in dict.fromkeys(project_ids):
        project = await db.get(Project, project_id)
        if project is None or project.status in ENDED_PROJECT_STATUSES or await _request_ended(db, project_id):
            continue
        return project_id
    return None


async def close_project_if_finished(db: AsyncSession, project_id: UUID, reason: str) -> bool:
    """CLOSED once the project's request has ended (spec rule 3), with one audit row giving the reason: REPAID,
    ROUND_FAILED, ROUND_CANCELLED or REJECTED. The open-project indexes then stop counting it, so the business can
    start a new project under the same tax code. Idempotent; a closed project is never reopened. True if it closed."""
    await db.flush()  # sessions do not autoflush: the caller's new round or application status must be visible below
    project = await db.get(Project, project_id)
    if project is None or project.status in ENDED_PROJECT_STATUSES or not await _request_ended(db, project_id):
        return False
    append_audit(
        db, entity_type="PROJECT", entity_id=project_id, action="PROJECT_CLOSED", actor_id=None,
        before_state={"status": project.status}, after_state={"status": "CLOSED", "reason": reason},
    )
    project.status = "CLOSED"
    await db.flush()
    return True


async def check_new_project(db: AsyncSession, *, user_id: UUID, tax_id: str | None) -> str:
    """Whether this SME may start a project for this business; returns the tax code in its one form. A business
    with a project still open on FundLok is refused by the database when the project is inserted
    (DUPLICATE_TAX_CODE)."""
    try:
        code = normalize_tax_code(tax_id)
    except ValueError:
        raise BorrowerRefused(
            422, INVALID_TAX_CODE,
            "Enter the business's tax code: 10 digits, a branch's 10-3 digits, or a 12-digit personal number",
        ) from None
    verified = await verified_tax_base(db, user_id)
    if verified is not None and verified != tax_base(code):
        raise BorrowerRefused(
            409, TAX_CODE_NOT_VERIFIED_BUSINESS,
            "This tax code is not the business you verified. A project can only be for your verified business",
        )
    if await unfinished_project(db, user_id) is not None:
        raise BorrowerRefused(409, SME_HAS_OPEN_PROJECT, OPEN_PROJECT_DETAIL)
    return code


async def check_can_open(db: AsyncSession, *, project: Project) -> None:
    """Before a project's listing opens: it is the business each owner verified, if they have. A second live
    listing for the same SME is refused by the database (SME_HAS_LIVE_LISTING)."""
    owners = (
        await db.execute(select(ProjectOwnership.user_id).where(ProjectOwnership.project_id == project.id))
    ).scalars().all()
    base = tax_base(project.tax_id)
    for owner in owners:
        verified = await verified_tax_base(db, owner)
        if verified is not None and verified != base:
            raise BorrowerRefused(
                409, TAX_CODE_NOT_VERIFIED_BUSINESS, "The project's tax code is not the business its owner verified"
            )
