"""Phase 2 slice S2: commit and decide (commit-then-accept flow).

Spec: docs/specs/direct_transfer/commit-then-accept-flow.md sections 3 to 8 and ADR-011, as amended by Money flow
FINAL v1.0 (pledges sign nothing and can be withdrawn until the end of Day 12; Day 13 and Day 14 never move).
Day 0 below is Mon 2 Nov 2026 (T0). Day 13 is Sun 15 Nov, Day 14 is Mon 16 Nov; weekends do not move them.
"""
import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select

from app.core.timezone import ICT
from app.direct_transfer import close, commit_flow, disputes, reservation, tranches, windows
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import (
    Commitment,
    Dispute,
    DisputeEvidence,
    EvidenceFile,
    EvidenceLog,
    FeeInvoice,
    LenderStanding,
    Tranche,
    Transfer,
)
from app.ledger.models import LedgerEntry
from app.lending.models import Listing
from app.notifications.models import Notification

from .helpers import T0, attested_tranche, good_fields, make_investor, make_world, open_window

M = 1_000_000
TERMS = "terms-1"
COMMIT = "COMMIT_THEN_ACCEPT"

COMMIT_CLOSES = dt.datetime(2026, 11, 15, 0, 0, tzinfo=ICT)  # 00:00 of Day 13
SME_DEADLINE = dt.datetime(2026, 11, 15, 15, 0, tzinfo=ICT)  # 15:00 of Day 13
PAY_DEADLINE = dt.datetime(2026, 11, 16, 14, 45, tzinfo=ICT)  # 14:45 of Day 14: signing and new QRs close
NO_SHOW_AT = dt.datetime(2026, 11, 16, 15, 0, tzinfo=ICT)  # 15:00 of Day 14
RECEIPT_DEADLINE = dt.datetime(2026, 11, 16, 15, 30, tzinfo=ICT)  # 15:30 of Day 14


@pytest.fixture
async def world(db_session):
    return await make_world(db_session)


async def _open(world, *, now=T0, flow=COMMIT):
    window = await windows.open_funding_window(
        world.db, listing_id=world.listing.id, sme_user_id=world.sme.id, payment_account_id=world.sme_account_id,
        declared_outside_debt_vnd=0, declaration_accepted=True, idempotency_key=f"open-{uuid.uuid4().hex}", now=now,
        signer=world.signer, storage=world.storage, params=world.params, flow=flow,
    )
    world.window = window
    return window


async def _commit(world, inv, amount, *, now=T0 + dt.timedelta(hours=1), key=None, window_id=None):
    return await commit_flow.commit_to_window(
        world.db, window_id=window_id or world.window.id, investor_id=inv.user.id, amount_vnd=amount,
        ack_risk_disclosure=True, terms_version=TERMS, current_terms_version=TERMS,
        payment_account_id=inv.account_id, idempotency_key=key or f"c-{uuid.uuid4().hex}", now=now,
        signer=world.signer, storage=world.storage, params=world.params,
    )


async def _refused(coro, code):
    with pytest.raises(DirectTransferError) as info:
        await coro
    assert info.value.code == code, (info.value.code, info.value.detail)
    return info.value


async def _fill(world, amounts, *, now=T0 + dt.timedelta(hours=1)):
    investors = []
    for amount in amounts:
        inv = await make_investor(world)
        await _commit(world, inv, amount, now=now)
        investors.append(inv)
    return investors


async def _reload(world, window=None):
    window = window or world.window
    await world.db.refresh(window)
    return window


async def _count(db, model, *where):
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _awaiting_sme(world, amounts):
    """A window with `amounts` pledged whose commit phase has ended at commit_closes_at."""
    await _open(world)
    investors = await _fill(world, amounts)
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    assert (await _reload(world)).status == "AWAITING_SME"
    return investors


# --------------------------------------------------------------------------- #
# deadlines
# --------------------------------------------------------------------------- #
async def test_new_windows_get_fixed_deadlines_that_an_early_fill_does_not_move(world):
    window = await _open(world)
    assert window.flow == COMMIT and window.status == "OPEN"
    assert window.commit_closes_at == COMMIT_CLOSES
    assert window.sme_deadline_at == SME_DEADLINE
    assert window.pay_deadline_at == PAY_DEADLINE
    assert window.no_show_at == NO_SHOW_AT and window.receipt_deadline_at == RECEIPT_DEADLINE
    assert window.window_end == window.commit_closes_at

    # Early fill (Money flow FINAL v1.0: "Day 13 and Day 14 do not move"): pledges close, nothing else changes.
    filled_at = dt.datetime(2026, 11, 3, 20, 30, tzinfo=ICT)
    await _fill(world, [40 * M, 30 * M, 20 * M, 10 * M], now=filled_at)
    await _reload(world)
    assert world.window.status == "OPEN"
    assert world.window.commit_closes_at == COMMIT_CLOSES and world.window.window_end == COMMIT_CLOSES
    assert world.window.sme_deadline_at == SME_DEADLINE and world.window.pay_deadline_at == PAY_DEADLINE
    await _refused(_commit(world, await make_investor(world), 10 * M, now=filled_at), "WINDOW_FULL")


async def test_default_window_flow_param_picks_the_flow(db_session, monkeypatch):
    monkeypatch.delenv("FUNDING_WINDOW_FLOW", raising=False)
    assert DirectTransferParams().default_window_flow == "LEGACY"
    monkeypatch.setenv("FUNDING_WINDOW_FLOW", "commit_then_accept")
    assert DirectTransferParams().default_window_flow == COMMIT  # the deployment switches it on
    monkeypatch.delenv("FUNDING_WINDOW_FLOW")
    w = await make_world(db_session, params=DirectTransferParams(repay_from_approval=False, default_window_flow=COMMIT))
    window = await open_window(w)
    assert window.flow == COMMIT and window.sme_deadline_at == SME_DEADLINE


# --------------------------------------------------------------------------- #
# commit
# --------------------------------------------------------------------------- #
async def test_commitment_moves_no_money_and_creates_no_tranche(world):
    await _open(world)
    inv = await make_investor(world)
    c = await _commit(world, inv, 20 * M)
    assert c.status == "PLEDGED" and c.amount_vnd == 20 * M and c.tranche_id is None
    assert c.investor_account_id == inv.account_id and c.investor_type == "INDIVIDUAL"
    assert c.pledge_file_key and len(c.pledge_sha256) == 64 and c.pledge_certificate["role"] == "INVESTOR"
    assert c.pledge_file_key in world.storage.objects
    assert await _count(world.db, Tranche) == 0
    assert await _count(world.db, Transfer) == 0
    assert await _count(world.db, LedgerEntry) == 0
    assert await _count(world.db, FeeInvoice) == 0
    notes = (await world.db.execute(select(Notification).where(Notification.user_id == inv.user.id))).scalars().all()
    assert [n.event for n in notes] == ["COMMITMENT_RECEIVED"]


async def test_a_lender_can_commit_several_times_to_one_window(world):
    await _open(world)
    inv = await make_investor(world)
    for amount in (10 * M, 10 * M, 15 * M):
        await _commit(world, inv, amount)
    assert await _count(world.db, Commitment, Commitment.investor_id == inv.user.id) == 3
    views = await commit_flow.list_investor_commitments(world.db, window_id=world.window.id, investor_id=inv.user.id)
    assert len(views) == 3 and views[0].my_committed_total_vnd == 35 * M
    assert views[0].window_committed_total_vnd == 35 * M and views[0].window_target_vnd == 100 * M


async def test_commitment_below_min_ticket_refused_unless_exact_remainder(world):
    await _open(world)
    inv = await make_investor(world)
    await _refused(_commit(world, inv, 5 * M), "BELOW_MIN_TICKET")
    await _fill(world, [19 * M] * 5)  # 95M pledged, 5M left
    await _refused(_commit(world, inv, 4 * M), "BELOW_MIN_TICKET")
    last = await _commit(world, inv, 5 * M)  # exactly the remainder is allowed below the minimum
    assert last.amount_vnd == 5 * M


async def test_commitments_above_capacity_refused(world):
    await _open(world)
    await _fill(world, [30 * M, 30 * M, 30 * M])
    inv = await make_investor(world)
    await _refused(_commit(world, inv, 20 * M), "CAPACITY_EXCEEDED")
    await _commit(world, inv, 10 * M)  # exactly what is left still fits


async def test_a_full_round_takes_no_more_pledges_until_a_withdrawal_reopens_it(world):
    await _open(world)
    await _fill(world, [50 * M, 40 * M])
    inv = await make_investor(world)
    last = await _commit(world, inv, 10 * M)
    assert (await _reload(world)).status == "OPEN"  # full, but the phase runs to the end of Day 12
    late = await make_investor(world)
    await _refused(_commit(world, late, 10 * M), "WINDOW_FULL")
    await commit_flow.withdraw_commitment(world.db, commitment_id=last.id, investor_id=inv.user.id, now=T0 + dt.timedelta(days=3))
    reopened = await _commit(world, late, 10 * M, now=T0 + dt.timedelta(days=3, hours=1))
    assert reopened.status == "PLEDGED"
    assert await commit_flow.pledged_total(world.db, world.window.id) == 100 * M


async def test_lender_can_withdraw_a_pledge_until_the_end_of_day_12(world):
    await _open(world)
    inv = await make_investor(world)
    keep = await _commit(world, inv, 20 * M)
    drop = await _commit(world, inv, 10 * M)
    out = await commit_flow.withdraw_commitment(
        world.db, commitment_id=drop.id, investor_id=inv.user.id, now=COMMIT_CLOSES - dt.timedelta(seconds=1)
    )
    assert out.status == "VOID" and out.void_reason == "WITHDRAWN"
    again = await commit_flow.withdraw_commitment(world.db, commitment_id=drop.id, investor_id=inv.user.id, now=COMMIT_CLOSES)
    assert again.status == "VOID"  # a replay returns it unchanged
    view = await commit_flow.commitment_view(world.db, keep)
    assert view.my_committed_total_vnd == 20 * M and view.window_committed_total_vnd == 20 * M
    events = (
        await world.db.execute(
            select(EvidenceLog.event_type).where(EvidenceLog.entity_id == drop.id, EvidenceLog.event_type == "COMMITMENT_WITHDRAWN")
        )
    ).scalars().all()
    assert events == ["COMMITMENT_WITHDRAWN"]


async def test_withdraw_is_refused_after_the_pledge_list_is_final_and_for_someone_elses_pledge(world):
    await _open(world)
    inv, other = await make_investor(world), await make_investor(world)
    pledge = await _commit(world, inv, 30 * M)
    err = await _refused(
        commit_flow.withdraw_commitment(world.db, commitment_id=pledge.id, investor_id=other.user.id, now=T0 + dt.timedelta(days=1)),
        "NOT_FOUND",
    )
    assert err.status_code == 404
    await _refused(
        commit_flow.withdraw_commitment(world.db, commitment_id=pledge.id, investor_id=inv.user.id, now=COMMIT_CLOSES),
        "WITHDRAW_CLOSED",
    )
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    await _refused(
        commit_flow.withdraw_commitment(world.db, commitment_id=pledge.id, investor_id=inv.user.id, now=COMMIT_CLOSES),
        "WITHDRAW_CLOSED",
    )
    assert (await world.db.get(Commitment, pledge.id)).status == "PLEDGED"


async def test_window_moves_to_awaiting_sme_at_commit_closes_at(world):
    await _open(world)
    await _fill(world, [30 * M])
    await commit_flow.advance_window(
        world.db, window_id=world.window.id, now=COMMIT_CLOSES - dt.timedelta(seconds=1), params=world.params
    )
    assert (await _reload(world)).status == "OPEN"
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    window = await _reload(world)
    assert window.status == "AWAITING_SME"
    assert window.sme_deadline_at == SME_DEADLINE and window.pay_deadline_at == PAY_DEADLINE  # fixed, not moved
    await _refused(_commit(world, await make_investor(world), 10 * M, now=COMMIT_CLOSES), "WINDOW_NOT_OPEN")


async def test_locked_lender_cannot_commit(world):
    await _open(world)
    locked, closed, fine = await make_investor(world), await make_investor(world), await make_investor(world)
    world.db.add(LenderStanding(investor_id=locked.user.id, status="LOCKED", no_show_count=1))
    world.db.add(LenderStanding(investor_id=closed.user.id, status="CLOSED", no_show_count=2))
    await world.db.flush()
    await _refused(_commit(world, locked, 10 * M), "LENDER_LOCKED")
    await _refused(_commit(world, closed, 10 * M), "LENDER_CLOSED")
    await _commit(world, fine, 10 * M)  # no standing row means GOOD


async def test_lender_with_fee_invoice_unpaid_after_seven_days_cannot_commit(world):
    window = await _open(world)
    inv = await make_investor(world)
    world.db.add(
        FeeInvoice(
            kind="INVESTOR_SERVICE", window_id=window.id, contract_id=world.contract.id, payer_user_id=inv.user.id,
            base_vnd=1_000_000, fee_vnd=1_000, vat_vnd=100, total_vnd=1_100, status="ISSUED", invoice_number="INV-C",
            provider="MANUAL", reference_code="FLCCCCCCCC", issued_at=T0, due_at=T0 + dt.timedelta(days=7),
        )
    )
    await world.db.flush()
    inside = await _commit(world, inv, 10 * M, now=T0 + dt.timedelta(days=6))
    assert inside.status == "PLEDGED"  # inside the 7 days nothing is blocked
    await _refused(_commit(world, inv, 10 * M, now=T0 + dt.timedelta(days=7, seconds=1)), "INVESTOR_FEE_UNPAID")


async def test_commitment_needs_no_service_agreement_but_keeps_the_other_gates(world):
    await _open(world)
    inv = await make_investor(world)
    # Money flow FINAL v1.0: pressing Confirm signs nothing; the Lender Service Agreement comes with the Day-14 contract.
    assert (await _commit(world, inv, 10 * M)).status == "PLEDGED"
    await _refused(
        commit_flow.commit_to_window(
            world.db, window_id=world.window.id, investor_id=inv.user.id, amount_vnd=10 * M, ack_risk_disclosure=False,
            terms_version=TERMS, current_terms_version=TERMS, payment_account_id=inv.account_id,
            idempotency_key="k-risk", now=T0, signer=world.signer, storage=world.storage, params=world.params,
        ),
        "RISK_ACK_REQUIRED",
    )
    await _refused(
        commit_flow.commit_to_window(
            world.db, window_id=world.window.id, investor_id=inv.user.id, amount_vnd=10 * M, ack_risk_disclosure=True,
            terms_version="old", current_terms_version=TERMS, payment_account_id=inv.account_id,
            idempotency_key="k-terms", now=T0, signer=world.signer, storage=world.storage, params=world.params,
        ),
        "TERMS_OUTDATED",
    )
    # The SME cannot commit to its own window (404, as for reservations).
    err = await _refused(
        commit_flow.commit_to_window(
            world.db, window_id=world.window.id, investor_id=world.sme.id, amount_vnd=10 * M, ack_risk_disclosure=True,
            terms_version=TERMS, current_terms_version=TERMS, payment_account_id=inv.account_id,
            idempotency_key="k-sme", now=T0, signer=world.signer, storage=world.storage, params=world.params,
        ),
        "NOT_FOUND",
    )
    assert err.status_code == 404


async def test_commitment_idempotency_key_replays_first_result(world):
    await _open(world)
    inv = await make_investor(world)
    first = await _commit(world, inv, 20 * M, key="same-key")
    again = await _commit(world, inv, 20 * M, key="same-key")
    assert again.id == first.id
    assert await _count(world.db, Commitment) == 1
    await _refused(_commit(world, inv, 30 * M, key="same-key"), "IDEMPOTENCY_CONFLICT")
    await _refused(
        commit_flow.commit_to_window(
            world.db, window_id=world.window.id, investor_id=inv.user.id, amount_vnd=10 * M, ack_risk_disclosure=True,
            terms_version=TERMS, current_terms_version=TERMS, payment_account_id=inv.account_id,
            idempotency_key="", now=T0, signer=world.signer, storage=world.storage, params=world.params,
        ),
        "IDEMPOTENCY_KEY_REQUIRED",
    )


# --------------------------------------------------------------------------- #
# SME decision
# --------------------------------------------------------------------------- #
async def _decide(world, decision, *, now, user=None, key=None):
    return await commit_flow.decide_window(
        world.db, window_id=world.window.id, sme_user_id=(user or world.sme).id, decision=decision,
        idempotency_key=key or f"d-{uuid.uuid4().hex}", now=now, params=world.params,
    )


async def test_sme_accept_below_fifty_percent_is_allowed_before_the_deadline(world):
    await _awaiting_sme(world, [40 * M])
    window = await _decide(world, "ACCEPT", now=SME_DEADLINE - dt.timedelta(minutes=1))
    assert window.status == "AWAITING_PAYMENT" and window.sme_decision == "ACCEPTED"
    assert window.sme_decided_by == world.sme.id and window.sme_decided_at is not None
    assert await _count(world.db, Tranche) == 1


async def test_decision_is_only_open_in_awaiting_sme_and_only_for_the_owner(world):
    await _open(world)
    await _fill(world, [60 * M])
    await _refused(_decide(world, "ACCEPT", now=T0 + dt.timedelta(hours=2)), "DECISION_NOT_OPEN")
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    other = await make_investor(world)
    err = await _refused(_decide(world, "ACCEPT", now=COMMIT_CLOSES, user=other.user), "FORBIDDEN")
    assert err.status_code == 403
    await _refused(_decide(world, "MAYBE", now=COMMIT_CLOSES), "INVALID_DECISION")
    await _decide(world, "ACCEPT", now=COMMIT_CLOSES, key="dk")
    assert (await _decide(world, "ACCEPT", now=COMMIT_CLOSES, key="dk")).status == "AWAITING_PAYMENT"  # replay
    await _refused(_decide(world, "DECLINE", now=COMMIT_CLOSES, key="dk"), "IDEMPOTENCY_CONFLICT")
    await _refused(_decide(world, "DECLINE", now=COMMIT_CLOSES), "DECISION_NOT_OPEN")


async def test_silence_at_fifty_percent_or_more_auto_accepts(world):
    await _awaiting_sme(world, [30 * M, 20 * M])  # exactly 50%
    window = await commit_flow.advance_window(world.db, window_id=world.window.id, now=SME_DEADLINE, params=world.params)
    assert window.status == "AWAITING_PAYMENT" and window.sme_decision == "AUTO_ACCEPTED"
    assert window.sme_decided_by is None and window.sme_decided_at == SME_DEADLINE
    assert await _count(world.db, Tranche) == 2


async def test_silence_below_fifty_percent_auto_declines_with_no_loan_no_money_no_fee(world):
    await _awaiting_sme(world, [30 * M, 19 * M])  # 49%
    window = await commit_flow.advance_window(world.db, window_id=world.window.id, now=SME_DEADLINE, params=world.params)
    assert window.status == "FAILED" and window.sme_decision == "AUTO_DECLINED" and window.close_reason == "AUTO_DECLINED"
    rows = (await world.db.execute(select(Commitment))).scalars().all()
    assert {(c.status, c.void_reason) for c in rows} == {("VOID", "AUTO_DECLINED")}
    assert await _count(world.db, Tranche) == 0 and await _count(world.db, Transfer) == 0
    assert await _count(world.db, FeeInvoice) == 0 and await _count(world.db, LedgerEntry) == 0
    assert window.facility_id is None
    assert (await world.db.get(Listing, window.listing_id)).status == "CANCELLED"
    declined = (await world.db.execute(select(Notification).where(Notification.event == "WINDOW_DECLINED"))).scalars().all()
    assert len(declined) == 2


async def test_explicit_sme_decision_before_deadline_overrides_the_automatic_result(world):
    # ACCEPT at 40% beats the automatic decline.
    await _awaiting_sme(world, [40 * M])
    await _decide(world, "ACCEPT", now=SME_DEADLINE - dt.timedelta(hours=3))
    window = await commit_flow.advance_window(
        world.db, window_id=world.window.id, now=SME_DEADLINE + dt.timedelta(hours=1), params=world.params
    )
    assert window.status == "AWAITING_PAYMENT" and window.sme_decision == "ACCEPTED"

    # DECLINE at 90% beats the automatic accept.
    other = await make_world(world.db)
    await _open(other)
    await _fill(other, [50 * M, 40 * M])
    await commit_flow.advance_window(other.db, window_id=other.window.id, now=COMMIT_CLOSES, params=other.params)
    declined = await _decide(other, "DECLINE", now=SME_DEADLINE - dt.timedelta(hours=1))
    assert declined.status == "FAILED" and declined.sme_decision == "DECLINED" and declined.close_reason == "DECLINED"
    again = await commit_flow.advance_window(other.db, window_id=other.window.id, now=SME_DEADLINE + dt.timedelta(days=1), params=other.params)
    assert again.status == "FAILED" and again.sme_decision == "DECLINED"
    assert await _count(other.db, Tranche, Tranche.window_id == other.window.id) == 0
    assert {c.void_reason for c in (await other.db.execute(select(Commitment).where(Commitment.window_id == other.window.id))).scalars()} == {"DECLINED"}


async def test_sme_decision_after_deadline_is_refused(world):
    await _awaiting_sme(world, [70 * M])
    await _refused(_decide(world, "DECLINE", now=SME_DEADLINE), "DECISION_DEADLINE_PASSED")
    await _refused(_decide(world, "ACCEPT", now=SME_DEADLINE + dt.timedelta(hours=2)), "DECISION_DEADLINE_PASSED")
    window = await commit_flow.advance_window(world.db, window_id=world.window.id, now=SME_DEADLINE, params=world.params)
    assert window.sme_decision == "AUTO_ACCEPTED"  # the automatic result stands


# --------------------------------------------------------------------------- #
# acceptance
# --------------------------------------------------------------------------- #
async def test_acceptance_creates_one_tranche_per_lender_with_summed_amount(world):
    await _open(world)
    a, b = await make_investor(world), await make_investor(world, holder_type="ENTITY")
    await _commit(world, a, 10 * M)
    await _commit(world, b, 20 * M)
    await _commit(world, a, 15 * M)
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    await _decide(world, "ACCEPT", now=COMMIT_CLOSES + dt.timedelta(hours=1))
    rows = {t.investor_id: t for t in (await world.db.execute(select(Tranche))).scalars()}
    assert set(rows) == {a.user.id, b.user.id}
    assert rows[a.user.id].amount_vnd == 25 * M and rows[b.user.id].amount_vnd == 20 * M
    assert rows[b.user.id].investor_type == "ENTITY" and rows[a.user.id].investor_account_id == a.account_id
    assert all(t.status == "RESERVED" and t.stub_interest_vnd == 0 and t.reference_code for t in rows.values())
    assert len({t.reference_code for t in rows.values()}) == 2


async def test_acceptance_folds_commitments_and_sets_hold_to_pay_deadline(world):
    await _open(world)
    a = await make_investor(world)
    await _commit(world, a, 10 * M)
    await _commit(world, a, 30 * M)
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    await _decide(world, "ACCEPT", now=COMMIT_CLOSES)
    tranche = (await world.db.execute(select(Tranche))).scalar_one()
    assert tranche.hold_expires_at == PAY_DEADLINE == world.window.pay_deadline_at
    commitments = (await world.db.execute(select(Commitment))).scalars().all()
    assert len(commitments) == 2 and {(c.status, c.tranche_id) for c in commitments} == {("FOLDED", tranche.id)}
    assert (await _reload(world)).status == "AWAITING_PAYMENT"


async def test_acceptance_notifies_every_lender(world):
    await _open(world)
    investors = await _fill(world, [30 * M, 20 * M, 10 * M])
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    await _decide(world, "ACCEPT", now=COMMIT_CLOSES)
    for inv in investors:
        events = [
            n.event for n in (await world.db.execute(select(Notification).where(Notification.user_id == inv.user.id))).scalars()
        ]
        assert sorted(events) == ["COMMITMENT_RECEIVED", "CONTRACT_SIGNING_OPEN", "WINDOW_ACCEPTED"]


# --------------------------------------------------------------------------- #
# the SME's receipt check and the 30-day dispute (slice S5: the same for both flows)
# --------------------------------------------------------------------------- #
async def _attested(world, amount=20 * M):
    if world.window is None:
        await open_window(world)
    inv = await make_investor(world)
    tranche = await attested_tranche(world, inv, amount)
    transfer = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id))).scalar_one()
    return inv, tranche, transfer


async def _sme_receipt(world, tranche, fields, *, now):
    return await tranches.sme_receipt_check(
        world.db, tranche_id=tranche.id, sme_user_id=world.sme.id, content=f"%PDF-1.4 sme {tranche.id}".encode(),
        content_type="application/pdf", filename="sme.pdf", fields=fields, storage=world.storage, now=now,
        params=world.params,
    )


async def _dispute(world, tranche, *, now):
    return await tranches.sme_dispute_after_confirmation(
        world.db, tranche_id=tranche.id, sme_user_id=world.sme.id, statement="Not on our Techcombank statement",
        content=b"%PDF-1.4 bank statement", content_type="application/pdf", filename="statement.pdf",
        storage=world.storage, now=now, params=world.params,
    )


async def test_sme_receipt_match_confirms_the_tranche(world):
    inv, tranche, transfer = await _attested(world)
    now = tranche.attested_at + dt.timedelta(hours=2)
    await _sme_receipt(world, tranche, good_fields(world, tranche, inv, when=now, bank_txn_id=transfer.bank_txn_id), now=now)
    assert tranche.status == "CONFIRMED" and tranche.dispute_until is None  # the SME checked it itself

    # A screenshot with another bank transaction goes to staff as a mismatch.
    inv2, other, _ = await _attested(world, 10 * M)
    await _sme_receipt(world, other, good_fields(world, other, inv2, when=now), now=now)
    assert other.status == "UNDER_REVIEW" and other.review_cause == "RECEIPT_MISMATCH"
    dispute = (await world.db.execute(select(Dispute).where(Dispute.tranche_id == other.id))).scalar_one()
    assert dispute.cause == "RECEIPT_MISMATCH" and dispute.status == "OPEN"

    # Nothing read off the screenshot (OCR off): the upload confirms on the SME's word.
    _, third, _ = await _attested(world, 10 * M)
    await _sme_receipt(world, third, None, now=now)
    assert third.status == "CONFIRMED"


async def test_silence_for_24_hours_confirms_the_tranche(world):
    _, tranche, _ = await _attested(world)
    deadline = tranche.confirm_deadline
    assert deadline == tranche.attested_at + dt.timedelta(hours=24)
    assert await tranches.confirm_lapsed_tranches(world.db, now=deadline - dt.timedelta(seconds=1), params=world.params) == 0
    await _refused(_sme_receipt(world, tranche, None, now=deadline), "DISPUTE_WINDOW_CLOSED")  # the 24 hours are over
    assert await tranches.confirm_lapsed_tranches(world.db, now=deadline, params=world.params) == 1
    await world.db.refresh(tranche)
    assert tranche.status == "CONFIRMED" and tranche.confirmed_at == deadline
    assert tranche.dispute_until == deadline + dt.timedelta(days=30)


async def test_dispute_after_silence_is_open_for_30_days_with_bank_statement(world):
    _, tranche, _ = await _attested(world)
    lapsed = tranche.confirm_deadline
    await tranches.confirm_lapsed_tranches(world.db, now=lapsed, params=world.params)
    await world.db.refresh(tranche)

    await _refused(_dispute(world, tranche, now=lapsed + dt.timedelta(days=31)), "DISPUTE_WINDOW_CLOSED")
    dispute = await _dispute(world, tranche, now=lapsed + dt.timedelta(days=29))
    assert dispute.cause == "SME_DISPUTE" and dispute.status == "OPEN" and dispute.required_next_party == "INVESTOR"
    claim = (await world.db.execute(select(DisputeEvidence).where(DisputeEvidence.dispute_id == dispute.id))).scalar_one()
    assert claim.party == "SME" and claim.kind == "CLAIM" and claim.statement == "Not on our Techcombank statement"
    assert (await world.db.get(EvidenceFile, claim.file_id)).purpose == "BANK_STATEMENT"
    assert tranche.status == "CONFIRMED"  # the ticket keeps its state while staff decide

    # Staff decide: received after all, so nothing changes.
    await disputes.decide_dispute(
        world.db, dispute_id=dispute.id, staff_id=world.admin.id, outcome="RECEIVED", reason="Bank trace shows it",
        now=lapsed + dt.timedelta(days=30), params=world.params,
    )
    assert tranche.status == "CONFIRMED" and dispute.status == "DECIDED"


# --------------------------------------------------------------------------- #
# advance_window
# --------------------------------------------------------------------------- #
async def test_advance_window_is_idempotent_and_time_driven(world):
    await _open(world)
    await _fill(world, [30 * M, 30 * M])

    async def totals():
        counts = []
        for model in (Tranche, Notification, EvidenceLog):
            counts.append((await world.db.execute(select(func.count()).select_from(model))).scalar_one())
        return tuple(counts)

    # Nothing is due yet: no change however often it runs.
    before = await totals()
    for _ in range(2):
        await commit_flow.advance_window(world.db, window_id=world.window.id, now=T0 + dt.timedelta(days=5), params=world.params)
    assert (await _reload(world)).status == "OPEN" and await totals() == before

    # The end of the commit phase, twice: the second run finds nothing due.
    await close.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    assert (await _reload(world)).status == "AWAITING_SME"
    mid = await totals()
    await close.advance_window(world.db, window_id=world.window.id, now=COMMIT_CLOSES, params=world.params)
    assert await totals() == mid

    # Before the SME deadline nothing happens; at it, the auto decision once.
    await commit_flow.advance_window(world.db, window_id=world.window.id, now=SME_DEADLINE - dt.timedelta(seconds=1), params=world.params)
    assert (await _reload(world)).status == "AWAITING_SME"
    await commit_flow.advance_all(world.db, now=SME_DEADLINE, params=world.params)
    after = await totals()
    assert (await _reload(world)).status == "AWAITING_PAYMENT" and after[0] == 2  # one per lender
    await commit_flow.advance_all(world.db, now=SME_DEADLINE + dt.timedelta(days=3), params=world.params)
    await close.advance_all_windows(world.db, now=SME_DEADLINE + dt.timedelta(days=3), params=world.params)
    assert await totals() == after
    assert (await _reload(world)).status == "AWAITING_PAYMENT"  # nothing after AWAITING_PAYMENT is this slice


# --------------------------------------------------------------------------- #
# legacy and reservations
# --------------------------------------------------------------------------- #
async def test_legacy_windows_keep_the_old_behaviour(world):
    window = await open_window(world)  # default flow
    assert window.flow == "LEGACY"
    assert window.commit_closes_at is None and window.sme_deadline_at is None and window.pay_deadline_at is None
    assert window.window_end == T0 + dt.timedelta(days=14)
    inv = await make_investor(world)
    tranche = await reservation.reserve_ticket(
        world.db, window_id=window.id, investor_id=inv.user.id, amount_vnd=10 * M, ack_risk_disclosure=True,
        payment_account_id=inv.account_id, idempotency_key="legacy-1", now=T0, params=world.params,
    )
    assert tranche.hold_expires_at == T0 + dt.timedelta(hours=2)
    await _refused(_commit(world, inv, 10 * M), "WINDOW_NOT_OPEN")
    # The commit-flow advance does not touch a LEGACY window; the old one still closes it on day 14.
    await commit_flow.advance_window(world.db, window_id=window.id, now=window.window_end, params=world.params)
    assert (await _reload(world, window)).status == "OPEN"
    closed = await close.advance_window(world.db, window_id=window.id, now=window.window_end, params=world.params)
    assert closed.status in ("CLOSING", "FAILED") and closed.close_reason == "DAY_14"


async def test_reservations_route_refused_for_commit_then_accept_windows(world):
    await _open(world)
    inv = await make_investor(world)
    err = await _refused(
        reservation.reserve_ticket(
            world.db, window_id=world.window.id, investor_id=inv.user.id, amount_vnd=10 * M, ack_risk_disclosure=True,
            payment_account_id=inv.account_id, idempotency_key="res-1", now=T0, params=world.params,
        ),
        "USE_COMMITMENTS",
    )
    assert err.status_code == 409
    assert await _count(world.db, Tranche) == 0
