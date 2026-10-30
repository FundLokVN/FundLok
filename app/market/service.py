from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.models import FundingWindow, PaymentAccount
from app.gverify.models import STATUS_APPROVED as KYC_APPROVED
from app.gverify.service import latest_for_user
from app.lending.kyc import project_has_verified_kyc
from app.lending.models import Contract, Holding, Listing, LoanApplication, Order
from app.projects.service import PAYMENT_ACCOUNT_LISTABLE_STATUSES
from app.users.models import User


def _dec(value: object) -> Decimal:
    """Coerce ORM / JSON numerics to Decimal (avoids Decimal ± float TypeError)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return Decimal(str(value))


async def _recompute_holding_shares(db: AsyncSession, contract_id: UUID) -> None:
    result = await db.execute(select(Holding).where(Holding.contract_id == contract_id))
    hs = result.scalars().all()
    total = sum((_dec(h.principal) for h in hs), Decimal("0"))
    if total <= 0:
        return
    for h in hs:
        p = _dec(h.principal)
        h.share_ratio = (p / total).quantize(Decimal("0.00000001"))
        db.add(h)


async def create_listing(db: AsyncSession, contract_id: UUID, target_amount: Decimal, min_ticket: Decimal) -> Listing:
    result = await db.execute(select(Contract).where(Contract.id == contract_id))
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Contract not found")
    if c.status != "ACTIVE_PENDING_FUNDING":
        raise HTTPException(
            status_code=400,
            detail="Contract must be ACTIVE_PENDING_FUNDING to list",
        )
    result = await db.execute(select(LoanApplication).where(LoanApplication.id == c.application_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=400, detail="Contract has no application")
    if not await project_has_verified_kyc(db, app.project_id):
        raise HTTPException(
            status_code=400,
            detail="Project must have all KYC documents APPROVED before listing",
        )
    result = await db.execute(select(Listing).where(Listing.contract_id == contract_id))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Listing already exists for this contract")
    if min_ticket <= 0:
        raise HTTPException(status_code=400, detail="min_ticket must be positive")
    now = datetime.now(timezone.utc)
    lst = Listing(
        contract_id=contract_id,
        target_amount=target_amount,
        min_ticket=min_ticket,
        status="OPEN",
        funded_amount=Decimal("0"),
        open_at=now,
    )
    db.add(lst)
    await db.flush()
    await db.refresh(lst)
    return lst


async def _require_eligible_investor(db: AsyncSession, investor_id: UUID) -> None:
    """Only a verified, identified investor may commit money to a listing.

    The INVESTOR role is self-selected right after sign-up, before the email is
    proven or any identity check has run, and an order fills on placement (see
    GAP-2 in docs/backlog/known-gaps.md: there is no payment step yet). Without
    this, an anonymous account could take a whole listing, lock real investors
    out and be credited a share of every repayment. KYC is the same signal the
    frontend gates on: the investor's latest GVerify KYC attempt is APPROVED.
    """
    user = (await db.execute(select(User).where(User.id == investor_id))).scalar_one_or_none()
    if user is None or not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verify your email address before investing",
        )
    attempt = await latest_for_user(db, investor_id, "KYC")
    if attempt is None or attempt.status != KYC_APPROVED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Complete identity verification before investing",
        )
    # Money moves between the investor's and the SME's own bank accounts (ADR-009), and repayments come back
    # to the investor's. With no account saved there is nowhere for them to come back to, so no order.
    has_account = (
        await db.execute(
            select(
                exists().where(
                    PaymentAccount.user_id == investor_id,
                    PaymentAccount.status.in_(PAYMENT_ACCOUNT_LISTABLE_STATUSES),
                )
            )
        )
    ).scalar()
    if not has_account:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Add a bank account before investing",
            headers={"X-Error-Code": "BANK_ACCOUNT_REQUIRED"},
        )


async def place_order(
    db: AsyncSession,
    listing_id: UUID,
    investor_id: UUID,
    amount: Decimal,
    ack_risk_disclosure: bool,
    idempotency_key: str | None,
) -> tuple[Order, bool]:
    if not ack_risk_disclosure:
        raise HTTPException(status_code=400, detail="Risk disclosure must be acknowledged")
    await _require_eligible_investor(db, investor_id)
    if idempotency_key:
        result = await db.execute(select(Order).where(Order.idempotency_key == idempotency_key))
        existing = result.scalar_one_or_none()
        if existing:
            # Keys are unique across all orders, so a replay is only a replay
            # when it comes from the investor who placed the order. Anyone else
            # gets a plain conflict, never that investor's order back.
            if existing.investor_id != investor_id:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key already used",
                )
            if existing.listing_id != listing_id:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key already used for a different listing",
                )
            return existing, False

    result = await db.execute(select(Listing).where(Listing.id == listing_id).with_for_update())
    lst = result.scalar_one_or_none()
    if not lst:
        raise HTTPException(status_code=404, detail="Listing not found")
    if lst.status != "OPEN":
        raise HTTPException(status_code=400, detail="Listing is not open for funding")
    # A listing with a funding window takes money only through it (ADR-009): an order here would fill with no
    # payment and the window's close would then overwrite funded_amount.
    has_window = (
        await db.execute(select(FundingWindow.id).where(FundingWindow.listing_id == lst.id))
    ).first()
    if has_window is not None:
        raise HTTPException(
            status_code=409,
            detail="Invest in this listing through its funding window",
            headers={"X-Error-Code": "USE_FUNDING_WINDOW"},
        )

    amount = _dec(amount)
    target_amt = _dec(lst.target_amount)
    funded_amt = _dec(lst.funded_amount)
    min_ticket = _dec(lst.min_ticket) if lst.min_ticket is not None else None

    # Unconditional floor, independent of min_ticket. OrderCreate already
    # enforces gt=0 for HTTP callers, but this function is the only place that
    # writes an Order and it is callable directly (tests, future ARQ jobs), so
    # the invariant belongs here too: the min_ticket check below is skipped
    # entirely when a listing has no min_ticket, and `amount > remaining`
    # cannot catch a negative.
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if min_ticket is not None and amount < min_ticket:
        raise HTTPException(status_code=400, detail="Amount below min_ticket")
    remaining = target_amt - funded_amt
    if amount > remaining:
        raise HTTPException(status_code=400, detail="Amount exceeds remaining capacity")

    now = datetime.now(timezone.utc)
    order = Order(
        listing_id=listing_id,
        investor_id=investor_id,
        amount=amount,
        status="FILLED",
        payment_confirmed_at=now,
        idempotency_key=idempotency_key,
    )
    db.add(order)
    await db.flush()

    new_listing_funded = funded_amt + amount
    lst.funded_amount = new_listing_funded
    result = await db.execute(select(Contract).where(Contract.id == lst.contract_id))
    c = result.scalar_one_or_none()
    if c:
        c.funded_amount = _dec(c.funded_amount) + amount

    result = await db.execute(
        select(Holding).where(Holding.contract_id == lst.contract_id, Holding.investor_id == investor_id)
    )
    holding = result.scalar_one_or_none()
    if holding:
        holding.principal = _dec(holding.principal) + amount
        holding.order_id = order.id
    else:
        holding = Holding(
            contract_id=lst.contract_id,
            investor_id=investor_id,
            order_id=order.id,
            principal=amount,
        )
        db.add(holding)

    if new_listing_funded >= target_amt:
        lst.status = "FUNDED"
        lst.close_at = now
        if c:
            c.status = "ACTIVE_FUNDED"

    db.add(lst)
    if c:
        db.add(c)
    await db.flush()
    await _recompute_holding_shares(db, lst.contract_id)
    await db.flush()
    await db.refresh(order)
    return order, True
