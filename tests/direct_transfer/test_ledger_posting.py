"""Spec: docs/specs/direct_transfer/ledger-amendment.md section 8 (E2 / D-026)."""
import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.timezone import ICT
from app.direct_transfer import repayment
from app.direct_transfer.ledger_posting import (
    LedgerPostingError,
    contract_claim_summary,
    post_repayment_day_settled,
    post_tranche_claims,
)
from app.direct_transfer.models import RepaymentDay
from app.ledger.models import LedgerAccount, LedgerEntry, LedgerTransaction
from app.ledger.service import get_account_balance

from .helpers import at, make_investor, open_window, reserve

M = 1_000_000


def ict(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=ICT)


async def test_external_account_type_is_allowed_by_migration(loan):
    world, _, _ = loan
    external = (await world.db.execute(select(LedgerAccount).where(LedgerAccount.account_type == "EXTERNAL"))).scalar_one()
    assert external.owner_user_id is None and external.contract_id is None


async def test_tranche_claims_post_lender_and_borrower_legs_against_external(loan):
    world, _, tr = loan
    summary = await contract_claim_summary(world.db, contract_id=world.contract.id)
    assert summary.borrower_obligation_vnd == 100 * M
    assert summary.lender_claims_vnd == {t.investor_id: 10 * M for t in tr}
    external = (await world.db.execute(select(LedgerAccount).where(LedgerAccount.account_type == "EXTERNAL"))).scalar_one()
    assert int(await get_account_balance(world.db, external.id)) == -200 * M  # the parties' own banks, outside FundLok
    entries = (await world.db.execute(select(LedgerEntry).where(LedgerEntry.contract_id == world.contract.id))).scalars().all()
    assert sorted(e.type for e in entries) == ["DISBURSEMENT"] * 10 + ["FUNDING"] * 10
    assert all(e.debit_account_id == external.id for e in entries)


async def test_tranche_claims_are_idempotent(loan):
    world, _, tr = loan
    before = (await world.db.execute(select(LedgerEntry))).scalars().all()
    txs = await post_tranche_claims(world.db, tranche=tr[0], contract_id=world.contract.id, sme_user_id=world.sme.id)
    after = (await world.db.execute(select(LedgerEntry))).scalars().all()
    assert len(before) == len(after) and len(txs) == 2
    assert {t.idempotency_key for t in txs} == {f"dt:claim:{tr[0].id}:lender", f"dt:claim:{tr[0].id}:borrower"}


async def test_no_new_path_leg_touches_omnibus_cash(loan):
    world, _, _ = loan
    await repayment.presume_paid_days(world.db, now=ict(2026, 11, 9))
    await repayment.settle_days(world.db, now=ict(2026, 11, 9))
    entries = (await world.db.execute(select(LedgerEntry))).scalars().all()
    assert entries
    ids = {e.debit_account_id for e in entries} | {e.credit_account_id for e in entries}
    types = {a.account_type for a in (await world.db.execute(select(LedgerAccount).where(LedgerAccount.id.in_(ids)))).scalars()}
    assert "OMNIBUS_CASH" not in types and types <= {"EXTERNAL", "LENDER", "BORROWER", "FL_REVENUE"}
    assert (await world.db.execute(select(LedgerAccount).where(LedgerAccount.account_type == "OMNIBUS_CASH"))).scalars().all() == []


async def test_repayment_day_posts_principal_legs_only(loan):
    world, _, _ = loan
    await repayment.presume_paid_days(world.db, now=ict(2026, 11, 9))
    await repayment.settle_days(world.db, now=ict(2026, 11, 9))
    repay = (await world.db.execute(select(LedgerEntry).where(LedgerEntry.type.in_(("REPAYMENT", "DISTRIBUTION"))))).scalars().all()
    day1 = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.day_index == 1, RepaymentDay.kind == "INSTALMENT"))).scalars().all()
    assert len(repay) == 2 * len(day1)  # one borrower leg + one lender leg per day row; stub rows post nothing
    assert sum(int(e.amount) for e in repay if e.type == "REPAYMENT") == sum(d.principal_vnd for d in day1)


async def test_zero_principal_day_posts_nothing(loan):
    world, _, tr = loan
    stub = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.kind == "STUB"))).scalars().first()
    tranche = tr[0]
    assert stub.principal_vnd == 0
    assert await post_repayment_day_settled(world.db, repayment_day=stub, tranche=tranche, contract_id=world.contract.id, sme_user_id=world.sme.id) == []


async def test_lender_claims_sum_equals_borrower_obligation_throughout(loan):
    world, _, _ = loan
    for day in (ict(2026, 11, 9), ict(2026, 11, 20), ict(2026, 12, 15)):
        await repayment.presume_paid_days(world.db, now=day)
        await repayment.settle_days(world.db, now=day)
        s = await contract_claim_summary(world.db, contract_id=world.contract.id)
        assert s.lender_total_vnd == s.borrower_obligation_vnd and 0 < s.borrower_obligation_vnd < 100 * M


async def test_full_repayment_returns_borrower_and_lender_balances_to_zero(loan):
    world, _, _ = loan
    far = ict(2027, 6, 1)
    await repayment.presume_paid_days(world.db, now=far)
    await repayment.settle_days(world.db, now=far)
    s = await contract_claim_summary(world.db, contract_id=world.contract.id)
    assert s.borrower_obligation_vnd == 0 and set(s.lender_claims_vnd.values()) == {0}


async def test_posting_requires_active_tranche(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    with pytest.raises(LedgerPostingError):
        await post_tranche_claims(world.db, tranche=t, contract_id=world.contract.id, sme_user_id=world.sme.id)


async def test_ledger_still_append_only(loan):
    world, _, _ = loan
    for sql in ("UPDATE ledger_entries SET amount = amount + 1", "DELETE FROM ledger_entries", "UPDATE ledger_transactions SET reference = 'x'", "DELETE FROM ledger_transactions"):
        with pytest.raises(DBAPIError) as info:
            async with world.db.begin_nested():  # savepoint: keep the loan rows for the next statement
                await world.db.execute(text(sql))
        assert "append-only" in str(info.value), sql


async def test_fee_paid_posts_fee_leg_to_fl_revenue_net_of_vat(loan):
    from app.direct_transfer import fees
    from app.direct_transfer.models import FeeInvoice

    world, _, _ = loan
    invoice = (await world.db.execute(select(FeeInvoice))).scalar_one()
    await fees.mark_invoice_paid(world.db, invoice_id=invoice.id, bank_txn_id="FT-LEDGER-FEE", reason="statement", staff_id=world.admin.id, now=ict(2026, 11, 10))
    legs = (await world.db.execute(select(LedgerEntry).where(LedgerEntry.type == "FEE"))).scalars().all()
    assert len(legs) == 1 and int(legs[0].amount) == 3_000_000 and invoice.total_vnd == 3_300_000
    external = (await world.db.execute(select(LedgerAccount).where(LedgerAccount.account_type == "EXTERNAL"))).scalar_one()
    assert legs[0].debit_account_id == external.id
