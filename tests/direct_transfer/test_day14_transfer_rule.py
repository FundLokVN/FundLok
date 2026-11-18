"""Day-14 transfer rule (docs/specs/direct_transfer/day14-transfer-rule.md section 8, backend criteria)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.core.timezone import ICT
from app.direct_transfer import day14, standing, tranches
from app.direct_transfer.day14_times import set_day14_cutoffs
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import LenderStanding, PaymentQr, TopUpPledge, Transfer
from app.gverify.models import STATUS_APPROVED, GVerifyVerification

from .helpers import T0, accept, at, good_fields, make_investor, open_window, reserve, upload_receipt

# T0 is Monday 2 Nov 2026, 10:00 ICT. The Day-14 tests make that day "disbursement day" for the window, so its
# cut-offs are 14:45, 15:00 and 15:30 on 2 Nov while the window itself stays open (window_end 16 Nov).
DAY = T0.date()


def ict(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hour, minute, second, tzinfo=ICT)


async def _signed(world, *, now=T0):
    investor = await make_investor(world)
    tranche = await reserve(world, investor, 10_000_000, now=now)
    await accept(world, investor, tranche, now=now)
    return investor, tranche


async def _pay(world, investor, tranche, *, now):
    return await tranches.payment_instructions(
        world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=now, params=world.params
    )


async def _receipt(world, tranche) -> Transfer:
    return (
        await world.db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id).order_by(Transfer.created_at.desc()))
    ).scalars().first()


def _day14(world):
    set_day14_cutoffs(world.window, DAY, world.params)


# --------------------------------------------------------------------------- #
# codes
# --------------------------------------------------------------------------- #
def test_code_in_memo_reads_the_four_digits_after_the_reference():
    assert day14.code_in_memo("FLAB12CD344821 chuyen tien", "FLAB12CD34") == "FLAB12CD344821"
    assert day14.code_in_memo("ck fl-ab12cd34 4821", "FLAB12CD34") == "FLAB12CD344821"  # bank dropped the spacing
    assert day14.code_in_memo("FLAB12CD34 cho vay", "FLAB12CD34") is None
    assert day14.code_in_memo("FLAB12CD3448", "FLAB12CD34") is None
    assert day14.code_in_memo(None, "FLAB12CD34") is None
    # Every occurrence is read; an issued code wins over an unknown one.
    assert day14.code_in_memo("FLAB12CD34 / FLAB12CD344821", "FLAB12CD34") == "FLAB12CD344821"
    assert (
        day14.code_in_memo("FLAB12CD341111 FLAB12CD344821", "FLAB12CD34", {"FLAB12CD344821"}) == "FLAB12CD344821"
    )


async def test_qr_code_is_reference_plus_4_digits_and_unique(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    pay = await _pay(world, investor, tranche, now=at(minutes=1))
    assert pay.qr_code.startswith(tranche.reference_code)
    assert len(pay.qr_code) == len(tranche.reference_code) + 4 and pay.qr_code[-4:].isdigit()
    assert pay.qr_code in pay.vietqr_payload  # the VietQR carries the code as the memo
    again = await _pay(world, investor, tranche, now=at(minutes=2))
    assert again.qr_code == pay.qr_code  # reading again shows the same QR


async def test_qr_expires_after_15_minutes_and_never_after_the_hold(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    pay = await _pay(world, investor, tranche, now=at(minutes=1))
    assert pay.qr_expires_at - pay.qr_issued_at == dt.timedelta(minutes=15)
    assert pay.pay_deadline_at is None and pay.receipt_deadline_at is None  # LEGACY: no Day-14 cut-offs

    near_end = tranche.hold_expires_at - dt.timedelta(minutes=5)
    qr = await day14.issue_new_qr(world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=near_end)
    assert qr.expires_at == tranche.hold_expires_at


async def test_qr_never_runs_past_1445_on_disbursement_day(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(14, 0))
    pay = await _pay(world, investor, tranche, now=ict(14, 40))
    assert pay.qr_expires_at == ict(14, 45)
    assert pay.pay_deadline_at == ict(14, 45) and pay.receipt_deadline_at == ict(15, 30)


async def test_new_qr_supersedes_the_previous_one_at_most_once_a_minute(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    first = await _pay(world, investor, tranche, now=at(minutes=1))

    with pytest.raises(DirectTransferError) as too_soon:
        await day14.issue_new_qr(world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=at(minutes=1, ) + dt.timedelta(seconds=30))
    assert too_soon.value.status_code == 429 and too_soon.value.code == "QR_TOO_SOON"
    assert too_soon.value.retry_after == 30

    second = await day14.issue_new_qr(world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=at(minutes=3))
    assert second.code != first.qr_code
    old = (await world.db.execute(select(PaymentQr).where(PaymentQr.code == first.qr_code))).scalar_one()
    assert old.superseded_at == at(minutes=3)
    pay = await _pay(world, investor, tranche, now=at(minutes=4))
    assert pay.qr_code == second.code


async def test_qr_and_signing_are_refused_after_1445(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(14, 0))
    for call in (
        lambda: _pay(world, investor, tranche, now=ict(14, 45)),
        lambda: day14.issue_new_qr(world.db, tranche_id=tranche.id, investor_id=investor.user.id, now=ict(14, 50)),
    ):
        with pytest.raises(DirectTransferError) as closed:
            await call()
        assert closed.value.code == "PAY_DEADLINE_PASSED"

    late_investor = await make_investor(world)
    late = await reserve(world, late_investor, 10_000_000, now=ict(14, 40))
    with pytest.raises(DirectTransferError) as closed:
        await accept(world, late_investor, late, now=ict(14, 46))
    assert closed.value.code == "PAY_DEADLINE_PASSED"


# --------------------------------------------------------------------------- #
# the on-time check
# --------------------------------------------------------------------------- #
async def test_on_time_needs_the_exact_code_and_a_time_inside_its_15_minutes(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    pay = await _pay(world, investor, tranche, now=at(minutes=1))
    fields = good_fields(world, tranche, investor, when=at(minutes=6), memo=f"{pay.qr_code} cho vay")
    await upload_receipt(world, investor, tranche, now=at(minutes=7), fields=fields)
    receipt = await _receipt(world, tranche)
    assert (receipt.payment_timing, receipt.late_reason, receipt.qr_code) == ("ON_TIME", None, pay.qr_code)
    assert tranche.status == "ATTESTED"


async def test_a_minute_only_receipt_time_counts_if_that_minute_overlaps(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    pay = await _pay(world, investor, tranche, now=at(minutes=1, ) + dt.timedelta(seconds=20))  # expires 10:16:20
    read_to_the_minute = at(minutes=16) + dt.timedelta(seconds=59)  # OCR stores 10:16 as 10:16:59
    fields = good_fields(world, tranche, investor, when=read_to_the_minute, memo=pay.qr_code)
    await upload_receipt(world, investor, tranche, now=at(minutes=18), fields=fields)
    assert (await _receipt(world, tranche)).payment_timing == "ON_TIME"


async def test_on_a_legacy_round_a_late_code_is_recorded_and_changes_nothing(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    pay = await _pay(world, investor, tranche, now=at(minutes=1))
    fields = good_fields(world, tranche, investor, when=at(minutes=30), memo=pay.qr_code)
    await upload_receipt(world, investor, tranche, now=at(minutes=31), fields=fields)
    receipt = await _receipt(world, tranche)
    assert (receipt.payment_timing, receipt.late_reason) == ("LATE", "EXPIRED_CODE")
    assert tranche.status == "ATTESTED"  # still inside the 2-hour hold: today's rules decide


async def test_a_memo_without_a_code_is_no_qr_code(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    await _pay(world, investor, tranche, now=at(minutes=1))
    await upload_receipt(world, investor, tranche, now=at(minutes=7))  # memo: the bare reference
    receipt = await _receipt(world, tranche)
    assert (receipt.payment_timing, receipt.late_reason) == ("LATE", "NO_QR_CODE")


async def test_a_ticket_that_never_had_a_qr_code_is_not_judged(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    await upload_receipt(world, investor, tranche, now=at(minutes=7))
    receipt = await _receipt(world, tranche)
    assert receipt.payment_timing is None and receipt.qr_code is None
    assert tranche.status == "ATTESTED"


# --------------------------------------------------------------------------- #
# Day-14 cut-offs, the 15:00 lock and the top-up pledge
# --------------------------------------------------------------------------- #
async def test_late_money_is_held_for_review_then_becomes_a_top_up_pledge_at_1500(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(10, 0))
    pay = await _pay(world, investor, tranche, now=ict(10, 1))
    fields = good_fields(world, tranche, investor, when=ict(10, 30), memo=pay.qr_code, amount_vnd=tranche.amount_vnd)
    await upload_receipt(world, investor, tranche, now=ict(10, 31), fields=fields)
    assert tranche.status == "UNDER_REVIEW" and tranche.review_cause == "LATE_MONEY"

    before = await day14.process_day14_no_shows(world.db, now=ict(14, 59), params=world.params)
    assert before.locked == [] and before.pledges == []

    at_three = await day14.process_day14_no_shows(world.db, now=ict(15, 0), params=world.params)
    assert at_three.pledges == [] and [lock.reason for lock in at_three.locked] == ["LATE_MONEY"]
    assert tranche.status == "UNDER_REVIEW"  # receipts are still taken until 15:30

    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    assert [p.tranche_id for p in outcome.pledges] == [tranche.id]
    pledge = (await world.db.execute(select(TopUpPledge).where(TopUpPledge.tranche_id == tranche.id))).scalar_one()
    assert (pledge.amount_vnd, pledge.reason, pledge.status) == (10_000_000, "EXPIRED_CODE", "PLEDGED")
    assert tranche.status == "VOID" and tranche.void_reason == "LATE_MONEY_TOP_UP"
    lender = await world.db.get(LenderStanding, investor.user.id)
    assert (lender.status, lender.no_show_count) == ("LOCKED", 0)

    again = await day14.process_day14_no_shows(world.db, now=ict(15, 40), params=world.params)
    assert again.pledges == [] and again.locked == []  # idempotent


async def test_a_lender_who_sent_nothing_is_locked_as_a_no_show_without_a_pledge(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(13, 30))
    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 0), params=world.params)
    assert [(lock.investor_id, lock.reason, lock.newly_locked) for lock in outcome.locked] == [
        (investor.user.id, "NO_SHOW", True)
    ]
    assert tranche.status == "SIGNED"  # a receipt may still come before 15:30
    lender = await world.db.get(LenderStanding, investor.user.id)
    assert (lender.status, lender.no_show_count, lender.locked_at) == ("LOCKED", 0, ict(15, 0))

    await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    assert tranche.status == "EXPIRED"
    assert (await world.db.get(LenderStanding, investor.user.id)).no_show_count == 1
    assert (await world.db.execute(select(TopUpPledge))).first() is None

    await day14.process_day14_no_shows(world.db, now=ict(15, 40), params=world.params)
    assert (await world.db.get(LenderStanding, investor.user.id)).no_show_count == 1


async def test_on_time_money_is_left_alone_at_1500(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(10, 0))
    pay = await _pay(world, investor, tranche, now=ict(10, 1))
    fields = good_fields(world, tranche, investor, when=ict(10, 5), memo=pay.qr_code)
    await upload_receipt(world, investor, tranche, now=ict(10, 6), fields=fields)
    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 0), params=world.params)
    assert outcome.locked == [] and outcome.pledges == []
    assert tranche.status == "ATTESTED"


async def test_a_receipt_is_refused_after_1530(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(13, 0))
    with pytest.raises(DirectTransferError) as closed:
        await upload_receipt(world, investor, tranche, now=ict(15, 31))
    assert closed.value.code == "RECEIPT_DEADLINE_PASSED"


# --------------------------------------------------------------------------- #
# standing and unlocking
# --------------------------------------------------------------------------- #
async def _locked_with_pledge(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(10, 0))
    pay = await _pay(world, investor, tranche, now=ict(10, 1))
    fields = good_fields(world, tranche, investor, when=ict(10, 40), memo=pay.qr_code)
    await upload_receipt(world, investor, tranche, now=ict(10, 41), fields=fields)
    await day14.process_day14_no_shows(world.db, now=ict(15, 0), params=world.params)
    await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    return investor


async def test_a_locked_lender_sees_what_unlocking_needs_and_their_pledge(world):
    investor = await _locked_with_pledge(world)
    view = await standing.get_standing(world.db, investor.user.id)
    assert view.status == "LOCKED" and view.locked_at == ict(15, 0)
    assert view.unlock_requires == ["EKYC", "ACKNOWLEDGEMENT"]
    assert view.acknowledgement_sentence == standing.UNLOCK_SENTENCE
    assert [(p.amount_vnd, p.status) for p in view.top_up_pledges] == [(10_000_000, "AWAITING_UNLOCK")]
    assert view.top_up_pledges[0].listing_title == world.project.legal_name
    assert await standing.is_locked(world.db, investor.user.id)


async def test_unlocking_needs_a_new_ekyc_and_the_sentence(world):
    investor = await _locked_with_pledge(world)
    with pytest.raises(DirectTransferError) as no_ekyc:
        await standing.unlock(world.db, investor_id=investor.user.id, acknowledgement=standing.UNLOCK_SENTENCE, now=ict(16, 0))
    assert no_ekyc.value.code == "EKYC_REQUIRED"

    # The test clock is 2 Nov 2026; the database stamps created_at with today, which is after the lock.
    world.db.add(GVerifyVerification(user_id=investor.user.id, verification_type="KYC", status=STATUS_APPROVED))
    await world.db.flush()
    with pytest.raises(DirectTransferError) as wrong:
        await standing.unlock(world.db, investor_id=investor.user.id, acknowledgement="I promise", now=ict(16, 0))
    assert (wrong.value.status_code, wrong.value.code) == (422, "ACKNOWLEDGEMENT_MISMATCH")

    typed = "toi cam ket chuyen tien dung han vao ngay giai ngan va hieu rang neu tai pham, tai khoan cua toi se bi khoa vinh vien"
    view = await standing.unlock(world.db, investor_id=investor.user.id, acknowledgement=typed, now=ict(16, 0))
    assert view.status == "GOOD" and view.unlock_requires == [] and view.acknowledgement_sentence is None
    assert [p.status for p in view.top_up_pledges] == ["AWAITING_SIGNING"]
    assert not await standing.is_locked(world.db, investor.user.id)

    with pytest.raises(DirectTransferError) as not_locked:
        await standing.unlock(world.db, investor_id=investor.user.id, acknowledgement=typed, now=ict(16, 5))
    assert not_locked.value.code == "NOT_LOCKED"


# --------------------------------------------------------------------------- #
# review fixes
# --------------------------------------------------------------------------- #
async def test_a_receipt_time_with_seconds_gets_no_extra_minute(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    pay = await _pay(world, investor, tranche, now=at(minutes=1) + dt.timedelta(seconds=20))  # expires 10:16:20
    fields = good_fields(world, tranche, investor, when=at(minutes=16) + dt.timedelta(seconds=50), memo=pay.qr_code)
    await upload_receipt(world, investor, tranche, now=at(minutes=18), fields=fields)
    receipt = await _receipt(world, tranche)
    assert (receipt.payment_timing, receipt.late_reason) == ("LATE", "EXPIRED_CODE")


async def test_on_disbursement_day_paying_without_a_qr_is_late_money(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(10, 0))
    # Never opened the payment page: paid with the bare reference.
    await upload_receipt(world, investor, tranche, now=ict(10, 31))
    receipt = await _receipt(world, tranche)
    assert (receipt.payment_timing, receipt.late_reason) == ("LATE", "NO_QR_CODE")
    assert tranche.status == "UNDER_REVIEW" and tranche.review_cause == "LATE_MONEY"


async def test_a_receipt_uploaded_after_1500_for_money_sent_in_time_unlocks_at_1535(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(14, 0))
    pay = await _pay(world, investor, tranche, now=ict(14, 25))
    await day14.process_day14_no_shows(world.db, now=ict(15, 0), params=world.params)
    assert (await world.db.get(LenderStanding, investor.user.id)).status == "LOCKED"

    fields = good_fields(world, tranche, investor, when=ict(14, 30), memo=pay.qr_code)
    await upload_receipt(world, investor, tranche, now=ict(15, 10), fields=fields)
    assert tranche.status == "ATTESTED"
    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    assert outcome.released == [investor.user.id]
    lender = await world.db.get(LenderStanding, investor.user.id)
    assert (lender.status, lender.no_show_count) == ("GOOD", 0)


async def test_late_money_that_also_fails_a_check_is_left_to_staff(world):
    await open_window(world)
    _day14(world)
    investor, tranche = await _signed(world, now=ict(10, 0))
    pay = await _pay(world, investor, tranche, now=ict(10, 1))
    fields = good_fields(world, tranche, investor, when=ict(10, 30), memo=pay.qr_code, payer_name="SOMEONE ELSE")
    await upload_receipt(world, investor, tranche, now=ict(10, 31), fields=fields)
    assert tranche.review_cause == "RECEIPT_MISMATCH"

    outcome = await day14.process_day14_no_shows(world.db, now=ict(15, 35), params=world.params)
    assert outcome.pledges == []
    assert tranche.status == "UNDER_REVIEW"  # staff decide; the open dispute stays theirs
    assert (await world.db.get(LenderStanding, investor.user.id)).status == "LOCKED"


async def test_a_locked_lender_needs_a_fresh_ekyc_until_they_redo_it(world):
    investor = await _locked_with_pledge(world)
    assert await standing.needs_fresh_ekyc(world.db, investor.user.id)
    world.db.add(GVerifyVerification(user_id=investor.user.id, verification_type="KYC", status=STATUS_APPROVED))
    await world.db.flush()
    assert not await standing.needs_fresh_ekyc(world.db, investor.user.id)


async def test_payment_instructions_carry_the_server_clock(world):
    await open_window(world)
    investor, tranche = await _signed(world)
    pay = await _pay(world, investor, tranche, now=at(minutes=3))
    assert pay.server_time == at(minutes=3)
