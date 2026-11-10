"""Investing in a project through its funding window (ADR-009, HANDOFF-04 Epic 1).

The rules are Edward's (app/direct_transfer: windows, reservation, tranches). This module decides when they run
and with what, so a project goes from approved to taking investment with nobody filing a contract or a listing by
hand:

- `publish_project_for_funding` opens the project's window once it is live (ACTIVE: the admin's approval and a
  LOCKED score run) and its SME has an approved bank account, the account every investor then pays into. It runs
  when an admin approves the SME's account and when a project goes live, whichever comes second (`publish_if_ready`),
  and an admin can run it again from the project panel.
- `reserve_and_accept` is the investor's one "I agree" step: hold the amount and sign the acceptance of the
  investing terms, with the commitment's Investment Confirmation (Annex of Contract 4). At their first commitment
  they then sign the Investor Service Agreement, filled in with this commitment's Annex, and upload it
  (app/market/investor_agreement); the payment details wait for it. They then pay the SME directly
  (`payment_instructions`); FundLok never holds the money.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.schemas import ContractCreate
from app.contracts.service import create_contract
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, conflict, not_found
from app.direct_transfer.models import FundingWindow, PaymentAccount, Tranche
from app.direct_transfer.reservation import reserve_ticket
from app.direct_transfer.signing import MockSigner, Signer
from app.direct_transfer.storage import EvidenceStorage, R2Storage
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.tranches import accept_tranche
from app.direct_transfer.windows import lock_window, open_funding_window, refresh_window_fullness
from app.lending.models import Contract, Listing, LoanApplication, Project, ProjectOwnership, ScoreRun
from app.market.investor_agreement import service as investor_agreement
from app.utils.audit import append_audit

logger = logging.getLogger(__name__)

# The window holds capacity and takes reservations in these states; at most one per project (Edward's partial
# unique index uq_funding_windows_one_live_per_project).
LIVE_WINDOW_STATUSES = ("OPEN", "FULL", "CLOSING", "REPAYING")

# The investing terms the investor agrees to before reserving. The text is the front end's
# (investment.terms in lib/i18n); bump this with any change to it, so an investor holding the old text is asked to
# read the new one (409 TERMS_OUTDATED) and every signed acceptance names the text it accepted.
INVESTOR_TERMS_VERSION = "2026-10-30"

# Where signed documents go. Tests swap in InMemoryStorage (root conftest).
evidence_storage: type[EvidenceStorage] = R2Storage

# The leading bytes of each type a receipt may be. The browser's Content-Type is the uploader's claim; what is
# stored, and later opened by staff, is typed by its content.
_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def sniff_receipt_type(content: bytes) -> str | None:
    """A receipt's type by its content (PDF, PNG, JPEG or WEBP), or None for anything else."""
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return next((kind for magic, kind in _SIGNATURES if content.startswith(magic)), None)


def _signer() -> Signer:
    # The remote-signing vendor is not chosen yet (funding-window.md rule 1); MockSigner is Edward's stand-in.
    return MockSigner()


def _from_http(exc: HTTPException) -> DirectTransferError:
    return DirectTransferError(exc.status_code, str(exc.detail), "CONTRACT_NOT_CREATED")


async def _sme_payout_account(db: AsyncSession, project_id: UUID) -> PaymentAccount | None:
    """The account investors pay into: the owner's most recently approved SME account at a listed bank. One saved
    by name only (no BIN) is approved for the record but cannot carry a VietQR, so it cannot take a window."""
    return (
        await db.execute(
            select(PaymentAccount)
            .join(ProjectOwnership, ProjectOwnership.user_id == PaymentAccount.user_id)
            .where(
                ProjectOwnership.project_id == project_id,
                PaymentAccount.holder_role == "SME",
                PaymentAccount.status == "APPROVED",
                PaymentAccount.bank_bin.is_not(None),
            )
            .order_by(PaymentAccount.approved_at.desc().nulls_last(), PaymentAccount.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _approved_request(db: AsyncSession, project_id: UUID) -> tuple[LoanApplication, ScoreRun] | None:
    """The newest request with both approvals: the admin's, and the engine's LOCKED score run (its terms)."""
    row = (
        await db.execute(
            select(LoanApplication, ScoreRun)
            .join(ScoreRun, ScoreRun.application_id == LoanApplication.id)
            .where(
                LoanApplication.project_id == project_id,
                LoanApplication.admin_approval == "APPROVED",
                ScoreRun.status == "LOCKED",
            )
            .order_by(LoanApplication.created_at.desc(), ScoreRun.locked_at.desc().nulls_last())
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def live_window_for_project(db: AsyncSession, project_id: UUID) -> FundingWindow | None:
    return (
        await db.execute(
            select(FundingWindow).where(
                FundingWindow.project_id == project_id, FundingWindow.status.in_(LIVE_WINDOW_STATUSES)
            )
        )
    ).scalar_one_or_none()


async def publish_project_for_funding(
    db: AsyncSession,
    *,
    project_id: UUID,
    now: dt.datetime,
    actor_id: UUID | None,
    trigger: str,
    ip_address: str | None = None,
    params: DirectTransferParams | None = None,
) -> FundingWindow:
    """Open the project's funding window, creating its contract and listing first when it has none.

    Returns the live window when there already is one. Raises DirectTransferError with a code saying why the
    project cannot open (PROJECT_NOT_LIVE, ACCOUNT_NOT_APPROVED, TERMS_NOT_LOCKED, or the window's own:
    TARGET_ABOVE_CAP, RATE_ABOVE_CAP, LISTING_NOT_OPENABLE, ...).

    The window is opened on the SME's behalf. The SME and its Personal Guarantor have already signed the Standing
    Offer itself (a project is only ACTIVE once they have: app/loans/signing.py); the window's StandingOffer row is
    still the machine-readable terms snapshot signed with the mock signer, and the SME's outside-debt declaration
    is taken as zero. Linking the window to the SME's signed PDF and collecting the declaration are later steps
    (funding-window.md rule 1, I-25).
    """
    p = resolve(params)
    project = await db.get(Project, project_id)
    if project is None:
        raise not_found("Project not found")
    live = await live_window_for_project(db, project_id)
    if live is not None:
        return live
    if project.status != "ACTIVE":
        raise conflict("PROJECT_NOT_LIVE", "The project is not live on the marketplace")
    account = await _sme_payout_account(db, project_id)
    if account is None:
        raise conflict("ACCOUNT_NOT_APPROVED", "The business has no approved bank account at a listed bank")
    request = await _approved_request(db, project_id)
    if request is None:
        raise conflict("TERMS_NOT_LOCKED", "The project has no approved request with locked terms")
    application, score_run = request

    contract = (
        await db.execute(select(Contract).where(Contract.application_id == application.id))
    ).scalar_one_or_none()
    if contract is None:
        try:
            contract = await create_contract(
                db, ContractCreate(application_id=application.id, score_run_id=score_run.id)
            )
        except HTTPException as exc:
            raise _from_http(exc) from exc
    listing = (await db.execute(select(Listing).where(Listing.contract_id == contract.id))).scalar_one_or_none()
    if listing is None:
        # Not market.create_listing: it checks the legacy KYC document rows, while a live project has already
        # passed the admin's document review and GVerify. open_funding_window takes a DRAFT listing and opens it.
        listing = Listing(
            contract_id=contract.id,
            target_amount=contract.target_amount,
            min_ticket=p.min_ticket_vnd,
            status="DRAFT",
            funded_amount=0,
        )
        db.add(listing)
        await db.flush()

    window = await open_funding_window(
        db,
        listing_id=listing.id,
        sme_user_id=account.user_id,
        payment_account_id=account.id,
        declared_outside_debt_vnd=0,
        declaration_accepted=True,
        idempotency_key=f"publish:{listing.id}",
        now=now,
        signer=_signer(),
        storage=evidence_storage(),
        params=p,
    )
    if window.status not in LIVE_WINDOW_STATUSES:
        # The key replayed an earlier window on this listing that has since ended.
        raise conflict("LISTING_NOT_OPENABLE", "The project's last funding window has ended")
    append_audit(
        db,
        entity_type="PROJECT",
        entity_id=project.id,
        action="FUNDING_WINDOW_OPENED",
        actor_id=actor_id,
        after_state={
            "trigger": trigger,
            "window_id": str(window.id),
            "listing_id": str(listing.id),
            "contract_id": str(contract.id),
            "target_vnd": window.target_vnd,
            "min_ticket_vnd": window.min_ticket_vnd,
            "annual_rate_pct": str(window.annual_rate_pct),
            "term_months": window.term_months,
            "window_end": window.window_end.isoformat(),
            "payment_account_id": str(account.id),
            "account_last4": account.account_number[-4:],
        },
        ip_address=ip_address,
    )
    await db.flush()
    return window


async def publish_if_ready(
    db: AsyncSession, *, project_id: UUID, now: dt.datetime, actor_id: UUID | None, trigger: str
) -> FundingWindow | None:
    """`publish_project_for_funding`, best effort: the approval that triggered it stands either way.

    Runs in a savepoint, so a project that cannot open yet (or a storage failure while signing) leaves nothing
    half-made behind; an admin can open it later from the project panel, which shows the reason.
    """
    try:
        async with db.begin_nested():
            return await publish_project_for_funding(
                db, project_id=project_id, now=now, actor_id=actor_id, trigger=trigger
            )
    except DirectTransferError as exc:
        logger.info("project %s not opened for funding (%s): %s", project_id, exc.code, exc.detail)
    except Exception:
        logger.exception("opening project %s for funding failed", project_id)
    return None


async def publish_owner_projects(
    db: AsyncSession, *, user_id: UUID, now: dt.datetime, actor_id: UUID | None, trigger: str
) -> list[FundingWindow]:
    """Every live project the user owns, opened for funding (best effort); the windows that are now live."""
    project_ids = (
        await db.execute(
            select(Project.id)
            .join(ProjectOwnership, ProjectOwnership.project_id == Project.id)
            .where(ProjectOwnership.user_id == user_id, Project.status == "ACTIVE")
        )
    ).scalars().all()
    opened = []
    for project_id in project_ids:
        window = await publish_if_ready(db, project_id=project_id, now=now, actor_id=actor_id, trigger=trigger)
        if window is not None:
            opened.append(window)
    return opened


async def _investor_payout_account(db: AsyncSession, investor_id: UUID) -> UUID | None:
    """Where the SME repays this investor: their most recently approved account at a listed bank."""
    account = await investor_agreement.payout_account(db, investor_id)
    return account.id if account is not None else None


async def expire_lapsed_holds(db: AsyncSession, *, window_id: UUID, now: dt.datetime) -> int:
    """The window's RESERVED and SIGNED tickets whose hold has lapsed with no receipt -> EXPIRED, freeing their
    capacity so their investors (and others) can reserve again (funding-window.md rule 9). How many expired.

    Edward's `expire_holds_job` does this for every window on a timer, but only `app/direct_transfer/worker.py`
    schedules it and nothing runs that worker yet, so a lapsed hold would otherwise block its investor
    (ALREADY_HAS_LIVE_TRANCHE) and the window's capacity for good. Same rule as the job, so both can run.
    """
    lapsed = (
        Tranche.window_id == window_id,
        Tranche.status.in_(("RESERVED", "SIGNED")),
        Tranche.receipt_submitted_at.is_(None),
        Tranche.hold_expires_at <= now,
    )
    if (await db.execute(select(Tranche.id).where(*lapsed).limit(1))).first() is None:
        return 0
    window = await lock_window(db, window_id)
    rows = (await db.execute(select(Tranche).where(*lapsed))).scalars().all()
    for tranche in rows:
        await transition_tranche(db, tranche, "EXPIRED", now=now, reason="HOLD_LAPSED")
    await refresh_window_fullness(db, window, now)
    return len(rows)


async def reserve_and_accept(
    db: AsyncSession,
    *,
    window_id: UUID,
    investor_id: UUID,
    amount_vnd: int,
    ack_risk_disclosure: bool,
    terms_version: str,
    idempotency_key: str,
    now: dt.datetime,
    payment_account_id: UUID | None = None,
    params: DirectTransferParams | None = None,
) -> Tranche:
    """Reserve `amount_vnd` on the window and sign the acceptance, in one step: the investor agrees to the
    investing terms once, and the tranche comes back SIGNED, ready for payment instructions.

    The acceptance carries the commitment's Investment Confirmation (Contract 4's Annex). With a signed Investor
    Service Agreement of the current version on file, the Annex is kept now (PDF and record) and the acceptance
    names it and the agreement. Without one (the first commitment), the commitment is still made, code and all,
    but no money moves: the investor next signs the agreement filled in with this commitment's Annex, and the
    payment details are refused until they upload it (funding_router.read_payment).

    One transaction: if signing fails the reservation is not kept either. Replaying the Idempotency-Key returns
    the same tranche (reserve_ticket and accept_tranche are both idempotent), unless its hold has since lapsed:
    then 409 HOLD_EXPIRED, and the investor reserves again with a new key.
    """
    if terms_version != INVESTOR_TERMS_VERSION:
        raise conflict("TERMS_OUTDATED", "The investing terms have changed; read them again before investing")
    agreement = await investor_agreement.current_signature(db, investor_id)
    account_id = payment_account_id or await _investor_payout_account(db, investor_id)
    if account_id is None:
        raise conflict("ACCOUNT_NOT_APPROVED", "Add a bank account at a listed bank before investing")
    # A lapsed hold no longer counts as the investor's live ticket, nor against the window's capacity.
    await expire_lapsed_holds(db, window_id=window_id, now=now)
    tranche = await reserve_ticket(
        db,
        window_id=window_id,
        investor_id=investor_id,
        amount_vnd=amount_vnd,
        ack_risk_disclosure=ack_risk_disclosure,
        payment_account_id=account_id,
        idempotency_key=idempotency_key,
        now=now,
        params=params,
    )
    if tranche.status == "EXPIRED":
        raise conflict("HOLD_EXPIRED", "The reservation hold has expired; reserve again")
    window = await db.get(FundingWindow, tranche.window_id)
    if agreement is not None:
        # The Annex, filled in and kept (PDF and record) before the payment details exist.
        confirmation = await investor_agreement.record_confirmation(
            db, tranche=tranche, window=window, agreement=agreement, storage=evidence_storage(), now=now,
            params=params,
        )
        agreement_doc = {
            "id": str(agreement.id),
            "version": agreement.version,
            "signed_at": agreement.signed_at.isoformat(),
            "document_sha256": agreement.document_sha256,
        }
        annex_doc = investor_agreement.confirmation_document(confirmation)
    else:
        # First commitment: the agreement, with this Annex filled in, is signed next, before any payment.
        terms = await investor_agreement.investment_confirmation(
            db, window=window, amount_vnd=tranche.amount_vnd, pay_by=tranche.hold_expires_at, params=params
        )
        agreement_doc = {"version": investor_agreement.VERSION, "signed_with_this_commitment_next": True}
        annex_doc = terms.as_document()
    # What the investor signs: the terms they read, by version, the ticket they are taking, its Investment
    # Confirmation, and the master agreement it falls under.
    document = json.dumps(
        {
            "document": "FUNDLOK_INVESTOR_ACCEPTANCE",
            "terms_version": terms_version,
            "investor_service_agreement": agreement_doc,
            "investment_confirmation": annex_doc,
            "window_id": str(window.id),
            "listing_id": str(window.listing_id),
            "tranche_id": str(tranche.id),
            "investor_id": str(investor_id),
            "amount_vnd": tranche.amount_vnd,
            "reference_code": tranche.reference_code,
            "annual_rate_pct": str(window.annual_rate_pct),
            "term_months": window.term_months,
            "accepted_at": now.isoformat(),
        },
        sort_keys=True,
    ).encode("utf-8")
    return await accept_tranche(
        db,
        tranche_id=tranche.id,
        investor_id=investor_id,
        document=document,
        signer=_signer(),
        storage=evidence_storage(),
        now=now,
        params=params,
    )
