"""Spec: docs/specs/direct_transfer/fees.md section 8 (B12). Fees are LIVE by default (E1)."""
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.core.timezone import ICT
from app.direct_transfer import close, fees, repayment
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.fees import ManualInvoiceProvider
from app.direct_transfer.models import FeeInvoice, InvestorFeeAccrual, RepaymentDay, Tranche
from app.ledger.models import LedgerAccount, LedgerEntry

from .helpers import T0, at, close_now, fund_window, make_investor, make_world, open_window, reserve

M = 1_000_000


def ict(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=ICT)


async def sme_invoice(db):
    return (await db.execute(select(FeeInvoice).where(FeeInvoice.kind == "SME_ACTIVATION"))).scalar_one()


async def test_sme_activation_fee_is_3_percent_of_confirmed_total_golden_3_000_000(loan):
    world, window, _ = loan
    inv = await sme_invoice(world.db)
    assert (inv.base_vnd, inv.fee_vnd) == (100 * M, 3_000_000)


async def test_sme_invoice_vat_and_total_and_due_in_7_days(loan):
    world, window, _ = loan
    inv = await sme_invoice(world.db)
    assert inv.vat_vnd == 300_000 and inv.total_vnd == 3_300_000  # PROVISIONAL(Q-D10): VAT 10%
    assert inv.due_at - inv.issued_at == dt.timedelta(days=7) and inv.status == "ISSUED"
    assert inv.payer_user_id == world.sme.id and inv.window_id == window.id
    import re

    assert re.fullmatch(r"FL[A-Z0-9]{8}", inv.reference_code) and inv.invoice_number == f"FL-2026-{inv.reference_code}"


async def test_sme_invoice_created_at_close_and_never_when_failed(world):
    await fund_window(world, [10 * M] * 4)
    await close_now(world, now=at(days=14))
    assert world.window.status == "FAILED"
    assert (await world.db.execute(select(func.count()).select_from(FeeInvoice))).scalar_one() == 0


async def test_fee_is_never_netted_from_principal_or_repayment_rows(loan):
    world, window, _ = loan
    from app.repayment_schedule.models import Facility

    facility = await world.db.get(Facility, window.facility_id)
    assert int(facility.principal_vnd) == 100 * M  # the SME gets the full principal; fee is a separate invoice
    inst = (await world.db.execute(select(func.sum(RepaymentDay.gross_vnd)).where(RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    assert inst == int(facility.total_obligation_vnd) == 105_333_333  # T0 untouched by any fee


async def test_investor_fee_total_golden_83_333_per_10m_ticket(loan):
    world, _, tr = loan
    for t in tr:
        await world.db.refresh(t)
        assert t.investor_fee_total_vnd == 83_333


def test_investor_fee_for_whole_loan_golden_833_333():
    assert fees.investor_fee_total(100 * M, 4) == 833_333


async def settle_through(world, now):
    await repayment.presume_paid_days(world.db, now=now)
    await repayment.settle_days(world.db, now=now)


async def test_monthly_accrual_pro_rata_to_received_and_sums_to_total(loan):
    world, _, tr = loan
    now = ict(2026, 12, 3)
    await settle_through(world, now)
    created = await fees.accrue_investor_fees(world.db, now=now)
    assert created == 10  # November for every tranche
    nov = (await world.db.execute(select(InvestorFeeAccrual).where(InvestorFeeAccrual.tranche_id == tr[0].id))).scalar_one()
    rows = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id))).scalars().all()
    scheduled = sum(r.gross_vnd for r in rows)
    received = sum(r.gross_vnd for r in rows if r.due_date.month == 11)
    assert nov.received_gross_vnd == received and nov.fee_vnd == 83_333 * received // scheduled
    assert 0 < nov.fee_vnd < 83_333
    assert await fees.accrue_investor_fees(world.db, now=now) == 0  # idempotent
    # collect everything: the accruals then sum exactly to the fixed fee (remainder lands on the last month)
    far = ict(2027, 6, 1)
    await settle_through(world, far)
    await fees.accrue_investor_fees(world.db, now=far)
    for t in tr:
        total = (await world.db.execute(select(func.sum(InvestorFeeAccrual.fee_vnd)).where(InvestorFeeAccrual.tranche_id == t.id))).scalar_one()
        assert total == 83_333


async def test_accrual_job_idempotent(loan):
    world, _, _ = loan
    now = ict(2026, 12, 3)
    await settle_through(world, now)
    assert await fees.accrue_investor_fees(world.db, now=now) == 10
    for _ in range(3):
        assert await fees.accrue_investor_fees(world.db, now=now) == 0
    assert (await world.db.execute(select(func.count()).select_from(InvestorFeeAccrual))).scalar_one() == 10


async def test_monthly_invoice_groups_accruals_per_investor(loan):
    world, _, tr = loan
    now = ict(2026, 12, 3)
    await settle_through(world, now)
    await fees.accrue_investor_fees(world.db, now=now)
    invoices = await fees.issue_investor_invoices(world.db, now=now)
    assert len(invoices) == 10 and {i.payer_user_id for i in invoices} == {t.investor_id for t in tr}
    inv = invoices[0]
    accrual = (await world.db.execute(select(InvestorFeeAccrual).where(InvestorFeeAccrual.invoice_id == inv.id))).scalar_one()
    assert inv.kind == "INVESTOR_SERVICE" and inv.fee_vnd == accrual.fee_vnd and inv.base_vnd == accrual.received_gross_vnd
    assert inv.vat_vnd == (2 * inv.fee_vnd * 1000 + 10_000) // 20_000 and inv.period_month == dt.date(2026, 11, 1)
    assert inv.due_at - inv.issued_at == dt.timedelta(days=7)
    assert await fees.issue_investor_invoices(world.db, now=now) == []  # accruals already invoiced


async def test_unpaid_sme_invoice_blocks_new_window(loan):
    world, _, _ = loan
    inv = await sme_invoice(world.db)
    late = inv.due_at + dt.timedelta(seconds=1)
    assert await fees.sme_has_unpaid_invoice(world.db, world.sme.id, now=late) is True
    assert await fees.investor_has_unpaid_invoice(world.db, world.sme.id, now=late) is False


async def test_sme_fee_blocks_next_listing_only_after_seven_days(loan):
    """D-047 / T5: the SME fee blocks the next listing only once 7 days from issue have passed (was: from issue)."""
    world, _, _ = loan
    inv = await sme_invoice(world.db)
    assert inv.due_at - inv.issued_at == dt.timedelta(days=7)
    for now in (inv.issued_at, inv.issued_at + dt.timedelta(days=6, hours=23), inv.due_at):
        assert await fees.sme_has_unpaid_invoice(world.db, world.sme.id, now=now) is False
    after = inv.due_at + dt.timedelta(seconds=1)
    assert await fees.sme_has_unpaid_invoice(world.db, world.sme.id, now=after) is True
    # No late interest on the SME fee: the rate stays 0 and an overdue invoice never grows.
    assert DirectTransferParams().late_fee_bps_per_day == 0
    await fees.mark_overdue_invoices(world.db, now=inv.due_at + dt.timedelta(days=30))
    assert inv.status == "OVERDUE" and inv.late_fee_vnd == 0 and inv.total_vnd == 3_300_000
    assert await fees.sme_has_unpaid_invoice(world.db, world.sme.id, now=after + dt.timedelta(days=30)) is True


async def test_lender_fee_unpaid_after_seven_days_blocks_new_commitments_only(world):
    """D-047 / T5: inside the 7 days a lender can still invest; after them only NEW commitments are refused."""
    window = await open_window(world)
    inv = await make_investor(world)
    invoice = FeeInvoice(
        kind="INVESTOR_SERVICE", window_id=window.id, contract_id=world.contract.id, payer_user_id=inv.user.id, base_vnd=1_000_000,
        fee_vnd=1_000, vat_vnd=100, total_vnd=1_100, status="ISSUED", invoice_number="INV-Y", provider="MANUAL", reference_code="FLDDDDDDDD",
        issued_at=T0, due_at=T0 + dt.timedelta(days=7),
    )
    world.db.add(invoice)
    await world.db.flush()
    inside = await reserve(world, inv, 10 * M, now=T0 + dt.timedelta(days=6))
    assert inside.status == "RESERVED"  # no block inside the 7 days
    other = await make_investor(world)
    other_invoice = FeeInvoice(
        kind="INVESTOR_SERVICE", window_id=window.id, contract_id=world.contract.id, payer_user_id=other.user.id, base_vnd=1_000_000,
        fee_vnd=1_000, vat_vnd=100, total_vnd=1_100, status="OVERDUE", invoice_number="INV-X", provider="MANUAL", reference_code="FLEEEEEEEE",
        issued_at=T0, due_at=T0 + dt.timedelta(days=7),
    )
    world.db.add(other_invoice)
    await world.db.flush()
    after = T0 + dt.timedelta(days=7, seconds=1)
    with pytest.raises(DirectTransferError) as info:
        await reserve(world, other, 10 * M, now=after)
    assert info.value.code == "INVESTOR_FEE_UNPAID"
    # Nothing else is locked: the lender's existing ticket is untouched and the SME side is not blocked by it.
    await world.db.refresh(inside)
    assert inside.status == "RESERVED"
    assert await fees.sme_has_unpaid_invoice(world.db, other.user.id, now=after) is False


async def test_unpaid_investor_invoice_blocks_reservation_and_paying_lifts_it(world):
    window = await open_window(world)
    inv = await make_investor(world)
    invoice = FeeInvoice(
        kind="INVESTOR_SERVICE", window_id=window.id, contract_id=world.contract.id, payer_user_id=inv.user.id, base_vnd=1_000_000,
        fee_vnd=1_000, vat_vnd=100, total_vnd=1_100, status="ISSUED", invoice_number="INV-Z", provider="MANUAL", reference_code="FLCCCCCCCC",
        issued_at=T0, due_at=T0 + dt.timedelta(days=7),
    )
    world.db.add(invoice)
    await world.db.flush()
    with pytest.raises(DirectTransferError) as info:
        await reserve(world, inv, 10 * M, now=T0 + dt.timedelta(days=7, hours=1))
    assert info.value.code == "INVESTOR_FEE_UNPAID"
    await fees.mark_invoice_paid(world.db, invoice_id=invoice.id, bank_txn_id="FT-INV-1", reason="bank statement line", staff_id=world.admin.id, now=at(hours=1))
    assert (await reserve(world, inv, 10 * M, now=at(hours=1))).status == "RESERVED"


async def test_paying_invoice_lifts_blocks(loan):
    world, _, _ = loan
    inv = await sme_invoice(world.db)
    await fees.mark_invoice_paid(world.db, invoice_id=inv.id, bank_txn_id="FT-SME-1", reason="statement", staff_id=world.admin.id, now=ict(2026, 11, 10))
    assert await fees.sme_has_unpaid_invoice(world.db, world.sme.id, now=ict(2027, 1, 1)) is False


async def test_overdue_job_marks_overdue_and_assesses_late_fee(loan):
    world, _, _ = loan
    params = DirectTransferParams(late_fee_bps_per_day=100)  # 1% of the fee per day: PROVISIONAL(Q-D11) default is 0
    inv = await sme_invoice(world.db)
    assert await fees.mark_overdue_invoices(world.db, now=inv.due_at - dt.timedelta(seconds=1), params=params) == 0
    assert await fees.mark_overdue_invoices(world.db, now=inv.due_at + dt.timedelta(days=3), params=params) == 1
    assert inv.status == "OVERDUE" and inv.late_fee_vnd == 3_000_000 * 100 * 3 // 10_000 == 90_000
    assert inv.total_vnd == 3_300_000 + 90_000
    assert await fees.sme_has_unpaid_invoice(world.db, world.sme.id, now=inv.due_at + dt.timedelta(days=3)) is True  # overdue still blocks
    assert await fees.mark_overdue_invoices(world.db, now=inv.due_at + dt.timedelta(days=3), params=params) == 0  # idempotent
    # default late fee is 0 but the block still works
    inv.status, inv.late_fee_vnd, inv.total_vnd = "ISSUED", 0, 3_300_000
    await world.db.flush()
    await fees.mark_overdue_invoices(world.db, now=inv.due_at + dt.timedelta(days=3))
    assert inv.status == "OVERDUE" and inv.late_fee_vnd == 0


async def test_mark_paid_posts_fee_ledger_leg_once(loan):
    world, _, _ = loan
    inv = await sme_invoice(world.db)
    paid = await fees.mark_invoice_paid(world.db, invoice_id=inv.id, bank_txn_id="FT-SME-2", reason="statement", staff_id=world.admin.id, now=ict(2026, 11, 10))
    again = await fees.mark_invoice_paid(world.db, invoice_id=inv.id, bank_txn_id="FT-SME-2", reason="replay", staff_id=world.admin.id, now=ict(2026, 11, 11))
    assert paid.id == again.id and paid.status == "PAID" and paid.paid_at == ict(2026, 11, 10)
    legs = (await world.db.execute(select(LedgerEntry).where(LedgerEntry.type == "FEE"))).scalars().all()
    assert len(legs) == 1 and int(legs[0].amount) == 3_000_000  # fee revenue excludes the VAT owed to the tax authority
    revenue = await world.db.get(LedgerAccount, legs[0].credit_account_id)
    assert revenue.account_type == "FL_REVENUE"
    with pytest.raises(DirectTransferError) as info:
        await fees.mark_invoice_paid(world.db, invoice_id=inv.id, bank_txn_id="x", reason="", staff_id=world.admin.id, now=ict(2026, 11, 11))
    assert info.value.status_code == 422


async def test_mark_paid_rejects_reused_bank_txn(world):
    window = await open_window(world)
    invs = []
    for i in range(2):
        inv = FeeInvoice(
            kind="INVESTOR_SERVICE", window_id=window.id, contract_id=world.contract.id, payer_user_id=world.sme.id, base_vnd=1, fee_vnd=1000,
            vat_vnd=100, total_vnd=1100, status="ISSUED", invoice_number=f"INV-{i}", provider="MANUAL", reference_code=f"FLDDDDDDD{i}",
            issued_at=T0, due_at=T0 + dt.timedelta(days=7),
        )
        world.db.add(inv)
        invs.append(inv)
    await world.db.flush()
    await fees.mark_invoice_paid(world.db, invoice_id=invs[0].id, bank_txn_id="FT-DUP", reason="r", staff_id=world.admin.id, now=T0)
    with pytest.raises(DirectTransferError) as info:
        await fees.mark_invoice_paid(world.db, invoice_id=invs[1].id, bank_txn_id="FT-DUP", reason="r", staff_id=world.admin.id, now=T0)
    assert info.value.code == "DUPLICATE_BANK_TXN"


async def test_fees_disabled_flag_removes_invoices_and_blocks(db_session):
    world = await make_world(db_session, params=DirectTransferParams(fees_enabled=False))
    tr = await fund_window(world, [10 * M] * 10)
    await close_now(world, now=at(days=1))
    assert world.window.status == "REPAYING"
    assert (await world.db.execute(select(func.count()).select_from(FeeInvoice))).scalar_one() == 0
    assert all(t.investor_fee_total_vnd == 0 for t in tr)
    world.db.add(
        FeeInvoice(
            kind="SME_ACTIVATION", window_id=world.window.id, contract_id=world.contract.id, payer_user_id=world.sme.id, base_vnd=1, fee_vnd=1,
            vat_vnd=0, total_vnd=1, status="ISSUED", invoice_number="INV-OFF", provider="MANUAL", reference_code="FLEEEEEEEE", issued_at=T0, due_at=T0,
        )
    )
    await world.db.flush()
    assert await fees.sme_has_unpaid_invoice(world.db, world.sme.id, world.params, now=ict(2027, 1, 1)) is False
    assert await fees.accrue_investor_fees(world.db, now=ict(2027, 6, 1), params=world.params) == 0


async def test_commission_eligible_only_when_tranche_settled(loan):
    world, _, tr = loan
    assert (await fees.commission_eligible(world.db, tranche_id=tr[0].id)).eligible is False
    far = ict(2027, 6, 1)
    await settle_through(world, far)
    result = await fees.commission_eligible(world.db, tranche_id=tr[0].id)
    assert result.eligible is True and result.settled_at == far


async def test_invoice_provider_interface_manual_provider_assigns_number():
    draft = fees.FeeInvoiceDraft("SME_ACTIVATION", __import__("uuid").uuid4(), 1, 1, 0, "FLABCDEFGH", ict(2026, 11, 3))
    result = await ManualInvoiceProvider().issue(invoice=draft)
    assert result.provider == "MANUAL" and result.invoice_number == "FL-2026-FLABCDEFGH"


async def test_invoice_vietqr_only_when_fee_account_configured(loan):
    world, _, _ = loan
    inv = await sme_invoice(world.db)
    assert fees.invoice_vietqr(inv, DirectTransferParams(fee_account_bin=None, fee_account_number=None)) is None
    qr = fees.invoice_vietqr(inv, DirectTransferParams(fee_account_bin="970436", fee_account_number="0011001234567", fee_account_name="FUNDLOK"))
    from app.direct_transfer.vietqr import parse_tlv

    assert parse_tlv(qr)["54"] == "3300000" and parse_tlv(parse_tlv(qr)["62"])["08"] == inv.reference_code


async def test_sme_fee_invoice_carries_a_qr(loan):
    """D-047 / T5: the SME fee invoice data carries a payment QR built from the FUNDLOK_FEE_* settings."""
    from app.direct_transfer.vietqr import parse_tlv, verify_crc

    world, _, _ = loan
    inv = await sme_invoice(world.db)
    params = DirectTransferParams(fee_account_bin="970436", fee_account_number="0011001234567", fee_account_name="FUNDLOK")
    out = fees.invoice_out(inv, params)
    assert out.kind == "SME_ACTIVATION" and out.reference_code == inv.reference_code
    assert verify_crc(out.vietqr_payload)
    tlv = parse_tlv(out.vietqr_payload)
    assert tlv["54"] == str(inv.total_vnd) == "3300000" and parse_tlv(tlv["62"])["08"] == inv.reference_code
    assert "0011001234567" in out.vietqr_payload
    # Unconfigured account: no QR, never a made-up one.
    assert fees.invoice_out(inv, DirectTransferParams(fee_account_bin=None, fee_account_number=None)).vietqr_payload is None
