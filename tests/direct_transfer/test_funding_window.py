"""Spec: docs/specs/direct_transfer/funding-window.md section 8 (B5, B6, B7 window parts, B8)."""
import asyncio
import datetime as dt
import re
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import engine
from app.core.timezone import ICT
from app.direct_transfer import close, reservation, tranches, windows
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import (
    CicEvent,
    EvidenceLog,
    FeeInvoice,
    FundingWindow,
    RepaymentDay,
    StandingOffer,
    Tranche,
    Transfer,
)
from app.lending.models import Contract, Holding, Listing, LoanApplication, ScoreRun
from app.repayment_schedule.models import Facility

from .helpers import (
    T0,
    accept,
    at,
    attested_tranche,
    close_now,
    confirmed_tranche,
    fund_window,
    make_investor,
    make_world,
    open_window,
    reserve,
    upload_receipt,
)

M = 1_000_000


async def expect_error(coro, code: str, status: int | None = None):
    with pytest.raises(DirectTransferError) as info:
        await coro
    assert info.value.code == code, (info.value.code, info.value.detail)
    if status is not None:
        assert info.value.status_code == status
    return info.value


async def second_listing(world):
    """A second contract + listing for the same SME business (to test the one-listing-per-SME lock)."""
    db = world.db
    app2 = LoanApplication(project_id=world.project.id, requested_amount=50 * M, duration_months=4, status="APPROVED", admin_approval="APPROVED")
    db.add(app2)
    await db.flush()
    sr = ScoreRun(application_id=app2.id, status="LOCKED", interest_rate_pct=16.0, engine_version="e", params_version="p")
    db.add(sr)
    await db.flush()
    contract = Contract(application_id=app2.id, score_run_id=sr.id, status="ACTIVE_PENDING_FUNDING", final_terms={}, target_amount=50 * M, funded_amount=0)
    db.add(contract)
    await db.flush()
    listing = Listing(contract_id=contract.id, target_amount=50 * M, min_ticket=10 * M, status="DRAFT", funded_amount=0)
    db.add(listing)
    await db.flush()
    return listing


async def open_second(world, listing, *, key=None):
    return await windows.open_funding_window(
        world.db, listing_id=listing.id, sme_user_id=world.sme.id, payment_account_id=world.sme_account_id,
        declared_outside_debt_vnd=0, declaration_accepted=True, idempotency_key=key or f"o2-{uuid.uuid4().hex}",
        now=at(days=20), signer=world.signer, storage=world.storage, params=world.params,
    )


# ----------------------------------------------------------------------------- open
async def test_open_window_signs_offer_and_opens_listing(world):
    window = await open_window(world)
    assert window.status == "OPEN"
    assert window.window_end - window.window_start == dt.timedelta(days=14)
    assert window.target_vnd == 100 * M and window.min_raise_vnd == 50 * M and window.min_ticket_vnd == 10 * M
    assert str(window.annual_rate_pct) == "16.000" and window.term_months == 4
    offer = (await world.db.execute(select(StandingOffer).where(StandingOffer.window_id == window.id))).scalar_one()
    assert len(offer.file_sha256) == 64 and offer.certificate["provider"] == "MOCK" and offer.valid_until == window.window_end
    await world.db.refresh(world.listing)
    assert world.listing.status == "OPEN" and world.listing.close_at == window.window_end
    events = {e.event_type for e in (await world.db.execute(select(EvidenceLog).where(EvidenceLog.entity_id == window.id))).scalars()}
    assert {"WINDOW_OPENED", "CAP_CHECK"} <= events
    # terms are copied from the locked score run and never recomputed (INV-5)
    assert window.offer_terms["engine_version"] == "eng-1" and window.offer_terms["params_version"] == "par-1"


async def test_open_window_rejects_target_above_100m(db_session):
    world = await make_world(db_session, target=150 * M)
    await expect_error(open_window(world), "TARGET_ABOVE_CAP", 400)


async def test_open_window_blocked_when_sme_has_live_window(world):
    await open_window(world)
    listing2 = await second_listing(world)
    await expect_error(open_second(world, listing2), "SME_HAS_LIVE_LISTING", 409)
    # after the first window is terminal (FAILED: nothing raised by day 14) the SME may list again
    await close_now(world, now=at(days=14, minutes=1))
    assert world.window.status == "FAILED"
    second = await open_second(world, listing2)
    assert second.status == "OPEN"


async def test_open_window_blocked_when_sme_invoice_unpaid(world):
    first = await open_window(world)
    await close_now(world, now=at(days=14, minutes=1))
    assert first.status == "FAILED"
    world.db.add(
        FeeInvoice(
            kind="SME_ACTIVATION", window_id=first.id, contract_id=world.contract.id, payer_user_id=world.sme.id, base_vnd=1, fee_vnd=1,
            vat_vnd=0, total_vnd=1, status="ISSUED", invoice_number="INV-X", provider="MANUAL", reference_code="FLAAAAAAAA",
            issued_at=T0, due_at=T0 + dt.timedelta(days=7),
        )
    )
    await world.db.flush()
    listing2 = await second_listing(world)
    await expect_error(open_second(world, listing2), "SME_INVOICE_UNPAID", 409)


async def test_open_window_idempotent(world):
    key = "same-key"
    w1 = await open_window(world, key=key)
    w2 = await open_window(world, key=key)
    assert w1.id == w2.id
    count = (await world.db.execute(select(func.count()).select_from(FundingWindow))).scalar_one()
    assert count == 1
    listing2 = await second_listing(world)
    await expect_error(open_second(world, listing2, key=key), "IDEMPOTENCY_CONFLICT", 409)


async def test_open_window_requires_approved_account(world):
    from .helpers import make_account

    pending = await make_account(world.db, world.sme, role="SME", holder_type="ENTITY", number="55555555")
    with pytest.raises(DirectTransferError) as info:
        await windows.open_funding_window(
            world.db, listing_id=world.listing.id, sme_user_id=world.sme.id, payment_account_id=pending.id,
            declared_outside_debt_vnd=0, declaration_accepted=True, idempotency_key="k", now=T0, signer=world.signer,
            storage=world.storage, params=world.params,
        )
    assert info.value.code == "ACCOUNT_NOT_APPROVED"


async def test_open_window_requires_declaration_and_borrower_cap(world):
    with pytest.raises(DirectTransferError) as info:
        await windows.open_funding_window(
            world.db, listing_id=world.listing.id, sme_user_id=world.sme.id, payment_account_id=world.sme_account_id,
            declared_outside_debt_vnd=0, declaration_accepted=False, idempotency_key="k1", now=T0, signer=world.signer,
            storage=world.storage, params=world.params,
        )
    assert info.value.code == "DECLARATION_REQUIRED"
    await expect_error(open_window(world, declared=1 * M), "BORROWER_CAP_EXCEEDED", 409)  # 100M target + 1M > 100M


async def test_borrower_cap_is_100m_including_declared_outside_debt(db_session):
    # D-054: target plus the SME's declared outside debt may not exceed 100M per legal entity.
    world = await make_world(db_session, target=60 * M)
    await expect_error(open_window(world, declared=50 * M), "BORROWER_CAP_EXCEEDED", 409)
    window = await open_window(world, declared=40 * M)
    assert window.status == "OPEN" and window.declared_outside_debt_vnd == 40 * M


async def test_per_listing_cap_unchanged(db_session):
    # D-054 leaves the per-listing cap (max_target_vnd) at 100M: a full 100M listing with no outside debt opens.
    assert DirectTransferParams().max_target_vnd == 100 * M
    await expect_error(open_window(await make_world(db_session, target=101 * M)), "TARGET_ABOVE_CAP", 400)
    window = await open_window(await make_world(db_session, target=100 * M))
    assert window.target_vnd == 100 * M


async def test_open_window_other_users_listing_returns_404(world, db_session):
    other = await make_investor(world)
    with pytest.raises(DirectTransferError) as info:
        await windows.open_funding_window(
            db_session, listing_id=world.listing.id, sme_user_id=other.user.id, payment_account_id=world.sme_account_id,
            declared_outside_debt_vnd=0, declaration_accepted=True, idempotency_key="k9", now=T0, signer=world.signer,
            storage=world.storage, params=world.params,
        )
    assert info.value.status_code == 404


# ----------------------------------------------------------------------------- reserve
async def test_reserve_happy_path_and_reference_code_format(world):
    window = await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    assert t.status == "RESERVED" and t.amount_vnd == 10 * M
    assert re.fullmatch(r"FL[A-Z0-9]{8}", t.reference_code)
    assert t.hold_expires_at - t.reserved_at == dt.timedelta(hours=2)
    assert t.investor_type == "INDIVIDUAL"
    progress = await windows.get_window_progress(world.db, listing_id=world.listing.id, now=T0)
    assert progress.reserved_vnd == 10 * M and progress.capacity_vnd == 90 * M and progress.confirmed_vnd == 0
    assert progress.days_left == 14 and progress.tranche_count == 1


async def test_reserve_below_min_ticket_rejected(world):
    await open_window(world)
    inv = await make_investor(world)
    await expect_error(reserve(world, inv, 9 * M), "BELOW_MIN_TICKET", 409)


async def test_reserve_above_capacity_rejected(world):
    await open_window(world)
    a, b = await make_investor(world), await make_investor(world)
    await reserve(world, a, 60 * M)
    await expect_error(reserve(world, b, 50 * M), "CAPACITY_EXCEEDED", 409)


async def test_reserve_last_ticket_below_min_allowed_when_equal_to_remaining(world):
    """PROVISIONAL(Q-D3): the final ticket may be below the minimum when it takes exactly the remaining capacity."""
    await open_window(world)
    a, b, c = await make_investor(world), await make_investor(world), await make_investor(world)
    await reserve(world, a, 95 * M)  # capacity left 5M < min ticket 10M
    await expect_error(reserve(world, b, 4 * M), "BELOW_MIN_TICKET", 409)
    last = await reserve(world, c, 5 * M)
    assert last.amount_vnd == 5 * M and world.window.status == "FULL"


async def test_reserve_one_live_tranche_per_investor(world):
    await open_window(world)
    inv = await make_investor(world)
    await reserve(world, inv, 10 * M)
    await expect_error(reserve(world, inv, 10 * M), "ALREADY_HAS_LIVE_TRANCHE", 409)


async def test_reserve_idempotency_key_replay_returns_same_tranche(world):
    await open_window(world)
    inv = await make_investor(world)
    t1 = await reserve(world, inv, 10 * M, key="res-1")
    t2 = await reserve(world, inv, 10 * M, key="res-1")
    assert t1.id == t2.id
    n = (await world.db.execute(select(func.count()).select_from(Tranche))).scalar_one()
    assert n == 1
    await expect_error(reserve(world, inv, 20 * M, key="res-1"), "IDEMPOTENCY_CONFLICT", 409)


async def test_reserve_allowed_when_an_existing_investor_fee_is_unpaid(world):
    window = await open_window(world)
    inv = await make_investor(world)
    world.db.add(
        FeeInvoice(
            kind="INVESTOR_SERVICE", window_id=window.id, contract_id=world.contract.id, payer_user_id=inv.user.id, base_vnd=1,
            fee_vnd=1, vat_vnd=0, total_vnd=1, status="ISSUED", invoice_number="INV-Y", provider="MANUAL",
            reference_code="FLBBBBBBBB", issued_at=T0, due_at=T0 + dt.timedelta(days=7),
        )
    )
    await world.db.flush()
    assert (await reserve(world, inv, 10 * M, now=T0 + dt.timedelta(days=7, hours=1))).status == "RESERVED"


async def test_reserve_requires_risk_ack(world):
    window = await open_window(world)
    inv = await make_investor(world)
    with pytest.raises(DirectTransferError) as info:
        await reservation.reserve_ticket(
            world.db, window_id=window.id, investor_id=inv.user.id, amount_vnd=10 * M, ack_risk_disclosure=False,
            payment_account_id=inv.account_id, idempotency_key="k", now=T0, params=world.params,
        )
    assert info.value.status_code == 400


async def test_reserve_rejects_window_not_open_and_sme_own_user(world):
    window = await open_window(world)
    inv = await make_investor(world)
    await expect_error(reserve(world, inv, 10 * M, now=at(days=15)), "WINDOW_NOT_OPEN", 409)
    from .helpers import make_account

    acct = await make_account(world.db, world.sme, role="INVESTOR", number="777777777", approve_by=world.admin)
    with pytest.raises(DirectTransferError) as info:
        await reservation.reserve_ticket(
            world.db, window_id=window.id, investor_id=world.sme.id, amount_vnd=10 * M, ack_risk_disclosure=True,
            payment_account_id=acct.id, idempotency_key="own", now=T0, params=world.params,
        )
    assert info.value.status_code == 404


async def test_capacity_never_exceeds_target_under_concurrent_reservations(world, db_session):
    window = await open_window(world)
    a, b = await make_investor(world), await make_investor(world)
    await db_session.commit()
    Session = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def attempt(inv):
        async with Session() as s:
            try:
                t = await reservation.reserve_ticket(
                    s, window_id=window.id, investor_id=inv.user.id, amount_vnd=60 * M, ack_risk_disclosure=True,
                    payment_account_id=inv.account_id, idempotency_key=f"c-{inv.user.id}", now=T0, params=world.params,
                )
                await s.commit()
                return "ok"
            except DirectTransferError as exc:
                await s.rollback()
                return exc.code

    results = await asyncio.gather(attempt(a), attempt(b))
    assert sorted(results) == ["CAPACITY_EXCEEDED", "ok"]
    async with Session() as s:
        total = (await s.execute(select(func.coalesce(func.sum(Tranche.amount_vnd), 0)).where(Tranche.window_id == window.id))).scalar_one()
    assert total == 60 * M <= window.target_vnd


async def test_window_becomes_full_at_zero_capacity_and_reopens_on_lapse(world):
    await open_window(world)
    inv = await make_investor(world)
    await reserve(world, inv, 100 * M)
    assert world.window.status == "FULL"
    other = await make_investor(world)
    await expect_error(reserve(world, other, 10 * M), "WINDOW_NOT_OPEN", 409)
    assert await tranches.expire_holds(world.db, now=at(hours=3)) == 1
    await world.db.refresh(world.window)
    assert world.window.status == "OPEN"  # PROVISIONAL(Q-D3): reopens until day 14
    again = await reserve(world, other, 10 * M, now=at(hours=3))
    assert again.status == "RESERVED"


# ----------------------------------------------------------------------------- sign / expire
async def test_accept_moves_reserved_to_signed_and_stores_signature(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=5))
    assert t.status == "SIGNED" and t.signed_at == at(minutes=5)
    assert len(t.acceptance_sha256) == 64 and t.acceptance_certificate["provider"] == "MOCK"
    assert t.acceptance_file_key in world.storage.objects


async def test_accept_after_hold_expiry_rejected(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    with pytest.raises(DirectTransferError) as info:
        await accept(world, inv, t, now=at(hours=3))
    assert info.value.code == "HOLD_EXPIRED"


async def test_expire_holds_expires_reserved_and_signed_without_receipt(world):
    await open_window(world)
    a, b = await make_investor(world), await make_investor(world)
    t1 = await reserve(world, a, 10 * M)
    t2 = await reserve(world, b, 10 * M)
    await accept(world, b, t2, now=at(minutes=5))
    assert await tranches.expire_holds(world.db, now=at(hours=1, minutes=59)) == 0
    assert await tranches.expire_holds(world.db, now=at(hours=2)) == 2
    assert t1.status == "EXPIRED" and t2.status == "EXPIRED"
    assert await tranches.expire_holds(world.db, now=at(hours=3)) == 0  # idempotent


async def test_expire_holds_skips_tranche_with_submitted_receipt(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    res = await upload_receipt(world, inv, t, now=at(minutes=10), fields=None)  # ManualEntry-style: awaiting staff
    assert res.check_status == "AWAITING_FIELDS"
    assert await tranches.expire_holds(world.db, now=at(hours=5)) == 0
    assert t.status == "SIGNED"


# ----------------------------------------------------------------------------- SME confirm / dispute / lapse
async def test_sme_confirm_attested_tranche(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M)
    await tranches.sme_confirm_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, now=at(minutes=30))
    assert t.status == "CONFIRMED" and t.confirmed_at == at(minutes=30)
    transfer = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == t.id))).scalar_one()
    assert transfer.confirmation_level == "CONFIRMED"


async def test_confirm_deadline_is_24_hours_after_the_receipt_is_checked(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M, now=at(days=4))  # Fri 6 Nov 2026 10:00 ICT
    assert t.confirm_deadline == t.attested_at + dt.timedelta(hours=24)  # Saturday (AM-E20; weekend case in test_calendar_days.py)


async def test_lapsed_dispute_window_auto_confirms(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M)  # Monday: deadline 24 hours later
    assert t.confirm_deadline == t.attested_at + dt.timedelta(hours=24)
    assert await tranches.confirm_lapsed_tranches(world.db, now=t.confirm_deadline - dt.timedelta(seconds=1)) == 0
    assert await tranches.confirm_lapsed_tranches(world.db, now=t.confirm_deadline) == 1
    assert t.status == "CONFIRMED"
    assert await tranches.confirm_lapsed_tranches(world.db, now=t.confirm_deadline) == 0  # idempotent


async def test_only_window_sme_can_confirm_or_dispute(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M)
    stranger = await make_investor(world)
    for fn in (
        tranches.sme_confirm_tranche(world.db, tranche_id=t.id, sme_user_id=stranger.user.id, now=at(minutes=30)),
        tranches.sme_dispute_tranche(world.db, tranche_id=t.id, sme_user_id=inv.user.id, statement="x", file_id=None, now=at(minutes=30)),
    ):
        with pytest.raises(DirectTransferError) as info:
            await fn
        assert info.value.status_code == 404


async def test_sme_dispute_moves_to_under_review_with_cause(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M)
    await tranches.sme_dispute_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, statement="Nothing arrived", file_id=None, now=at(minutes=30), params=world.params)
    assert t.status == "UNDER_REVIEW" and t.review_cause == "SME_DISPUTE"
    # no longer counts as confirmed; after the deadline it may not be disputed
    inv2 = await make_investor(world)
    t2 = await attested_tranche(world, inv2, 10 * M)
    with pytest.raises(DirectTransferError) as info:
        await tranches.sme_dispute_tranche(world.db, tranche_id=t2.id, sme_user_id=world.sme.id, statement="late", file_id=None, now=t2.confirm_deadline, params=world.params)
    assert info.value.code == "DISPUTE_WINDOW_CLOSED"


async def test_invalid_transition_returns_400_message(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    with pytest.raises(DirectTransferError) as info:
        await tranches.sme_confirm_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, now=at(minutes=1))
    assert info.value.status_code == 400
    assert info.value.detail == "Invalid state transition: RESERVED → CONFIRMED"


# ----------------------------------------------------------------------------- close
async def test_close_early_blocked_below_min_raise_and_allowed_above(world):
    await open_window(world)
    await fund_window(world, [10 * M, 10 * M, 10 * M, 10 * M])  # 40M < 50M
    await expect_error(
        close.request_early_close(world.db, window_id=world.window.id, sme_user_id=world.sme.id, now=at(days=1), params=world.params),
        "BELOW_MIN_RAISE_FOR_EARLY_CLOSE", 409,
    )
    await fund_window(world, [10 * M, 10 * M], now=at(hours=5))
    window = await close.request_early_close(world.db, window_id=world.window.id, sme_user_id=world.sme.id, now=at(days=1), params=world.params)
    assert window.status == "CLOSING" and window.close_reason == "SME_EARLY"
    other = await make_investor(world)
    with pytest.raises(DirectTransferError) as info:
        await close.request_early_close(world.db, window_id=world.window.id, sme_user_id=other.user.id, now=at(days=1), params=world.params)
    assert info.value.status_code == 404


async def test_window_closes_when_full_and_no_unsigned_tranches(world):
    tr = await fund_window(world, [10 * M] * 10)
    assert world.window.status == "FULL"
    window = await close_now(world, now=at(days=1))
    assert window.status == "REPAYING" and window.close_reason == "FULL"
    assert window.confirmed_total_vnd == 100 * M
    assert all(t.status == "ACTIVE" for t in tr)


async def test_window_closes_on_day_14_and_expires_unsigned(world):
    await fund_window(world, [60 * M])
    inv = await make_investor(world)
    stray = await reserve(world, inv, 10 * M, now=at(days=14, hours=-1))  # hold outlives the window
    window = await close_now(world, now=at(days=14))
    assert window.status == "REPAYING" and window.close_reason == "DAY_14"
    await world.db.refresh(stray)
    assert stray.status == "EXPIRED"
    assert window.confirmed_total_vnd == 60 * M


async def test_close_waits_up_to_two_days_for_open_review(world):
    await fund_window(world, [60 * M])
    inv = await make_investor(world)
    late_now = at(days=14, hours=-1)  # Mon 16 Nov 09:00 ICT
    t = await attested_tranche(world, inv, 10 * M, now=late_now)
    window = await close_now(world, now=at(days=14))
    assert window.status == "CLOSING"
    assert window.close_wait_until == at(days=16)  # Wed 18 Nov 10:00 ICT, two calendar days after the close began
    window = await close_now(world, now=at(days=14, hours=5))  # before the receipt's 24 hours are up
    assert window.status == "CLOSING"
    window = await close_now(world, now=at(days=16))
    assert window.status == "REPAYING"  # the attested tranche lapsed into CONFIRMED and joined
    await world.db.refresh(t)
    assert t.status == "ACTIVE" and window.confirmed_total_vnd == 70 * M


async def test_close_below_50_percent_voids_everything_with_refunds_and_no_fee(world):
    tr = await fund_window(world, [10 * M] * 4)
    window = await close_now(world, now=at(days=14))
    assert window.status == "FAILED"
    for t in tr:
        await world.db.refresh(t)
        assert t.status == "VOID" and t.void_reason == "BELOW_MIN_RAISE"
    refunds = (await world.db.execute(select(Transfer).where(Transfer.kind == "REFUND"))).scalars().all()
    assert len(refunds) == 4 and all(r.amount_vnd == 10 * M and r.check_status == "DUE" and r.due_by for r in refunds)
    assert (await world.db.execute(select(func.count()).select_from(FeeInvoice))).scalar_one() == 0
    await world.db.refresh(world.listing)
    assert world.listing.status == "CANCELLED"
    assert (await world.db.execute(select(func.count()).select_from(Facility))).scalar_one() == 0
    voids = (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "VOID"))).scalars().all()
    assert len(voids) == 4  # attested earlier, now corrected (new events, not edits)


async def test_close_success_creates_facility_holdings_and_activates_tranches(world):
    tr = await fund_window(world, [10 * M] * 10)
    window = await close_now(world, now=at(days=1))
    facility = await world.db.get(Facility, window.facility_id)
    assert facility.status == "ACTIVE" and int(facility.principal_vnd) == 100 * M
    holdings = (await world.db.execute(select(Holding).where(Holding.contract_id == world.contract.id))).scalars().all()
    assert len(holdings) == 10 and all(int(h.principal) == 10 * M for h in holdings)
    await world.db.refresh(world.contract)
    await world.db.refresh(world.listing)
    assert world.contract.status == "ACTIVE_FUNDED" and int(world.contract.funded_amount) == 100 * M
    assert world.listing.status == "FUNDED"
    invoices = (await world.db.execute(select(FeeInvoice))).scalars().all()
    assert [i.kind for i in invoices] == ["SME_ACTIVATION"] and invoices[0].fee_vnd == 3 * M
    assert all(t.holding_id is not None and t.activated_at == at(days=1) for t in tr)


async def test_close_start_date_is_first_working_day_after_close(world):
    await open_window(world, days=5)  # window_end = Saturday 7 Nov 2026 10:00 ICT
    await fund_window(world, [60 * M])
    window = await close_now(world, now=at(days=5))
    assert window.status == "REPAYING" and window.repayment_start_date == dt.date(2026, 11, 9)  # Monday
    first = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.kind == "INSTALMENT", RepaymentDay.day_index == 1))).scalars().first()
    assert first.due_date == dt.date(2026, 11, 9)


async def test_close_start_date_skips_friday_to_monday(db_session):
    world = await make_world(db_session)
    await open_window(world, days=4)  # Friday 6 Nov 10:00
    await fund_window(world, [60 * M])
    window = await close_now(world, now=at(days=4))
    assert window.repayment_start_date == dt.date(2026, 11, 9)


async def test_stub_interest_basis_and_separate_obligation_not_in_t0(world):
    await fund_window(world, [10 * M] * 6)
    window = await close_now(world, now=at(days=14))
    assert window.repayment_start_date == dt.date(2026, 11, 17)
    facility = await world.db.get(Facility, window.facility_id)
    assert int(facility.total_obligation_vnd) == 63_200_000  # T0 = P + I with no stub in it
    # receipt Mon 2 Nov to < Tue 17 Nov = 11 business days: 10M x 16% x 11 / 252 = 69,841.27
    stubs = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.kind == "STUB"))).scalars().all()
    assert len(stubs) == 6
    assert all(s.interest_vnd == 69_841 and s.principal_vnd == 0 and s.due_date == dt.date(2026, 11, 17) for s in stubs)
    ts = (await world.db.execute(select(Tranche))).scalars().all()
    assert all(t.stub_days == 11 and t.stub_interest_vnd == 69_841 for t in ts)
    # the facility schedule is untouched by the stub: instalment rows still sum to T0
    gross = (
        await world.db.execute(select(func.sum(RepaymentDay.gross_vnd)).where(RepaymentDay.kind == "INSTALMENT"))
    ).scalar_one()
    assert gross == 63_200_000


async def test_cap_test_runs_both_ways_and_logs_both(world):
    await fund_window(world, [10 * M] * 6)
    window = await close_now(world, now=at(days=14))
    row = (
        await world.db.execute(select(EvidenceLog).where(EvidenceLog.entity_id == window.id, EvidenceLog.event_type == "STUB_CAP_TEST"))
    ).scalar_one()
    p = row.payload
    assert {"implied_nominal_without_fee", "implied_nominal_with_fee", "within_cap_without_fee", "within_cap_with_fee"} <= p.keys()
    assert p["within_cap_without_fee"] is True
    assert p["activation_fee_vnd"] == 1_800_000
    assert float(p["implied_nominal_with_fee"]) > float(p["implied_nominal_without_fee"])


async def test_stub_interest_reduced_to_cap_headroom(db_session):
    # 19.9%, not 20.0: origination's INV-5 check trips on Decimal division noise at exactly 20% with a 1-month term
    # (pre-existing, reported in HANDOFF-04 notes).
    world = await make_world(db_session, rate=19.9, term=1)
    await fund_window(world, [10 * M] * 6)
    window = await close_now(world, now=at(days=14))
    row = (
        await world.db.execute(select(EvidenceLog).where(EvidenceLog.entity_id == window.id, EvidenceLog.event_type == "STUB_CAP_TEST"))
    ).scalar_one()
    assert row.payload["stub_reduced"] is True
    ts = (await world.db.execute(select(Tranche))).scalars().all()
    # headroom under the 20% cap: floor(60M x 20% x 1/12) - I = 1,000,000 - 995,000 = 5,000, split across 6 tranches
    assert sum(t.stub_interest_vnd for t in ts) == 5_000
    assert row.payload["stub_total_vnd"] == 5_000
    assert (await world.db.execute(select(func.count()).select_from(RepaymentDay).where(RepaymentDay.kind == "STUB"))).scalar_one() == 6


async def test_under_review_at_close_is_excluded_and_never_joins(world):
    await fund_window(world, [60 * M])
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M, now=at(days=14, hours=-1))
    await tranches.sme_dispute_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, statement="not received", file_id=None, now=at(days=14, hours=-1, minutes=-30) + dt.timedelta(minutes=45), params=world.params)
    window = await close_now(world, now=at(days=14))
    assert window.status == "CLOSING"
    window = await close_now(world, now=at(days=16))
    assert window.status == "REPAYING" and window.confirmed_total_vnd == 60 * M
    await world.db.refresh(t)
    assert t.status == "UNDER_REVIEW" and t.excluded_at_close is True and t.holding_id is None


async def test_window_cancelled_before_any_confirmed_frees_sme(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    window = await close.cancel_window(world.db, window_id=world.window.id, staff_id=world.admin.id, reason="KYB failed", now=at(hours=1))
    assert window.status == "CANCELLED" and t.status == "EXPIRED"
    await world.db.refresh(world.listing)
    assert world.listing.status == "CANCELLED"
    listing2 = await second_listing(world)
    assert (await open_second(world, listing2)).status == "OPEN"
    await expect_error(
        close.cancel_window(world.db, window_id=world.window.id, staff_id=world.admin.id, reason="again", now=at(hours=2)),
        "WINDOW_NOT_CANCELLABLE",
    )


async def test_cancel_refused_when_money_reported(world):
    await open_window(world)
    inv = await make_investor(world)
    await attested_tranche(world, inv, 10 * M)
    await expect_error(
        close.cancel_window(world.db, window_id=world.window.id, staff_id=world.admin.id, reason="x", now=at(hours=1)),
        "WINDOW_NOT_CANCELLABLE",
    )


async def test_every_transition_appends_evidence_log(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await confirmed_tranche(world, inv, 10 * M)
    rows = (
        await world.db.execute(
            select(EvidenceLog).where(EvidenceLog.entity_id == t.id, EvidenceLog.event_type == "TRANCHE_TRANSITION").order_by(EvidenceLog.occurred_at, EvidenceLog.created_at)
        )
    ).scalars().all()
    assert [(r.payload["from"], r.payload["to"]) for r in rows] == [
        (None, "RESERVED"), ("RESERVED", "SIGNED"), ("SIGNED", "ATTESTED"), ("ATTESTED", "CONFIRMED"),
    ]
    assert rows[-1].actor_role == "SME"


async def test_advance_window_is_idempotent(world):
    await fund_window(world, [10 * M] * 10)
    await close_now(world, now=at(days=1))
    before = (await world.db.execute(select(func.count()).select_from(RepaymentDay))).scalar_one()
    await close_now(world, now=at(days=1, hours=1))
    after = (await world.db.execute(select(func.count()).select_from(RepaymentDay))).scalar_one()
    assert before == after
    assert (await world.db.execute(select(func.count()).select_from(Facility))).scalar_one() == 1
