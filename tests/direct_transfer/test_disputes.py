"""Spec: docs/specs/direct_transfer/disputes.md section 8 (B10)."""
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.core.timezone import ICT
from app.direct_transfer import close, disputes, repayment, reservation, tranches
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import CicEvent, Dispute, DisputeEvidence, EvidenceLog, RepaymentDay, Tranche, Transfer

from .helpers import T0, accept, at, attested_tranche, fund_window, make_investor, open_window, reserve, upload_receipt

M = 1_000_000


def ict(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=ICT)


async def sme_disputed(world, amount=10 * M, *, now=T0):
    if world.window is None:
        await open_window(world)
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, amount, now=now)
    await tranches.sme_dispute_tranche(
        world.db, tranche_id=t.id, sme_user_id=world.sme.id, statement="Nothing arrived in our account", file_id=None,
        now=now + dt.timedelta(minutes=30), params=world.params,
    )
    d = (await world.db.execute(select(Dispute).where(Dispute.tranche_id == t.id))).scalar_one()
    return inv, t, d


async def test_sme_dispute_creates_dispute_with_investor_first(world):
    inv, t, d = await sme_disputed(world)
    assert (d.cause, d.status, d.required_next_party) == ("SME_DISPUTE", "OPEN", "INVESTOR")
    assert d.response_deadline == at(days=2, minutes=30)  # 2 calendar days after the dispute at Mon 2 Nov 10:30
    claim = (await world.db.execute(select(DisputeEvidence).where(DisputeEvidence.dispute_id == d.id))).scalar_one()
    assert (claim.party, claim.kind, claim.statement) == ("SME", "CLAIM", "Nothing arrived in our account")


async def test_missed_day_dispute_has_sme_first(loan):
    world, _, tr = loan
    day = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id, RepaymentDay.day_index == 1, RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="not received", file_id=None, now=ict(2026, 11, 5, 10), params=world.params)
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    assert d.cause == "MISSED_DAY" and d.required_next_party == "SME" and d.response_deadline == ict(2026, 11, 7, 10)  # 2 calendar days after the report, Thu 5 Nov 10:00
    claim = (await world.db.execute(select(DisputeEvidence).where(DisputeEvidence.dispute_id == d.id))).scalar_one()
    assert (claim.party, claim.kind) == ("INVESTOR", "CLAIM")


async def test_wrong_party_cannot_submit_evidence(world):
    inv, t, d = await sme_disputed(world)
    with pytest.raises(DirectTransferError) as e1:
        await disputes.submit_dispute_evidence(world.db, dispute_id=d.id, user_id=world.sme.id, statement="my statement", file_id=None, now=at(hours=2))
    assert e1.value.code == "NOT_YOUR_TURN" and e1.value.status_code == 409
    stranger = await make_investor(world)
    with pytest.raises(DirectTransferError) as e2:
        await disputes.submit_dispute_evidence(world.db, dispute_id=d.id, user_id=stranger.user.id, statement="x", file_id=None, now=at(hours=2))
    assert e2.value.status_code == 404
    with pytest.raises(DirectTransferError) as e3:
        await disputes.submit_dispute_evidence(world.db, dispute_id=d.id, user_id=inv.user.id, statement=None, file_id=None, now=at(hours=2))
    assert e3.value.status_code == 400


async def test_two_step_proof_then_staff_turn(world):
    inv, t, d = await sme_disputed(world)
    await disputes.submit_dispute_evidence(world.db, dispute_id=d.id, user_id=inv.user.id, statement="my bank statement shows the transfer", file_id=None, now=at(hours=2))
    assert d.required_next_party == "SME" and d.response_deadline is not None
    await disputes.submit_dispute_evidence(world.db, dispute_id=d.id, user_id=world.sme.id, statement="our statement shows nothing", file_id=None, now=at(hours=3))
    assert d.required_next_party == "STAFF" and d.response_deadline is None
    kinds = [(e.party, e.kind) for e in (await world.db.execute(select(DisputeEvidence).where(DisputeEvidence.dispute_id == d.id).order_by(DisputeEvidence.submitted_at))).scalars()]
    assert kinds == [("SME", "CLAIM"), ("INVESTOR", "PROOF"), ("SME", "COUNTER_PROOF")]
    with pytest.raises(DirectTransferError) as info:
        await disputes.submit_dispute_evidence(world.db, dispute_id=d.id, user_id=inv.user.id, statement="more", file_id=None, now=at(hours=4))
    assert info.value.code == "NOT_YOUR_TURN"


async def test_deadline_lapse_moves_turn_to_staff_without_deciding(world):
    inv, t, d = await sme_disputed(world)
    deadline = d.response_deadline
    assert await disputes.advance_dispute_deadlines(world.db, now=deadline - dt.timedelta(seconds=1)) == 0
    assert await disputes.advance_dispute_deadlines(world.db, now=deadline) == 1
    assert d.required_next_party == "STAFF" and d.status == "OPEN" and d.outcome is None
    assert await disputes.advance_dispute_deadlines(world.db, now=deadline) == 0
    ev = (await world.db.execute(select(EvidenceLog).where(EvidenceLog.entity_id == d.id, EvidenceLog.event_type == "DISPUTE_EVIDENCE"))).scalars().all()
    assert any(e.payload.get("kind") == "NO_RESPONSE" for e in ev)


async def test_decision_requires_reason_and_records_decider(world):
    inv, t, d = await sme_disputed(world)
    for reason in ("", "  "):
        with pytest.raises(DirectTransferError) as info:
            await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="RECEIVED", reason=reason, now=at(hours=5))
        assert info.value.status_code == 422
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="RECEIVED", reason="Investor statement shows credit to the SME", now=at(hours=5))
    assert (d.status, d.outcome, d.decided_by, d.decided_at, d.decision_reason) == ("DECIDED", "RECEIVED", world.admin.id, at(hours=5), "Investor statement shows credit to the SME")
    with pytest.raises(DirectTransferError) as info:
        await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_RECEIVED", reason="changed my mind", now=at(hours=6))
    assert info.value.code == "DISPUTE_ALREADY_DECIDED"


async def test_decision_mirrors_to_append_only_evidence_log(world):
    inv, t, d = await sme_disputed(world)
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_RECEIVED", reason="Bank trace confirmed non-receipt", now=at(hours=5))
    row = (await world.db.execute(select(EvidenceLog).where(EvidenceLog.entity_id == d.id, EvidenceLog.event_type == "DISPUTE_DECISION"))).scalar_one()
    assert row.actor_id == world.admin.id and row.reason == "Bank trace confirmed non-receipt" and row.payload["outcome"] == "NOT_RECEIVED"


async def test_received_outcome_confirms_tranche(world):
    inv, t, d = await sme_disputed(world)
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="RECEIVED", reason="received", now=at(hours=5))
    assert t.status == "CONFIRMED" and t.confirmed_at == at(hours=5)
    transfer = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == t.id))).scalar_one()
    assert transfer.confirmation_level == "CONFIRMED"


async def test_not_received_outcome_voids_tranche_without_refund(world):
    inv, t, d = await sme_disputed(world)
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_RECEIVED", reason="not received", now=at(hours=5))
    assert t.status == "VOID" and t.void_reason == "NOT_RECEIVED"
    assert (await world.db.execute(select(func.count()).select_from(Transfer).where(Transfer.kind == "REFUND"))).scalar_one() == 0
    keys = {e.emit_key for e in (await world.db.execute(select(CicEvent))).scalars()}
    assert {f"attested:{t.id}", f"void:{t.id}"} <= keys  # the correction is a new event; the original stays


async def test_received_after_exclusion_voids_with_refund_transfer(world):
    await fund_window(world, [60 * M])
    inv = await make_investor(world)
    t = await attested_tranche(world, inv, 10 * M, now=at(days=14, hours=-1))
    await tranches.sme_dispute_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, statement="x", file_id=None, now=at(days=14, hours=-1, minutes=1), params=world.params)
    await close.advance_window(world.db, window_id=world.window.id, now=at(days=14), params=world.params)
    await close.advance_window(world.db, window_id=world.window.id, now=ict(2026, 11, 19), params=world.params)
    await world.db.refresh(world.window)
    assert world.window.status == "REPAYING" and t.excluded_at_close
    d = (await world.db.execute(select(Dispute).where(Dispute.tranche_id == t.id))).scalar_one()
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="RECEIVED", reason="money did arrive, after the loan formed", now=ict(2026, 11, 20, 10))
    assert t.status == "VOID" and t.void_reason == "EXCLUDED_AT_CLOSE"  # PROVISIONAL(Q-D7): never joins
    refund = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == t.id, Transfer.kind == "REFUND"))).scalar_one()
    assert refund.amount_vnd == 10 * M and refund.due_by is not None and refund.check_status == "DUE"
    assert world.window.confirmed_total_vnd == 60 * M


async def test_late_receipt_revival_rechecks_capacity(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    await tranches.expire_holds(world.db, now=at(hours=3))
    await upload_receipt(world, inv, t, now=at(hours=4), fields=None)
    d = (await world.db.execute(select(Dispute).where(Dispute.tranche_id == t.id))).scalar_one()
    other = await make_investor(world)
    await reserve(world, other, 100 * M, now=at(hours=4, minutes=1))  # takes the whole capacity: the late ticket held none
    with pytest.raises(DirectTransferError) as info:
        await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="RECEIVED", reason="revive", now=at(hours=5))
    assert info.value.code == "CAPACITY_EXCEEDED"
    await world.db.refresh(d)
    assert d.status == "OPEN"
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_RECEIVED", reason="no room; SME refunds", now=at(hours=5))
    assert t.status == "VOID"


async def test_late_receipt_revival_with_capacity_confirms(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    await tranches.expire_holds(world.db, now=at(hours=3))
    await upload_receipt(world, inv, t, now=at(hours=4), fields=None)
    d = (await world.db.execute(select(Dispute).where(Dispute.tranche_id == t.id))).scalar_one()
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="RECEIVED", reason="revived", now=at(hours=5))
    assert t.status == "CONFIRMED"


async def test_paid_outcome_matches_repayment_day(loan):
    world, _, tr = loan
    day = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id, RepaymentDay.day_index == 1, RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="not received", file_id=None, now=ict(2026, 11, 5, 10), params=world.params)
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    await disputes.submit_dispute_evidence(world.db, dispute_id=d.id, user_id=world.sme.id, statement="SME bank statement", file_id=None, now=ict(2026, 11, 6, 9))
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="PAID", reason="SME statement shows the transfer", now=ict(2026, 11, 6, 12))
    assert day.status == "MATCHED" and day.settled_at == ict(2026, 11, 6, 12)
    keys = {e.emit_key for e in (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "MISSED_DAY_CORRECTION"))).scalars()}
    assert keys == {f"missed:{day.id}:reported", f"missed:{day.id}:resolved_paid"}


async def test_not_paid_outcome_keeps_reported_missed(loan):
    world, _, tr = loan
    day = (await world.db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tr[0].id, RepaymentDay.day_index == 1, RepaymentDay.kind == "INSTALMENT"))).scalar_one()
    await repayment.report_missing_day(world.db, day_id=day.id, investor_id=tr[0].investor_id, statement="not received", file_id=None, now=ict(2026, 11, 5, 10), params=world.params)
    d = (await world.db.execute(select(Dispute).where(Dispute.repayment_day_id == day.id))).scalar_one()
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_PAID", reason="no payment found", now=ict(2026, 11, 6, 12))
    assert day.status == "REPORTED_MISSED" and day.settled_at is None and d.status == "DECIDED"


async def test_bank_trace_escalation_recorded(world):
    inv, t, d = await sme_disputed(world)
    with pytest.raises(DirectTransferError) as info:
        await disputes.escalate_to_bank_trace(world.db, dispute_id=d.id, staff_id=world.admin.id, reason="", now=at(hours=6))
    assert info.value.status_code == 422
    await disputes.escalate_to_bank_trace(world.db, dispute_id=d.id, staff_id=world.admin.id, reason="Statements conflict; investor to request tra soat", now=at(hours=6))
    assert d.status == "BANK_TRACE" and d.required_next_party == "STAFF" and d.bank_trace_reason and d.bank_trace_at == at(hours=6)
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="RECEIVED", reason="Bank trace: credited", now=at(days=3))
    assert d.status == "DECIDED"


async def test_one_open_dispute_per_subject(world):
    inv, t, d = await sme_disputed(world)
    with pytest.raises(DirectTransferError) as info:
        await disputes.open_tranche_dispute(world.db, tranche=t, cause="RECEIPT_MISMATCH", now=at(hours=2), params=world.params)
    assert info.value.code == "DISPUTE_ALREADY_OPEN"


async def test_outsider_gets_404(world):
    inv, t, d = await sme_disputed(world)
    stranger = await make_investor(world)
    with pytest.raises(DirectTransferError) as info:
        await disputes.get_dispute_for_party(world.db, dispute_id=d.id, user_id=stranger.user.id)
    assert info.value.status_code == 404
    assert (await disputes.get_dispute_for_party(world.db, dispute_id=d.id, user_id=inv.user.id)).id == d.id
    assert (await disputes.get_dispute_for_party(world.db, dispute_id=d.id, user_id=world.sme.id)).id == d.id
    assert (await disputes.get_dispute_for_party(world.db, dispute_id=d.id, user_id=stranger.user.id, is_staff=True)).id == d.id


async def test_invalid_outcome_for_subject_type_rejected(world):
    inv, t, d = await sme_disputed(world)
    with pytest.raises(DirectTransferError) as info:
        await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="PAID", reason="wrong word", now=at(hours=5))
    assert info.value.status_code == 400 and info.value.code == "INVALID_OUTCOME"


async def test_corrections_are_new_cic_events_not_edits(world):
    inv, t, d = await sme_disputed(world)
    before = (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "ATTESTED"))).scalar_one()
    snapshot = (before.id, before.payload, before.sent_at)
    await disputes.decide_dispute(world.db, dispute_id=d.id, staff_id=world.admin.id, outcome="NOT_RECEIVED", reason="not received", now=at(hours=5))
    after = (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "ATTESTED"))).scalar_one()
    assert (after.id, after.payload, after.sent_at) == snapshot  # the reported event is never edited
    voids = (await world.db.execute(select(CicEvent).where(CicEvent.event_type == "VOID"))).scalars().all()
    assert len(voids) == 1 and voids[0].id != after.id  # the correction is a second event
