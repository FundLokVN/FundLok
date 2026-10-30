"""Funding window: open (standing offer), capacity, progress (HANDOFF-04 B5/B6).

Spec: docs/specs/direct_transfer/funding-window.md. Window state lives in `funding_windows` (1:1 with a
listing) so the shared `app/lending/models.py` is not touched; the listing's own status/open_at/close_at are
still driven here at open and at close.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer import fees
from app.direct_transfer.accounts import get_usable_account
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, bad_request, conflict, not_found
from app.direct_transfer.evidence import log_event, store_evidence_file
from app.direct_transfer.models import FundingWindow, StandingOffer, Tranche
from app.direct_transfer.signing import Signer, SignerIdentity, render_offer_document
from app.direct_transfer.storage import EvidenceStorage
from app.lending.models import Contract, Listing, LoanApplication, Order, ProjectOwnership, ScoreRun
from app.users.models import User

CAPACITY_STATES = ("RESERVED", "SIGNED", "ATTESTED", "CONFIRMED")


def compute_min_raise(target_vnd: int, pct: int) -> int:
    return -(-target_vnd * pct // 100)  # ceil


async def capacity_used(db: AsyncSession, window_id: UUID) -> int:
    """Amount held by live tranches. UNDER_REVIEW holds capacity except a LATE_RECEIPT review, which does not
    (otherwise re-entering review could overbook the window; reviving re-checks capacity). PROVISIONAL(Q-D3)."""
    base = (
        await db.execute(
            select(func.coalesce(func.sum(Tranche.amount_vnd), 0)).where(
                Tranche.window_id == window_id, Tranche.status.in_(CAPACITY_STATES)
            )
        )
    ).scalar_one()
    under_review = (
        await db.execute(
            select(func.coalesce(func.sum(Tranche.amount_vnd), 0)).where(
                Tranche.window_id == window_id,
                Tranche.status == "UNDER_REVIEW",
                (Tranche.review_cause.is_(None)) | (Tranche.review_cause != "LATE_RECEIPT"),
            )
        )
    ).scalar_one()
    return int(base) + int(under_review)


async def capacity_left(db: AsyncSession, window: FundingWindow) -> int:
    return max(0, window.target_vnd - await capacity_used(db, window.id))


async def confirmed_total(db: AsyncSession, window_id: UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Tranche.amount_vnd), 0)).where(
                    Tranche.window_id == window_id, Tranche.status.in_(("CONFIRMED", "ACTIVE"))
                )
            )
        ).scalar_one()
    )


async def reserved_total(db: AsyncSession, window_id: UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(Tranche.amount_vnd), 0)).where(
                    Tranche.window_id == window_id, Tranche.status.in_(("RESERVED", "SIGNED", "ATTESTED"))
                )
            )
        ).scalar_one()
    )


async def check_borrower_cap(
    db: AsyncSession, *, window_id: UUID | None, project_id: UUID, target_vnd: int, declared_outside_debt_vnd: int,
    now: dt.datetime, params: DirectTransferParams, actor_id: UUID | None = None,
) -> dict:
    """Own records plus the SME's signed declaration (I-25, D-022); CIC replaces the source once connected.

    Raises BORROWER_CAP_EXCEEDED when target + declared outside debt would exceed the cross-platform cap
    (PROVISIONAL(Q-D12)) or the per-platform cap. Returns the evidence payload."""
    total = target_vnd + declared_outside_debt_vnd
    basis = {
        "basis": "OWN_RECORDS_PLUS_DECLARATION",
        "target_vnd": target_vnd,
        "declared_outside_debt_vnd": declared_outside_debt_vnd,
        "total_vnd": total,
        "per_platform_cap_vnd": params.max_target_vnd,
        "cross_platform_cap_vnd": params.cross_platform_cap_vnd,
    }
    if target_vnd > params.max_target_vnd or total > params.cross_platform_cap_vnd:
        raise conflict("BORROWER_CAP_EXCEEDED", "The borrower's total exposure would exceed the cap")
    return basis


async def open_funding_window(
    db: AsyncSession,
    *,
    listing_id: UUID,
    sme_user_id: UUID,
    payment_account_id: UUID,
    declared_outside_debt_vnd: int,
    declaration_accepted: bool,
    idempotency_key: str,
    now: dt.datetime,
    signer: Signer,
    storage: EvidenceStorage,
    window_days: int | None = None,
    params: DirectTransferParams | None = None,
) -> FundingWindow:
    p = resolve(params)
    if not idempotency_key:
        raise bad_request("Idempotency-Key is required", "IDEMPOTENCY_KEY_REQUIRED")
    existing = (await db.execute(select(FundingWindow).where(FundingWindow.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing is not None:
        if existing.listing_id != listing_id or existing.sme_user_id != sme_user_id:
            raise conflict("IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for a different request")
        return existing
    if not declaration_accepted:
        raise bad_request("The borrower declaration must be accepted", "DECLARATION_REQUIRED")
    if declared_outside_debt_vnd < 0:
        raise bad_request("Invalid declared debt")
    days = window_days if window_days is not None else p.window_days
    if not (1 <= days <= p.window_days):
        raise bad_request(f"window_days must be between 1 and {p.window_days}")

    listing = (await db.execute(select(Listing).where(Listing.id == listing_id).with_for_update())).scalar_one_or_none()
    if listing is None:
        raise not_found("Listing not found")
    contract = await db.get(Contract, listing.contract_id)
    application = await db.get(LoanApplication, contract.application_id) if contract and contract.application_id else None
    if contract is None or application is None:
        raise not_found("Listing not found")
    owner = (
        await db.execute(
            select(ProjectOwnership.id).where(
                ProjectOwnership.project_id == application.project_id, ProjectOwnership.user_id == sme_user_id
            )
        )
    ).first()
    if owner is None:
        raise not_found("Listing not found")  # 404, not 403: do not confirm the listing exists

    if listing.status not in ("DRAFT", "OPEN") or contract.status != "ACTIVE_PENDING_FUNDING":
        raise conflict("LISTING_NOT_OPENABLE", "The listing cannot be opened for funding")
    filled = (await db.execute(select(func.count()).select_from(Order).where(Order.listing_id == listing.id, Order.status == "FILLED"))).scalar_one()
    if filled:
        raise conflict("LISTING_NOT_OPENABLE", "The listing already has legacy orders")

    target = int(listing.target_amount)
    if target <= 0 or target > p.max_target_vnd:
        raise bad_request("The target must be positive and at most 100,000,000 VND", "TARGET_ABOVE_CAP")

    score_run = await db.get(ScoreRun, contract.score_run_id) if contract.score_run_id else None
    if score_run is None or score_run.status != "LOCKED" or score_run.interest_rate_pct is None:
        raise conflict("TERMS_NOT_LOCKED", "The loan terms are not locked")
    term = application.duration_months
    if not term or term <= 0:
        raise conflict("TERMS_NOT_LOCKED", "The loan term is missing")
    rate = Decimal(str(score_run.interest_rate_pct)).quantize(Decimal("0.001"))
    if rate > Decimal(p.rate_cap_pct):
        raise conflict("RATE_ABOVE_CAP", "The rate exceeds the statutory cap")

    if await fees.sme_has_unpaid_invoice(db, sme_user_id, p):
        raise conflict("SME_INVOICE_UNPAID", "A FundLok invoice is unpaid")

    account = await get_usable_account(db, account_id=payment_account_id, user_id=sme_user_id, holder_role="SME")

    live = (
        await db.execute(
            select(FundingWindow.id).where(
                FundingWindow.project_id == application.project_id,
                FundingWindow.status.in_(("OPEN", "FULL", "CLOSING", "REPAYING")),
            )
        )
    ).first()
    if live is not None:
        raise conflict("SME_HAS_LIVE_LISTING", "The SME already has a live listing")

    cap = await check_borrower_cap(
        db, window_id=None, project_id=application.project_id, target_vnd=target,
        declared_outside_debt_vnd=declared_outside_debt_vnd, now=now, params=p,
    )
    min_ticket = max(p.min_ticket_vnd, int(listing.min_ticket or 0))
    window_end = now + dt.timedelta(days=days)
    offer_terms = {
        "listing_id": str(listing.id),
        "contract_id": str(contract.id),
        "target_vnd": target,
        "min_raise_vnd": compute_min_raise(target, p.min_raise_pct),
        "min_ticket_vnd": min_ticket,
        "annual_rate_pct": str(rate),
        "term_months": term,
        "window_start": now.isoformat(),
        "window_end": window_end.isoformat(),
        "engine_version": score_run.engine_version,
        "params_version": score_run.params_version,
        "score_run_id": str(score_run.id),
        "sme_payment_account": {"bank_bin": account.bank_bin, "account_number": account.account_number},
        "separate_contract_per_accepting_investor": True,
    }
    window = FundingWindow(
        listing_id=listing.id,
        contract_id=contract.id,
        project_id=application.project_id,
        sme_user_id=sme_user_id,
        sme_payment_account_id=account.id,
        target_vnd=target,
        min_raise_vnd=offer_terms["min_raise_vnd"],
        min_ticket_vnd=min_ticket,
        annual_rate_pct=rate,
        term_months=term,
        offer_terms=json.loads(json.dumps(offer_terms)),
        declared_outside_debt_vnd=declared_outside_debt_vnd,
        window_start=now,
        window_end=window_end,
        status="OPEN",
        idempotency_key=idempotency_key,
    )
    db.add(window)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise conflict("SME_HAS_LIVE_LISTING", "The SME already has a live listing") from exc

    sme_user = await db.get(User, sme_user_id)
    signature = await signer.sign(
        document=render_offer_document(offer_terms),
        signer=SignerIdentity(user_id=sme_user_id, display_name=(sme_user.full_name or sme_user.email), role="SME"),
        now=now,
    )
    stored = await store_evidence_file(
        db, storage, owner_user_id=sme_user_id, purpose="SIGNED_DOCUMENT", content=signature.signed_content,
        content_type="application/pdf", filename=f"standing-offer-{window.id}.pdf", params=p,
    )
    db.add(
        StandingOffer(
            window_id=window.id,
            signer_user_id=sme_user_id,
            provider=signature.provider,
            file_key=stored.storage_key,
            file_sha256=signature.sha256,
            certificate=signature.certificate,
            signed_at=signature.signed_at,
            valid_until=window_end,
        )
    )
    listing.status = "OPEN"
    listing.open_at = now
    listing.close_at = window_end
    await db.flush()
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="CAP_CHECK", now=now,
        actor_id=sme_user_id, actor_role="SME", payload=cap,
    )
    await log_event(
        db, entity_type="WINDOW", entity_id=window.id, event_type="WINDOW_OPENED", now=now,
        actor_id=sme_user_id, actor_role="SME",
        payload={"offer_sha256": signature.sha256, "window_end": window_end.isoformat()},
    )
    return window


@dataclasses.dataclass(frozen=True)
class WindowProgress:
    id: UUID
    listing_id: UUID
    status: str
    target_vnd: int
    min_raise_vnd: int
    min_ticket_vnd: int
    confirmed_vnd: int
    reserved_vnd: int
    capacity_vnd: int
    annual_rate_pct: str
    term_months: int
    window_start: dt.datetime
    window_end: dt.datetime
    days_left: int
    tranche_count: int
    closing_started_at: dt.datetime | None
    close_wait_until: dt.datetime | None
    repayment_start_date: dt.date | None


async def get_window_progress(db: AsyncSession, *, listing_id: UUID, now: dt.datetime) -> WindowProgress:
    window = (await db.execute(select(FundingWindow).where(FundingWindow.listing_id == listing_id))).scalar_one_or_none()
    if window is None:
        raise not_found("Window not found")
    count = (
        await db.execute(
            select(func.count()).select_from(Tranche).where(
                Tranche.window_id == window.id, Tranche.status.in_(("RESERVED", "SIGNED", "ATTESTED", "CONFIRMED", "UNDER_REVIEW", "ACTIVE"))
            )
        )
    ).scalar_one()
    remaining = window.window_end - now
    days_left = max(0, -(-int(remaining.total_seconds()) // 86400)) if window.status in ("OPEN", "FULL") else 0
    return WindowProgress(
        id=window.id,
        listing_id=window.listing_id,
        status=window.status,
        target_vnd=window.target_vnd,
        min_raise_vnd=window.min_raise_vnd,
        min_ticket_vnd=window.min_ticket_vnd,
        confirmed_vnd=await confirmed_total(db, window.id),
        reserved_vnd=await reserved_total(db, window.id),
        capacity_vnd=await capacity_left(db, window),
        annual_rate_pct=str(window.annual_rate_pct),
        term_months=window.term_months,
        window_start=window.window_start,
        window_end=window.window_end,
        days_left=days_left,
        tranche_count=int(count),
        closing_started_at=window.closing_started_at,
        close_wait_until=window.close_wait_until,
        repayment_start_date=window.repayment_start_date,
    )


async def lock_window(db: AsyncSession, window_id: UUID) -> FundingWindow:
    window = (await db.execute(select(FundingWindow).where(FundingWindow.id == window_id).with_for_update())).scalar_one_or_none()
    if window is None:
        raise not_found("Window not found")
    return window


async def refresh_window_fullness(db: AsyncSession, window: FundingWindow, now: dt.datetime) -> None:
    """FULL <-> OPEN as capacity changes (a lapse while FULL reopens the window until day 14, PROVISIONAL(Q-D3))."""
    if window.status == "FULL" and now < window.window_end and await capacity_left(db, window) > 0:
        window.status = "OPEN"
    elif window.status == "OPEN" and await capacity_left(db, window) == 0:
        window.status = "FULL"
    await db.flush()
