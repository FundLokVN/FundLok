"""Spec: HANDOFF-04 B14 (staff service layer for the exceptions and dispute queues, fee blocks, account approvals)."""
import pytest
from sqlalchemy import select

from app.direct_transfer import admin, fees, tranches
from app.direct_transfer.models import FeeInvoice

from .helpers import at, attested_tranche, make_account, make_investor, make_user, open_window

M = 1_000_000


async def test_dispute_queue_shows_evidence_whose_turn_and_timeline(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M)
    await tranches.sme_dispute_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, statement="not received", file_id=None, now=at(minutes=30), params=world.params)
    queue = await admin.dispute_queue(world.db)
    assert len(queue) == 1
    item = queue[0]
    assert (item["subject_type"], item["cause"], item["required_next_party"], item["status"]) == ("TRANCHE", "SME_DISPUTE", "INVESTOR", "OPEN")
    assert [(e["party"], e["kind"], e["statement"]) for e in item["evidence"]] == [("SME", "CLAIM", "not received")]
    assert [e["event_type"] for e in item["timeline"]][0] == "DISPUTE_OPENED"
    await admin.decide_dispute(world.db, dispute_id=item["id"], staff_id=world.admin.id, outcome="RECEIVED", reason="credited", now=at(hours=2))
    assert await admin.dispute_queue(world.db) == []
    assert len(await admin.dispute_queue(world.db, only_open=False)) == 1


async def test_fee_blocks_view_lists_unpaid_smes_and_investors(loan):
    world, _, tr = loan
    blocks = await admin.blocked_parties(world.db)
    assert [b["user_id"] for b in blocks["smes"]] == [str(world.sme.id)] and blocks["investors"] == []
    invoice = (await world.db.execute(select(FeeInvoice))).scalar_one()
    await admin.mark_invoice_paid(world.db, invoice_id=invoice.id, bank_txn_id="FT-B14", reason="statement", staff_id=world.admin.id, now=at(days=10))
    assert (await admin.blocked_parties(world.db)) == {"smes": [], "investors": []}


async def test_pending_payment_accounts_listed_until_decided(world):
    # An SME account with no approved KYB name to match waits for staff (accounts.py); investors never do.
    user = await make_user(world.db, role="SME", full_name="Le Van C")
    account = await make_account(world.db, user, role="SME", holder_type="ENTITY")
    assert [a.id for a in await admin.pending_payment_accounts(world.db)] == [account.id]
    await admin.decide_payment_account(world.db, account_id=account.id, decision="REJECTED", reason="name does not match the statement", staff_id=world.admin.id, now=at())
    assert await admin.pending_payment_accounts(world.db) == []
    assert account.status == "REJECTED" and account.rejection_reason == "name does not match the statement"
