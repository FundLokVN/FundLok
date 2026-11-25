"""Staff approve late money into the loan before 15:30 (docs/specs/direct_transfer/late-money-approval.md section 8).

T0 is Monday 2 Nov 2026; the tests make that day Day 14 for the window, so its cut-offs are 14:45, 15:00 and 15:30.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from app.admin import funding as admin_funding
from app.core.timezone import ICT
from app.direct_transfer import day14, late_money, tranches
from app.direct_transfer.day14_times import set_day14_cutoffs
from app.direct_transfer.disputes import open_tranche_dispute
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import EvidenceLog, LenderStanding, TopUpPledge, Transfer
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.windows import capacity_left, confirmed_total
from app.lending.models import AuditLog

from .helpers import T0, accept, good_fields, make_investor, open_window, reserve, upload_receipt

DAY = T0.date()


def ict(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=ICT)


async def _late_money(world, *, amount: int = 10_000_000):
    """A signed ticket whose right transfer came outside its QR's 15 minutes: UNDER_REVIEW[LATE_MONEY]."""
    if world.window is None:
        await open_window(world)
        set_day14_cutoffs(world.window, DAY, world.params)
    investor = await make_investor(world)
    tranche = await reserve(world, investor, amount, now=ict(10, 0))
    await accept(world, investor, tranche, now=ict(10, 0))
    pay = await tranches.payment_instructions(
        world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=ict(10, 1), params=world.params
    )
    fields = good_fields(world, tranche, investor, when=ict(10, 30), memo=pay.qr_code, amount_vnd=amount)
    await upload_receipt(world, investor, tranche, now=ict(10, 31), fields=fields)
    assert tranche.status == "UNDER_REVIEW" and tranche.review_cause == "LATE_MONEY"
    transfer = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id))).scalars().first()
    return investor, tranche, transfer


async def _approve(world, transfer, *, now, reason="Bank statement shows the transfer at 14:52", staff=None):
    return await late_money.approve_late_money(
        world.db, transfer_id=transfer.id, staff_id=(staff or world.admin).id, reason=reason, now=now,
        params=world.params,
    )


async def _code(awaitable) -> str:
    with pytest.raises(DirectTransferError) as info:
        await awaitable
    return info.value.code


async def test_staff_approve_late_money_before_1530_moves_it_to_attested_with_a_24_hour_sme_check(world):
    _, tranche, transfer = await _late_money(world)
    now = ict(15, 10)
    await _approve(world, transfer, now=now)
    assert tranche.status == "ATTESTED" and tranche.review_cause is None
    assert tranche.confirm_deadline == now + dt.timedelta(hours=24)
    assert transfer.confirmation_level == "ATTESTED"


async def test_approved_late_money_counts_toward_the_round_once_the_sme_check_lapses(world):
    _, tranche, transfer = await _late_money(world, amount=10_000_000)
    assert await confirmed_total(world.db, world.window.id) == 0
    await _approve(world, transfer, now=ict(15, 10))
    await tranches.confirm_lapsed_tranches(world.db, now=ict(15, 10) + dt.timedelta(hours=24), params=world.params)
    assert tranche.status == "CONFIRMED"
    assert await confirmed_total(world.db, world.window.id) == 10_000_000  # the lender's full amount


async def test_approval_at_or_after_1530_is_refused_and_the_1535_run_records_the_pledge(world):
    _, tranche, transfer = await _late_money(world)
    assert await _code(_approve(world, transfer, now=ict(15, 30))) == "LATE_MONEY_APPROVAL_CLOSED"
    assert tranche.status == "UNDER_REVIEW"
    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    assert [p.tranche_id for p in outcome.pledges] == [tranche.id]


async def _paid_in_time(world, amount: int = 10_000_000):
    investor = await make_investor(world)
    tranche = await reserve(world, investor, amount, now=ict(10, 0))
    await accept(world, investor, tranche, now=ict(10, 0))
    pay = await tranches.payment_instructions(
        world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=ict(10, 1), params=world.params
    )
    fields = good_fields(world, tranche, investor, when=ict(10, 5), memo=pay.qr_code, amount_vnd=amount)
    await upload_receipt(world, investor, tranche, now=ict(10, 6), fields=fields)
    assert tranche.status == "ATTESTED"
    return tranche


async def test_late_money_can_be_approved_on_a_full_round(world):
    # Target 100M: one lender's 10M came late, nine others paid 90M in time. Late money holds no room, so the 10M it
    # needs is still there and staff can approve it; before, it was counted against itself (CAPACITY_EXCEEDED).
    _, tranche, transfer = await _late_money(world)
    for _ in range(9):
        await _paid_in_time(world)
    assert await capacity_left(world.db, world.window) == 10_000_000
    await _approve(world, transfer, now=ict(15, 10))
    assert tranche.status == "ATTESTED"
    assert await capacity_left(world.db, world.window) == 0


async def test_late_money_is_refused_when_other_lenders_took_the_room(world):
    # The same round, but ten lenders paid 100M in time: there is no room left for the late 10M, so it stays late
    # money (a top-up after 15:30) and the round is never overbooked.
    _, tranche, transfer = await _late_money(world)
    for _ in range(10):
        await _paid_in_time(world)
    assert await capacity_left(world.db, world.window) == 0
    # This test round turns FULL, which closes approval before the room is even checked; a commit-then-accept round
    # stays AWAITING_PAYMENT through Day 14, so put it there to reach the room check.
    world.window.status = "AWAITING_PAYMENT"
    assert await _code(_approve(world, transfer, now=ict(15, 10))) == "CAPACITY_EXCEEDED"
    assert tranche.status == "UNDER_REVIEW" and tranche.review_cause == "LATE_MONEY"


async def test_approved_late_money_records_no_top_up_pledge(world):
    _, tranche, transfer = await _late_money(world)
    await _approve(world, transfer, now=ict(15, 10))
    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    assert outcome.pledges == []
    assert (await world.db.execute(select(func.count()).select_from(TopUpPledge))).scalar_one() == 0
    assert tranche.status == "ATTESTED"


async def test_the_1535_run_unlocks_a_lender_whose_late_money_was_approved(world):
    investor, _, transfer = await _late_money(world)
    await day14.process_day14_no_shows(world.db, now=ict(15, 0), params=world.params)
    await _approve(world, transfer, now=ict(15, 10))
    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    assert outcome.released == [investor.user.id]
    assert (await world.db.get(LenderStanding, investor.user.id)).status == "GOOD"


async def test_a_lender_locked_at_1500_stays_locked_until_the_1535_run(world):
    investor, _, transfer = await _late_money(world)
    await day14.process_day14_no_shows(world.db, now=ict(15, 0), params=world.params)
    await _approve(world, transfer, now=ict(15, 10))
    assert (await world.db.get(LenderStanding, investor.user.id)).status == "LOCKED"


async def test_approval_needs_a_clean_matched_receipt_and_no_open_dispute(world):
    # A receipt that fails a check is RECEIPT_MISMATCH, not LATE_MONEY: staff use the review route for it.
    await open_window(world)
    set_day14_cutoffs(world.window, DAY, world.params)
    investor = await make_investor(world)
    tranche = await reserve(world, investor, 10_000_000, now=ict(10, 0))
    await accept(world, investor, tranche, now=ict(10, 0))
    pay = await tranches.payment_instructions(
        world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=ict(10, 1), params=world.params
    )
    fields = good_fields(world, tranche, investor, when=ict(10, 30), memo=pay.qr_code, payer_name="SOMEONE ELSE")
    await upload_receipt(world, investor, tranche, now=ict(10, 31), fields=fields)
    bad = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id))).scalars().first()
    assert await _code(_approve(world, bad, now=ict(15, 10))) == "NOT_LATE_MONEY"

    _, clean, transfer = await _late_money(world)
    await open_tranche_dispute(world.db, tranche=clean, cause="RECEIPT_MISMATCH", now=ict(14, 0), params=world.params)
    assert await _code(_approve(world, transfer, now=ict(15, 10))) == "NOT_LATE_MONEY"


async def test_approval_is_refused_for_a_ticket_that_is_not_late_money(world):
    await open_window(world)
    set_day14_cutoffs(world.window, DAY, world.params)
    investor = await make_investor(world)
    tranche = await reserve(world, investor, 10_000_000, now=ict(10, 0))
    await accept(world, investor, tranche, now=ict(10, 0))
    pay = await tranches.payment_instructions(
        world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=ict(10, 1), params=world.params
    )
    fields = good_fields(world, tranche, investor, when=ict(10, 5), memo=pay.qr_code)
    await upload_receipt(world, investor, tranche, now=ict(10, 6), fields=fields)
    assert tranche.status == "ATTESTED"  # on time
    transfer = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id))).scalars().first()
    assert await _code(_approve(world, transfer, now=ict(15, 10))) == "NOT_LATE_MONEY"


async def test_approval_twice_changes_nothing_the_second_time(world):
    _, tranche, transfer = await _late_money(world)
    await _approve(world, transfer, now=ict(15, 10))
    before = (tranche.status, tranche.confirm_deadline)
    assert await _code(_approve(world, transfer, now=ict(15, 11))) == "ALREADY_APPROVED"
    assert (tranche.status, tranche.confirm_deadline) == before


async def test_staff_cannot_approve_their_own_transfer(world):
    investor, tranche, transfer = await _late_money(world)
    with pytest.raises(DirectTransferError) as info:
        await late_money.approve_late_money(
            world.db, transfer_id=transfer.id, staff_id=investor.user.id, reason="mine", now=ict(15, 10),
            params=world.params,
        )
    assert info.value.status_code == 403
    assert tranche.status == "UNDER_REVIEW"


async def test_a_blank_reason_is_refused(world):
    _, tranche, transfer = await _late_money(world)
    with pytest.raises(DirectTransferError) as info:
        await _approve(world, transfer, now=ict(15, 10), reason="   ")
    assert (info.value.status_code, info.value.code) == (422, "REASON_REQUIRED")
    assert tranche.status == "UNDER_REVIEW"


async def test_approval_is_audited_and_logged_with_the_staff_member_and_reason(world, monkeypatch):
    _, tranche, transfer = await _late_money(world)
    monkeypatch.setattr(admin_funding, "_now", lambda: ict(15, 10))  # the wrapper reads the server clock
    admin_id, tranche_id = world.admin.id, tranche.id  # the wrapper expires the session
    await admin_funding.approve_late_money_transfer(
        world.db, transfer.id, reason="Statement checked", actor=world.admin, ip_address="10.0.0.9"
    )
    audit = (
        await world.db.execute(
            select(AuditLog).where(AuditLog.entity_id == tranche_id, AuditLog.action == "LATE_MONEY_APPROVED")
        )
    ).scalar_one()
    assert audit.actor_id == admin_id and audit.after_state["reason"] == "Statement checked"
    event = (
        await world.db.execute(
            select(EvidenceLog).where(EvidenceLog.entity_id == tranche_id, EvidenceLog.event_type == "LATE_MONEY_APPROVED")
        )
    ).scalar_one()
    assert event.actor_id == admin_id and event.reason == "Statement checked"
    assert event.payload["amount_vnd"] == 10_000_000


async def test_approval_never_bypasses_the_sme_receipt_check(world):
    _, tranche, transfer = await _late_money(world)
    await _approve(world, transfer, now=ict(15, 10))
    assert tranche.status == "ATTESTED"  # not CONFIRMED: the SME's 24 hours still run
    await tranches.confirm_lapsed_tranches(
        world.db, now=ict(15, 10) + dt.timedelta(hours=23, minutes=59), params=world.params
    )
    assert tranche.status == "ATTESTED"


async def test_a_late_money_ticket_shows_under_needs_action_until_1530(world):
    _, tranche, transfer = await _late_money(world)
    page = await admin_funding.list_transfers(
        world.db, status="NEEDS_ACTION", window_id=world.window.id, page=1, page_size=20
    )
    assert [row.id for row in page.items] == [tranche.id]
    assert await late_money.approval_blocker(
        world.db, tranche=tranche, window=world.window, transfer=transfer, now=ict(15, 29), params=world.params
    ) is None
    assert await late_money.approval_blocker(
        world.db, tranche=tranche, window=world.window, transfer=transfer, now=ict(15, 30), params=world.params
    ) == "LATE_MONEY_APPROVAL_CLOSED"


async def test_the_new_transition_is_only_allowed_for_a_late_money_ticket(world):
    investor, tranche, _ = await _late_money(world)
    tranche.review_cause = "RECEIPT_MISMATCH"
    with pytest.raises(DirectTransferError) as info:
        await transition_tranche(world.db, tranche, "ATTESTED", now=ict(15, 10))
    assert info.value.code == "INVALID_TRANSITION"
    assert tranche.status == "UNDER_REVIEW"
