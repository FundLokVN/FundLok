"""Spec: docs/specs/direct_transfer/repayment-collection.md section 8 (B9)."""
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.core.timezone import ICT
from app.direct_transfer import cic, disputes, fees, repayment
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.ledger_posting import contract_claim_summary
from app.direct_transfer.models import CicEvent, Dispute, FundingWindow, RepaymentDay, Tranche
from app.repayment_schedule.models import Facility, ScheduledPayment

from .helpers import at, close_now, fund_window, make_investor, make_world, open_window, pay_days_through, receipt_file

M = 1_000_000


def ict(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ICT)


async def day_rows(db, **where):
    stmt = select(RepaymentDay)
    for k, v in where.items():
        stmt = stmt.where(getattr(RepaymentDay, k) == v)
    return (await db.execute(stmt.order_by(RepaymentDay.due_date, RepaymentDay.day_index))).scalars().all()


async def test_daily_gross_split_sums_to_schedule_amount_every_day(loan):
    world, window, _ = loan
    sched = (await world.db.execute(select(ScheduledPayment).where(ScheduledPayment.facility_id == window.facility_id))).scalars().all()
    sums = dict(
        (await world.db.execute(
            select(RepaymentDay.day_index, func.sum(RepaymentDay.gross_vnd)).where(RepaymentDay.kind == "INSTALMENT").group_by(RepaymentDay.day_index)
        )).all()
    )
    assert len(sched) == 84
    for sp in sched:
        assert sums[sp.day_index] == int(sp.expected_amount_vnd)


async def test_principal_over_term_equals_tranche_amount_exactly(loan):
    world, _, tr = loan
    for t in tr:
        total = (await world.db.execute(select(func.sum(RepaymentDay.principal_vnd)).where(RepaymentDay.tranche_id == t.id))).scalar_one()
        assert total == t.amount_vnd


async def test_interest_sum_over_tranches_equals_facility_interest(loan):
    world, window, _ = loan
    facility = await world.db.get(Facility, window.facility_id)
    total = (await world.db.execute(select(func.sum(RepaymentDay.interest_vnd)).where(RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    assert total == int(facility.interest_vnd) == 5_333_333


async def test_withholding_total_is_five_percent_with_remainder_on_final_day(loan):
    world, _, tr = loan
    rows = await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT")
    total_interest = sum(r.interest_vnd for r in rows)
    expected_total = (2 * total_interest * 500 + 10_000) // 20_000  # round half up of 5%
    assert sum(r.tax_withheld_vnd for r in rows) == expected_total
    assert all(r.tax_withheld_vnd == r.interest_vnd * 500 // 10_000 for r in rows[:-1])  # per-day floor
    assert rows[0].tax_withheld_vnd == 317
    assert rows[-1].tax_withheld_vnd == expected_total - sum(r.tax_withheld_vnd for r in rows[:-1])
    assert all(r.net_vnd == r.gross_vnd - r.tax_withheld_vnd for r in rows)


async def test_withholding_skips_entity_investors(world):
    tr = await fund_window(world, [50 * M, 50 * M], holder_types=["ENTITY", "INDIVIDUAL"])
    await close_now(world, now=at(days=1))
    ent = await day_rows(world.db, tranche_id=tr[0].id)
    ind = await day_rows(world.db, tranche_id=tr[1].id)
    assert all(r.tax_withheld_vnd == 0 for r in ent)
    assert sum(r.tax_withheld_vnd for r in ind) > 0


async def test_stub_row_is_separate_dated_obligation_on_first_instalment_day(loan):
    world, window, tr = loan
    stubs = await day_rows(world.db, kind="STUB")
    assert len(stubs) == 10
    # receipt Mon 2 Nov, start Wed 4 Nov -> 2 business days: 10M x 16% x 2 / 252 = 12,698.4
    assert all(s.due_date == window.repayment_start_date and s.interest_vnd == 12_698 and s.principal_vnd == 0 for s in stubs)
    assert all(s.tax_withheld_vnd == 12_698 * 500 // 10_000 for s in stubs)


async def test_todays_payments_returns_net_amount_and_vietqr_per_investor(loan):
    world, window, tr = loan
    out = await repayment.todays_payments(world.db, sme_user_id=world.sme.id, on_date=window.repayment_start_date)
    rows = out["rows"]
    assert len(rows) == 20  # 10 instalments + 10 stubs due on the first day
    inst = [r for r in rows if r["kind"] == "INSTALMENT"]
    assert all(r["vietqr_payload"] and r["net_vnd"] == r["gross_vnd"] - r["tax_withheld_vnd"] for r in rows)
    from app.direct_transfer.vietqr import parse_tlv, verify_crc

    first = inst[0]
    assert verify_crc(first["vietqr_payload"]) and parse_tlv(first["vietqr_payload"])["54"] == str(first["net_vnd"])
    assert first["payee"]["account_number"] and first["investor_display"].endswith("***")
    assert {r["tranche_id"] for r in rows} == {t.id for t in tr}  # one row (and QR) per investor and obligation


async def test_todays_payments_totals_match_rows(loan):
    world, window, _ = loan
    out = await repayment.todays_payments(world.db, sme_user_id=world.sme.id, on_date=window.repayment_start_date)
    assert out["totals"]["gross_vnd"] == sum(r["gross_vnd"] for r in out["rows"])
    assert out["totals"]["net_vnd"] == out["totals"]["gross_vnd"] - out["totals"]["tax_withheld_vnd"]
    inst = sum(r["gross_vnd"] for r in out["rows"] if r["kind"] == "INSTALMENT")
    assert inst == 1_253_968


async def test_mark_paid_sets_presumed_paid_and_is_idempotent(loan):
    world, window, _ = loan
    rows = await day_rows(world.db, tranche_id=(await day_rows(world.db, kind="INSTALMENT", day_index=1))[0].tranche_id)
    first = [d.id for d in rows if d.due_date == rows[0].due_date]  # the stub and day 1, both due Wed 4 Nov
    now = ict(2026, 11, 4, 16)
    proof = (await receipt_file(world)).id
    out = await repayment.mark_days_paid(world.db, sme_user_id=world.sme.id, repayment_day_ids=first, receipt_file_id=proof, idempotency_key="m1", now=now)
    day = out[-1]
    assert day.status == "PRESUMED_PAID" and day.marked_paid_at == now
    again = await repayment.mark_days_paid(world.db, sme_user_id=world.sme.id, repayment_day_ids=first, receipt_file_id=proof, idempotency_key="m1", now=now + dt.timedelta(hours=1))
    assert again[-1].marked_paid_at == now
    with pytest.raises(DirectTransferError) as info:
        await repayment.mark_days_paid(world.db, sme_user_id=world.sme.id, repayment_day_ids=[(await day_rows(world.db, day_index=2))[0].id], receipt_file_id=proof, idempotency_key="m2", now=now)
    assert info.value.code == "NOT_DUE_YET"
    with pytest.raises(DirectTransferError) as info:  # no receipt, no proof
        await repayment.mark_days_paid(world.db, sme_user_id=world.sme.id, repayment_day_ids=[day.id], receipt_file_id=None, idempotency_key="m3", now=now)
    assert info.value.code == "RECEIPT_REQUIRED"


async def test_mark_paid_rejects_future_rows(loan):
    world, _, _ = loan
    future = (await day_rows(world.db, kind="INSTALMENT", day_index=30))[0]
    with pytest.raises(DirectTransferError) as info:
        await repayment.mark_days_paid(world.db, sme_user_id=world.sme.id, repayment_day_ids=[future.id], receipt_file_id=(await receipt_file(world)).id, idempotency_key="x", now=ict(2026, 11, 4, 9))
    assert info.value.status_code == 409


async def test_report_deadline_is_24_hours_after_the_proof(loan):
    world, _, _ = loan
    await pay_days_through(world, ict(2026, 11, 5))  # Wed 4 Nov's days paid at 10:00 that day
    day = (await day_rows(world.db, kind="INSTALMENT", day_index=1))[0]
    assert day.report_deadline == ict(2026, 11, 5, 10)


async def test_report_missing_before_deadline_marks_reported_missed_and_opens_dispute(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 5), paid_at=dt.time(17))  # marked paid Wed 17:00: 24 hours to report
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=1))[0]
    out = await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="Nothing arrived", file_id=None, now=ict(2026, 11, 5, 10), params=world.params)
    assert out.status == "REPORTED_MISSED" and out.reported_at == ict(2026, 11, 5, 10)
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    assert d.cause == "MISSED_DAY" and d.required_next_party == "SME"  # the SME proves first
    ev = (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "MISSED_DAY_CORRECTION"))).scalars().all()
    assert [e.emit_key for e in ev] == [f"missed:{day.id}:reported"]
    with pytest.raises(DirectTransferError) as info:
        await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="again", file_id=None, now=ict(2026, 11, 5, 11), params=world.params)
    assert info.value.code == "ALREADY_REPORTED"


async def test_report_by_other_investor_is_404(loan):
    world, _, tr = loan
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=1))[0]
    with pytest.raises(DirectTransferError) as info:
        await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[1].investor_id, statement="x", file_id=None, now=ict(2026, 11, 5, 10), params=world.params)
    assert info.value.status_code == 404


async def test_late_report_within_cap_goes_to_reconciliation_without_changing_status(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 9, 0, 5))
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=1))[0]
    assert day.status == "PRESUMED_PAID"
    out = await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="late", file_id=None, now=ict(2026, 11, 10, 10), params=world.params)
    assert out.status == "PRESUMED_PAID"  # history is never silently rewritten
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    assert d.cause == "LATE_REPORT" and d.required_next_party == "STAFF"


async def test_late_report_beyond_cap_refused(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 5))
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=1))[0]
    # marked paid Wed 4 Nov 10:00: report_deadline Thu 5 Nov 10:00; +30 calendar days = Sat 5 Dec 10:00 ICT
    with pytest.raises(DirectTransferError) as info:
        await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="too late", file_id=None, now=ict(2026, 12, 6), params=world.params)
    assert info.value.code == "REPORT_WINDOW_CLOSED" and info.value.persist is True


async def test_settle_days_posts_principal_ledger_legs_once(loan):
    world, window, tr = loan
    await pay_days_through(world, ict(2026, 11, 9, 0, 5))
    assert await repayment.settle_days(world.db, now=ict(2026, 11, 5)) == 0  # deadline not reached
    n = await repayment.settle_days(world.db, now=ict(2026, 11, 6))
    assert n == 20  # Wed 4 Nov rows (10 instalments + 10 stubs) have passed their deadline
    summary = await contract_claim_summary(world.db, contract_id=world.contract.id)
    day1_principal = sum(
        r.principal_vnd for r in await day_rows(world.db, kind="INSTALMENT", day_index=1)
    )
    assert summary.borrower_obligation_vnd == 100 * M - day1_principal
    assert summary.lender_total_vnd == summary.borrower_obligation_vnd
    assert await repayment.settle_days(world.db, now=ict(2026, 11, 6)) == 0
    facility = await world.db.get(Facility, window.facility_id)
    assert int(facility.collected_to_date_vnd) == 1_253_968  # instalment gross only; the stub sits outside T0


async def test_reported_missed_day_blocks_settlement_until_matched(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 5, 0, 5))
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=1))[0]
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="missing", file_id=None, now=ict(2026, 11, 5, 9), params=world.params)
    await repayment.settle_days(world.db, now=ict(2026, 11, 20))
    await world.db.refresh(day)
    assert day.settled_at is None and day.status == "REPORTED_MISSED"
    with pytest.raises(DirectTransferError) as info:
        await repayment.match_day(world.db, day_id=day.id, staff_id=world.admin.id, reason="", now=ict(2026, 11, 20))
    assert info.value.status_code == 422
    await repayment.match_day(world.db, day_id=day.id, staff_id=world.admin.id, reason="SME proved payment", now=ict(2026, 11, 20))
    assert day.status == "MATCHED" and day.settled_at == ict(2026, 11, 20)


async def test_facility_settles_when_all_rows_settled_and_window_repaid_and_sme_unlocked(loan):
    world, window, tr = loan
    far = ict(2027, 6, 1)
    await pay_days_through(world, far)
    n = await repayment.settle_days(world.db, now=far)
    assert n == 840 + 10
    facility = await world.db.get(Facility, window.facility_id)
    assert facility.status == "SETTLED" and int(facility.collected_to_date_vnd) == 105_333_333
    await world.db.refresh(window)
    assert window.status == "REPAID"
    summary = await contract_claim_summary(world.db, contract_id=world.contract.id)
    assert summary.borrower_obligation_vnd == 0 and all(v == 0 for v in summary.lender_claims_vnd.values())
    # the SME is unlocked for a new listing once the activation invoice is paid too
    from .test_funding_window import open_second, second_listing

    listing2 = await second_listing(world)
    invoice = (await world.db.execute(select(fees.FeeInvoice))).scalars().first()
    with pytest.raises(DirectTransferError) as info:
        await open_second(world, listing2)
    assert info.value.code == "SME_INVOICE_UNPAID"
    await fees.mark_invoice_paid(world.db, invoice_id=invoice.id, bank_txn_id="FTPAID1", reason="statement", staff_id=world.admin.id, now=far)
    assert (await open_second(world, listing2)).status == "OPEN"


async def test_daily_balance_counts_presumed_paid_and_excludes_reported_missed(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 6, 0, 5))
    day = (await day_rows(world.db, tranche_id=tr[0].id, kind="INSTALMENT", day_index=1))[0]
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="missing", file_id=None, now=ict(2026, 11, 5, 9, 59), params=world.params)  # inside the 24 hours from the proof
    created = await cic.emit_daily_balances(world.db, on_date=dt.date(2026, 11, 5), now=ict(2026, 11, 6, 1))
    assert created == 10
    ev0 = (await world.db.execute(select(CicEvent).where(CicEvent.emit_key == f"daily:{tr[0].id}:2026-11-05"))).scalar_one()
    ev1 = (await world.db.execute(select(CicEvent).where(CicEvent.emit_key == f"daily:{tr[1].id}:2026-11-05"))).scalar_one()
    d1 = (await day_rows(world.db, tranche_id=tr[1].id, kind="INSTALMENT", day_index=1))[0]
    d2 = (await day_rows(world.db, tranche_id=tr[1].id, kind="INSTALMENT", day_index=2))[0]
    assert ev1.payload["outstanding_principal_vnd"] == 10 * M - d1.principal_vnd - d2.principal_vnd and ev1.payload["paid_days"] == 2
    assert ev0.payload["missed_days"] == 1 and ev0.payload["paid_days"] == 1
    assert ev0.payload["outstanding_principal_vnd"] > ev1.payload["outstanding_principal_vnd"]
    assert await cic.emit_daily_balances(world.db, on_date=dt.date(2026, 11, 5), now=ict(2026, 11, 6, 2)) == 0  # idempotent


async def test_investor_cannot_see_other_tranche_schedule_404(loan):
    world, _, tr = loan
    ok = await repayment.tranche_schedule(world.db, tranche_id=tr[0].id, user_id=tr[0].investor_id)
    assert len(ok) == 85
    with pytest.raises(DirectTransferError) as info:
        await repayment.tranche_schedule(world.db, tranche_id=tr[0].id, user_id=tr[1].investor_id)
    assert info.value.status_code == 404
    assert len(await repayment.tranche_schedule(world.db, tranche_id=tr[0].id, user_id=world.sme.id)) == 85
