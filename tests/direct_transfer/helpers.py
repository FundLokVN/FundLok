"""Shared builders for tests/direct_transfer (fixtures only: no real bank, no real signer, no wall clock)."""
from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import ICT
from app.direct_transfer import close, reservation, tranches, windows
from app.direct_transfer.accounts import decide_payment_account, register_payment_account
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.models import FundingWindow, Tranche
from app.direct_transfer.receipts import ReceiptFields
from app.direct_transfer.signing import MockSigner
from app.direct_transfer.storage import InMemoryStorage
from app.lending.models import Contract, Listing, LoanApplication, Project, ProjectOwnership, ScoreRun
from app.users.models import User
from tests.tax_codes import fake_tax_code

# Monday 2 Nov 2026, 10:00 ICT. Window days 14 -> Monday 16 Nov 2026 10:00 ICT. No VN public holidays in this range.
T0 = dt.datetime(2026, 11, 2, 10, 0, tzinfo=ICT)
_counter = itertools.count(1)


def at(days: int = 0, hours: int = 0, minutes: int = 0) -> dt.datetime:
    return T0 + dt.timedelta(days=days, hours=hours, minutes=minutes)


class StaticExtractor:
    """Test double for ReceiptExtractor: returns the fields it was given (never touches a network)."""

    def __init__(self):
        self.next_fields: ReceiptFields | None = None

    async def extract(self, *, content: bytes, content_type: str):
        return self.next_fields


@dataclasses.dataclass
class Investor:
    user: User
    account_id: uuid.UUID
    name: str


@dataclasses.dataclass
class World:
    db: AsyncSession
    sme: User
    project: Project
    application: LoanApplication
    score_run: ScoreRun
    contract: Contract
    listing: Listing
    sme_account_id: uuid.UUID
    sme_account_number: str
    admin: User
    storage: InMemoryStorage
    signer: MockSigner
    extractor: StaticExtractor
    params: DirectTransferParams
    window: FundingWindow | None = None


async def make_user(db: AsyncSession, *, role: str, full_name: str, email: str | None = None) -> User:
    user = User(email=email or f"{role.lower()}-{uuid.uuid4().hex[:10]}@example.com", full_name=full_name, role=role, email_verified=True)
    db.add(user)
    await db.flush()
    return user


async def make_account(
    db: AsyncSession, user: User, *, role: str, holder_type: str = "INDIVIDUAL", number: str | None = None,
    name: str | None = None, bin_: str = "970407", approve_by: User | None = None,
):
    number = number or f"{next(_counter):010d}"
    account = await register_payment_account(
        db, user_id=user.id, holder_role=role, holder_type=holder_type, bank_bin=bin_, bank_name="Techcombank",
        account_number=number, account_name=name or user.full_name, evidence_file_id=None, now=T0,
    )
    if approve_by is not None:
        # Staff record the holder name seen at the bank. Here it equals the account name; for an SME with no
        # approved KYB (no verified name to compare with) the difference is accepted explicitly.
        await decide_payment_account(
            db, account_id=account.id, decision="APPROVED", reason="statement checked", staff_id=approve_by.id, now=T0,
            verified_holder_name=name or user.full_name, accept_difference=True,
        )
    return account


async def make_world(
    db: AsyncSession, *, target: int = 100_000_000, rate: float = 16.0, term: int = 4, params: DirectTransferParams | None = None,
    listing_min_ticket: int = 10_000_000,
) -> World:
    # The spec's worked examples have one loan formed at close; repaying from approval (the product default) is
    # tested in tests/market.
    params = params or DirectTransferParams(repay_from_approval=False)
    admin = await make_user(db, role="ADMIN", full_name="Staff Admin")
    sme = await make_user(db, role="SME", full_name="Cong Ty ABC")
    project = Project(legal_name=f"ABC Co {uuid.uuid4().hex[:6]}", tax_id=fake_tax_code())
    db.add(project)
    await db.flush()
    db.add(ProjectOwnership(project_id=project.id, user_id=sme.id))
    application = LoanApplication(
        project_id=project.id, requested_amount=target, duration_months=term, status="APPROVED", admin_approval="APPROVED"
    )
    db.add(application)
    await db.flush()
    score_run = ScoreRun(
        application_id=application.id, status="LOCKED", interest_rate_pct=rate, engine_version="eng-1", params_version="par-1"
    )
    db.add(score_run)
    await db.flush()
    contract = Contract(
        application_id=application.id, score_run_id=score_run.id, status="ACTIVE_PENDING_FUNDING", final_terms={}, target_amount=target,
        funded_amount=0,
    )
    db.add(contract)
    await db.flush()
    listing = Listing(contract_id=contract.id, target_amount=target, min_ticket=listing_min_ticket, status="DRAFT", funded_amount=0)
    db.add(listing)
    await db.flush()
    sme_account = await make_account(db, sme, role="SME", holder_type="ENTITY", number="19036789012", name="CONG TY ABC", approve_by=admin)
    return World(
        db=db, sme=sme, project=project, application=application, score_run=score_run, contract=contract, listing=listing,
        sme_account_id=sme_account.id, sme_account_number=sme_account.account_number, admin=admin, storage=InMemoryStorage(),
        signer=MockSigner(), extractor=StaticExtractor(), params=params,
    )


async def open_window(world: World, *, now: dt.datetime = T0, key: str | None = None, declared: int = 0, days: int | None = None) -> FundingWindow:
    window = await windows.open_funding_window(
        world.db, listing_id=world.listing.id, sme_user_id=world.sme.id, payment_account_id=world.sme_account_id,
        declared_outside_debt_vnd=declared, declaration_accepted=True, idempotency_key=key or f"open-{uuid.uuid4().hex}",
        now=now, signer=world.signer, storage=world.storage, window_days=days, params=world.params,
    )
    world.window = window
    return window


async def make_investor(world: World, *, name: str | None = None, holder_type: str = "INDIVIDUAL") -> Investor:
    name = name or f"Nguyen Van {chr(64 + next(_counter) % 26 + 1)}{next(_counter)}"
    user = await make_user(world.db, role="INVESTOR", full_name=name)
    account = await make_account(world.db, user, role="INVESTOR", holder_type=holder_type, name=name, approve_by=world.admin)
    return Investor(user=user, account_id=account.id, name=name)


async def reserve(world: World, investor: Investor, amount: int, *, now: dt.datetime = T0, key: str | None = None) -> Tranche:
    return await reservation.reserve_ticket(
        world.db, window_id=world.window.id, investor_id=investor.user.id, amount_vnd=amount, ack_risk_disclosure=True,
        payment_account_id=investor.account_id, idempotency_key=key or f"res-{uuid.uuid4().hex}", now=now, params=world.params,
    )


def good_fields(world: World, tranche: Tranche, investor: Investor, *, when: dt.datetime, **overrides) -> ReceiptFields:
    base = dict(
        amount_vnd=tranche.amount_vnd,
        transferred_at=when,
        payer_name=investor.name.upper(),
        payer_account="0011223344",
        payee_account=world.sme_account_number,
        memo=f"{tranche.reference_code} cho vay",
        bank_txn_id=f"FT{uuid.uuid4().hex[:12].upper()}",
    )
    base.update(overrides)
    return ReceiptFields(**base)


async def accept(world: World, investor: Investor, tranche: Tranche, *, now: dt.datetime = T0) -> Tranche:
    return await tranches.accept_tranche(
        world.db, tranche_id=tranche.id, investor_id=investor.user.id, document=None, signer=world.signer, storage=world.storage,
        now=now, params=world.params,
    )


async def upload_receipt(
    world: World, investor: Investor, tranche: Tranche, *, now: dt.datetime, fields: ReceiptFields | None = ..., key: str | None = None,
    content: bytes | None = None,
):
    world.extractor.next_fields = good_fields(world, tranche, investor, when=now) if fields is ... else fields
    return await tranches.submit_receipt(
        world.db, tranche_id=tranche.id, investor_id=investor.user.id, content=content or f"receipt-{uuid.uuid4().hex}".encode(),
        content_type="application/pdf", filename="r.pdf", extractor=world.extractor, storage=world.storage,
        idempotency_key=key, now=now, params=world.params,
    )


async def attested_tranche(world: World, investor: Investor, amount: int, *, now: dt.datetime = T0) -> Tranche:
    tranche = await reserve(world, investor, amount, now=now)
    await accept(world, investor, tranche, now=now + dt.timedelta(minutes=1))
    await upload_receipt(world, investor, tranche, now=now + dt.timedelta(minutes=5))
    await world.db.refresh(tranche)
    assert tranche.status == "ATTESTED", tranche.status
    return tranche


async def confirmed_tranche(world: World, investor: Investor, amount: int, *, now: dt.datetime = T0) -> Tranche:
    tranche = await attested_tranche(world, investor, amount, now=now)
    await tranches.sme_confirm_tranche(world.db, tranche_id=tranche.id, sme_user_id=world.sme.id, now=now + dt.timedelta(minutes=10))
    await world.db.refresh(tranche)
    assert tranche.status == "CONFIRMED"
    return tranche


async def fund_window(
    world: World, amounts: list[int], *, now: dt.datetime = T0, holder_types: list[str] | None = None
) -> list[Tranche]:
    """Open (if needed) and fully confirm one tranche per amount, each from a new investor."""
    if world.window is None:
        await open_window(world, now=now)
    out = []
    for i, amount in enumerate(amounts):
        inv = await make_investor(world, holder_type=(holder_types[i] if holder_types else "INDIVIDUAL"))
        t = await confirmed_tranche(world, inv, amount, now=now + dt.timedelta(minutes=20 * i))
        out.append(t)
    return out


async def close_now(world: World, *, now: dt.datetime) -> FundingWindow:
    window = await close.advance_window(world.db, window_id=world.window.id, now=now, params=world.params)
    await world.db.refresh(window)
    return window
