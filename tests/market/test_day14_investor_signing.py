"""Day 14 of a commit-then-accept round: the Investor signs their loan contract with a Zalo code, then pays.

Spec: docs/specs/direct_transfer/day14-investor-signing.md (Money flow FINAL v1.0, Day 14; ops rule "Day-14 signing
close and 15-minute QR"). Day 0 is Mon 2 Nov 2026; Day 13 Sun 15 Nov; Day 14 Mon 16 Nov.
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.timezone import ICT
from app.direct_transfer import commit_flow, day14, standing, tranches
from app.direct_transfer.models import Commitment, LenderStanding, Tranche, WindowLoanContract
from app.market import day14_signing
from app.market.investor_agreement.models import InvestorServiceAgreement
from app.phone_verification import zalo
from app.users.models import User
from tests.direct_transfer.helpers import make_world
from tests.market.test_day13_loan_contracts import _day13, _refused, _sign_all

M = 1_000_000
DAY14_MORNING = dt.datetime(2026, 11, 16, 9, 0, tzinfo=ICT)
PAY_DEADLINE = dt.datetime(2026, 11, 16, 14, 45, tzinfo=ICT)
RECEIPT_DEADLINE = dt.datetime(2026, 11, 16, 15, 30, tzinfo=ICT)


@pytest.fixture
async def world(db_session):
    return await make_world(db_session)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    async def _record(to, code, *, tracking_id):
        sent.append({"to": to, "code": code, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_otp", _record)
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "zalo")
    return sent


async def _accepted(world, amounts_by_lender):
    """A round the SME accepted on Day 13 by signing every contract: one RESERVED ticket per lender."""
    window, lenders, _ = await _day13(world, amounts_by_lender)
    await _sign_all(world, window)
    await commit_flow.decide_window(
        world.db, window_id=window.id, sme_user_id=world.sme.id, decision="ACCEPT", idempotency_key="acc",
        now=dt.datetime(2026, 11, 15, 10, 0, tzinfo=ICT),
        contracts_signed=True,
    )
    for i, inv in enumerate(lenders):
        user = await world.db.get(User, inv.user.id)
        user.phone = f"+8490300{i:04d}"
    await world.db.flush()
    tickets = {
        t.investor_id: t for t in (await world.db.execute(select(Tranche).where(Tranche.window_id == window.id))).scalars()
    }
    return window, lenders, tickets


async def _sign(world, inv, ticket, outbox, *, now=DAY14_MORNING):
    await day14_signing.send_code(world.db, investor_id=inv.user.id, tranche=ticket, now=now)
    return await day14_signing.sign_contract(
        world.db, investor_id=inv.user.id, tranche=ticket, code=outbox[-1]["code"], click_to_sign=True,
        storage=world.storage, now=now,
    )


async def test_payment_instructions_refused_until_contract_signed(world, outbox):
    _, (a,), tickets = await _accepted(world, [[60 * M]])
    await _refused(
        tranches.payment_instructions(world.db, tranche_id=tickets[a.user.id].id, investor_id=a.user.id, now=DAY14_MORNING),
        "CONTRACT_REQUIRED",
    )
    await _sign(world, a, tickets[a.user.id], outbox)
    pay = await tranches.payment_instructions(
        world.db, tranche_id=tickets[a.user.id].id, investor_id=a.user.id, now=DAY14_MORNING
    )
    assert pay.amount_vnd == 60 * M and pay.qr_code and pay.pay_deadline_at == PAY_DEADLINE


async def test_contract_signing_needs_otp_and_click_and_logs_both(world, outbox):
    _, (a,), tickets = await _accepted(world, [[40 * M, 20 * M]])
    ticket = tickets[a.user.id]
    sent = await day14_signing.send_code(world.db, investor_id=a.user.id, tranche=ticket, now=DAY14_MORNING)
    assert sent.purpose == "LOAN_CONTRACT" and sent.tranche_id == ticket.id and outbox[0]["to"] == "84903000000"
    await _refused(
        day14_signing.sign_contract(
            world.db, investor_id=a.user.id, tranche=ticket, code=outbox[0]["code"], click_to_sign=False,
            storage=world.storage, now=DAY14_MORNING,
        ),
        "CLICK_REQUIRED",
    )
    wrong = "000000" if outbox[0]["code"] != "000000" else "111111"
    await _refused(
        day14_signing.sign_contract(
            world.db, investor_id=a.user.id, tranche=ticket, code=wrong, click_to_sign=True, storage=world.storage,
            now=DAY14_MORNING,
        ),
        "CODE_INCORRECT",
    )
    signed = await day14_signing.sign_contract(
        world.db, investor_id=a.user.id, tranche=ticket, code=outbox[0]["code"], click_to_sign=True,
        storage=world.storage, now=DAY14_MORNING, ip_address="1.2.3.4", user_agent="pytest",
    )
    assert signed.status == "SIGNED" and signed.acceptance_sha256
    contract = (await world.db.execute(select(WindowLoanContract))).scalar_one()
    assert contract.status == "INVESTOR_SIGNED" and contract.amount_vnd == 60 * M
    sig = contract.investor_signature
    assert sig["signed_by"] == "ZALO_OTP" and sig["click_to_sign"] is True and sig["code_id"] == str(sent.id)
    assert sig["ip_address"] == "1.2.3.4" and sig["contract_no"] == contract.contract_no
    # The first Day 14: the same code confirmed the Investor Service Agreement too.
    agreement = (await world.db.execute(select(InvestorServiceAgreement))).scalar_one()
    assert agreement.investor_id == a.user.id and agreement.tranche_id == ticket.id
    assert sig["investor_agreement_id"] == str(agreement.id)
    # Signed once: a second attempt is refused.
    await _refused(
        day14_signing.send_code(world.db, investor_id=a.user.id, tranche=signed, now=DAY14_MORNING),
        "TRANCHE_NOT_SIGNABLE",
    )


async def test_contract_signing_after_pay_deadline_is_refused(world, outbox):
    _, (a,), tickets = await _accepted(world, [[60 * M]])
    await _refused(
        day14_signing.send_code(world.db, investor_id=a.user.id, tranche=tickets[a.user.id], now=PAY_DEADLINE),
        "CONTRACT_DEADLINE_PASSED",
    )


async def test_no_show_voids_its_pledges_and_contract_and_locks_the_lender(world, outbox):
    window, (a, b), tickets = await _accepted(world, [[30 * M], [30 * M]])
    await _sign(world, b, tickets[b.user.id], outbox)
    await day14.process_day14_window(world.db, window_id=window.id, now=RECEIPT_DEADLINE)
    a_ticket = await world.db.get(Tranche, tickets[a.user.id].id, populate_existing=True)
    assert a_ticket.status == "EXPIRED"
    pledges = (await world.db.execute(select(Commitment).where(Commitment.investor_id == a.user.id))).scalars().all()
    assert pledges and all(c.status == "VOID" and c.void_reason == "NO_SHOW" for c in pledges)
    contract = (
        await world.db.execute(select(WindowLoanContract).where(WindowLoanContract.investor_id == a.user.id))
    ).scalar_one()
    assert contract.status == "VOID" and contract.void_reason == "NO_SHOW"
    held = await world.db.get(LenderStanding, a.user.id, populate_existing=True)
    assert held.status == "LOCKED" and held.no_show_count == 1


async def test_second_no_show_closes_the_lender_permanently(world):
    window, (a,), _ = await _accepted(world, [[60 * M]])
    world.db.add(LenderStanding(investor_id=a.user.id, status="GOOD", no_show_count=1))
    await world.db.flush()
    await day14.process_day14_window(world.db, window_id=window.id, now=RECEIPT_DEADLINE)
    closed = await world.db.get(LenderStanding, a.user.id, populate_existing=True)
    assert closed.status == "CLOSED" and closed.no_show_count == 2 and closed.closed_at is not None
    await _refused(
        standing.unlock(world.db, investor_id=a.user.id, acknowledgement=standing.UNLOCK_SENTENCE, now=RECEIPT_DEADLINE),
        "LENDER_CLOSED",
    )


async def test_staff_waiver_lifts_lock_without_counting_the_miss(world):
    window, (a,), _ = await _accepted(world, [[60 * M]])
    await day14.process_day14_window(world.db, window_id=window.id, now=RECEIPT_DEADLINE)
    await _refused(
        standing.waive_lock(
            world.db, investor_id=a.user.id, window_id=window.id, reason="BANK_OUTAGE", note=" ",
            staff_id=world.admin.id, now=RECEIPT_DEADLINE,
        ),
        "NOTE_REQUIRED",
    )
    view = await standing.waive_lock(
        world.db, investor_id=a.user.id, window_id=window.id, reason="BANK_OUTAGE",
        note="Bank statement shows the transfer at 14:20 failed (Vietcombank outage)", staff_id=world.admin.id,
        now=RECEIPT_DEADLINE,
    )
    assert view.status == "GOOD" and view.no_show_count == 0
    await _refused(
        standing.waive_lock(
            world.db, investor_id=a.user.id, window_id=window.id, reason="BANK_OUTAGE", note="again",
            staff_id=world.admin.id, now=RECEIPT_DEADLINE,
        ),
        "NOT_LOCKED",
    )
