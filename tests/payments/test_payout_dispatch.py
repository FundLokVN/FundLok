"""The dispatcher against a scripted bank: every failure mode a network can
produce must end in exactly one transfer and at most one ledger posting.

Spec: docs/specs/payments/payout-idempotency.md, sections 5, 7 and 8.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import func, select, update

from app.banking.gateway import InquiryOutcome, InquiryResult, SubmitOutcome, SubmitResult
from app.core.config import settings
from app.ledger.models import LedgerEntry, LedgerTransaction
from app.payments import payouts
from app.payments.models import PayoutEvent, PayoutInstruction
from tests.payments.fakes import FakeGateway


async def _instruction(session_factory, instruction_id) -> PayoutInstruction:
    async with session_factory() as db:
        return (await db.execute(select(PayoutInstruction).where(PayoutInstruction.id == instruction_id))).scalar_one()


async def _disbursement_postings(session_factory, contract_id) -> list[LedgerTransaction]:
    async with session_factory() as db:
        return list(
            (
                await db.execute(
                    select(LedgerTransaction).where(
                        LedgerTransaction.contract_id == contract_id, LedgerTransaction.event_type == "DISBURSEMENT"
                    )
                )
            ).scalars()
        )


async def _make_due(session_factory, instruction_id) -> None:
    """Skip the backoff so the next dispatcher/reconciler pass picks it up."""
    async with session_factory() as db:
        await db.execute(
            update(PayoutInstruction)
            .where(PayoutInstruction.id == instruction_id)
            .values(next_check_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 1))
        )
        await db.commit()


async def test_dispatch_success_posts_ledger_exactly_once(approved_instruction, session_factory):
    setup, instr = await approved_instruction(target_amount="10000.00")
    gateway = FakeGateway(submit=[SubmitResult(SubmitOutcome.SUCCEEDED, bank_transaction_ref="BANK-OK-1")])

    assert await payouts.dispatch_approved(session_factory, gateway) == ["SUCCEEDED"]

    row = await _instruction(session_factory, instr["id"])
    assert row.status == "SUCCEEDED"
    assert row.bank_transaction_ref == "BANK-OK-1"
    postings = await _disbursement_postings(session_factory, setup["contract"]["id"])
    assert len(postings) == 1
    assert postings[0].idempotency_key == f"payout:{instr['id']}"
    assert row.ledger_transaction_id == postings[0].id
    async with session_factory() as db:
        amount = (
            await db.execute(select(LedgerEntry.amount).where(LedgerEntry.ledger_transaction_id == postings[0].id))
        ).scalar_one()
    assert int(amount) == 10000
    assert gateway.submits[0].client_request_id == instr["client_request_id"]
    assert gateway.submits[0].amount == 10000


async def test_unapproved_instruction_is_never_dispatched(payout_ready, create_disbursement, session_factory):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "dispatch-unapproved-1")).json()
    gateway = FakeGateway()
    assert await payouts.dispatch_approved(session_factory, gateway) == []
    assert gateway.submits == []
    assert (await _instruction(session_factory, created["id"])).status == "PENDING_APPROVAL"


async def test_accepted_then_inquiry_succeeded(approved_instruction, session_factory):
    setup, instr = await approved_instruction()
    gateway = FakeGateway(
        submit=[SubmitResult(SubmitOutcome.ACCEPTED, bank_transaction_ref="BANK-ACK-1")],
        inquiry=[InquiryResult(InquiryOutcome.SUCCEEDED, bank_transaction_ref="BANK-ACK-1")],
    )
    assert await payouts.dispatch_approved(session_factory, gateway) == ["SUBMITTED"]
    assert await _disbursement_postings(session_factory, setup["contract"]["id"]) == []

    await _make_due(session_factory, instr["id"])
    assert await payouts.reconcile_open_instructions(session_factory, gateway) == ["SUCCEEDED"]
    assert gateway.inquiries == [instr["client_request_id"]]
    assert len(await _disbursement_postings(session_factory, setup["contract"]["id"])) == 1


async def test_timeout_marks_unknown_and_never_resends(approved_instruction, session_factory):
    setup, instr = await approved_instruction()
    gateway = FakeGateway(
        submit=[SubmitResult(SubmitOutcome.UNKNOWN, message="read timeout")],
        inquiry=[InquiryResult(InquiryOutcome.SUCCEEDED, bank_transaction_ref="BANK-LATE-1")],
    )
    assert await payouts.dispatch_approved(session_factory, gateway) == ["UNKNOWN"]

    # Another dispatcher pass must not send it again: UNKNOWN is not APPROVED.
    await _make_due(session_factory, instr["id"])
    assert await payouts.dispatch_approved(session_factory, gateway) == []
    assert len(gateway.submits) == 1

    assert await payouts.reconcile_open_instructions(session_factory, gateway) == ["SUCCEEDED"]
    assert len(gateway.submits) == 1
    assert len(await _disbursement_postings(session_factory, setup["contract"]["id"])) == 1


async def test_gateway_exception_is_treated_as_unknown(approved_instruction, session_factory):
    _, instr = await approved_instruction()
    gateway = FakeGateway(submit=[RuntimeError("connection reset by peer")])
    assert await payouts.dispatch_approved(session_factory, gateway) == ["UNKNOWN"]
    assert (await _instruction(session_factory, instr["id"])).status == "UNKNOWN"


async def test_not_sent_is_retried_with_the_same_client_request_id(approved_instruction, session_factory):
    setup, instr = await approved_instruction()
    gateway = FakeGateway(
        submit=[
            SubmitResult(SubmitOutcome.NOT_SENT, message="connect timeout"),
            SubmitResult(SubmitOutcome.SUCCEEDED, bank_transaction_ref="BANK-RETRY-1"),
        ]
    )
    assert await payouts.dispatch_approved(session_factory, gateway) == ["APPROVED"]
    # Backed off: the very next pass leaves it alone.
    assert await payouts.dispatch_approved(session_factory, gateway) == []

    await _make_due(session_factory, instr["id"])
    assert await payouts.dispatch_approved(session_factory, gateway) == ["SUCCEEDED"]
    assert [s.client_request_id for s in gateway.submits] == [instr["client_request_id"]] * 2
    row = await _instruction(session_factory, instr["id"])
    assert row.attempt_count == 2
    assert len(await _disbursement_postings(session_factory, setup["contract"]["id"])) == 1


async def test_rejected_marks_failed_and_frees_the_contract(approved_instruction, session_factory, create_disbursement):
    setup, instr = await approved_instruction()
    gateway = FakeGateway(submit=[SubmitResult(SubmitOutcome.REJECTED, bank_code="INVALID_ACCOUNT", message="no such account")])
    assert await payouts.dispatch_approved(session_factory, gateway) == ["FAILED"]
    row = await _instruction(session_factory, instr["id"])
    assert row.failure_code == "INVALID_ACCOUNT"
    assert row.ledger_transaction_id is None
    assert await _disbursement_postings(session_factory, setup["contract"]["id"]) == []

    fresh = await create_disbursement(setup, "after-failure-create-1")
    assert fresh.status_code == 202, fresh.text


async def test_concurrent_dispatchers_submit_exactly_once(approved_instruction, session_factory):
    _, instr = await approved_instruction()
    gateway = FakeGateway(submit=[SubmitResult(SubmitOutcome.ACCEPTED)], submit_delay=0.05)
    instruction_id = uuid.UUID(instr["id"])
    results = await asyncio.gather(
        *(payouts.submit_instruction(session_factory, gateway, instruction_id) for _ in range(10))
    )
    assert len(gateway.submits) == 1
    assert sorted(results) == ["SKIPPED"] * 9 + ["SUBMITTED"]


async def test_crashed_dispatcher_is_swept_to_unknown_then_resolved(approved_instruction, session_factory):
    """Claim committed, then the worker dies before recording anything."""
    setup, instr = await approved_instruction()
    instruction_id = uuid.UUID(instr["id"])
    async with session_factory() as db:
        assert await payouts.claim_for_submission(db, instruction_id) is not None
        await db.commit()
        await db.execute(
            update(PayoutInstruction)
            .where(PayoutInstruction.id == instruction_id)
            .values(lease_expires_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 1))
        )
        await db.commit()

    gateway = FakeGateway(inquiry=[InquiryResult(InquiryOutcome.NOT_FOUND), InquiryResult(InquiryOutcome.SUCCEEDED)])
    # Sweep -> UNKNOWN; a young NOT_FOUND keeps it UNKNOWN (never back to APPROVED by default).
    assert await payouts.reconcile_open_instructions(session_factory, gateway) == ["UNKNOWN"]
    assert gateway.submits == []

    await _make_due(session_factory, instr["id"])
    assert await payouts.reconcile_open_instructions(session_factory, gateway) == ["SUCCEEDED"]
    assert gateway.submits == []
    assert len(await _disbursement_postings(session_factory, setup["contract"]["id"])) == 1


async def test_late_response_after_lease_expiry_still_settles(approved_instruction, session_factory):
    setup, instr = await approved_instruction()
    instruction_id = uuid.UUID(instr["id"])
    async with session_factory() as db:
        ticket = await payouts.claim_for_submission(db, instruction_id)
        await db.commit()
    async with session_factory() as db:
        await db.execute(
            update(PayoutInstruction)
            .where(PayoutInstruction.id == instruction_id)
            .values(lease_expires_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 1))
        )
        assert await payouts.expire_stale_submissions(db) == 1
        await db.commit()
    async with session_factory() as db:
        status = await payouts.record_submit_result(
            db, ticket, SubmitResult(SubmitOutcome.SUCCEEDED, bank_transaction_ref="BANK-SLOW-1")
        )
        await db.commit()
    assert status == "SUCCEEDED"
    assert len(await _disbursement_postings(session_factory, setup["contract"]["id"])) == 1


async def test_duplicate_outcomes_post_the_ledger_once(approved_instruction, session_factory):
    """Webhook, inquiry and a late response can all report the same success,
    even at the same moment."""
    setup, instr = await approved_instruction()
    gateway = FakeGateway(submit=[SubmitResult(SubmitOutcome.ACCEPTED)])
    await payouts.dispatch_approved(session_factory, gateway)

    async def report(source: str) -> str:
        async with session_factory() as db:
            row = await payouts.apply_bank_outcome(
                db, client_request_id=instr["client_request_id"], outcome="SUCCEEDED", source=source
            )
            status = row.status
            await db.commit()
            return status

    results = await asyncio.gather(*(report(f"WEBHOOK-{i}") for i in range(5)))
    assert results == ["SUCCEEDED"] * 5
    assert len(await _disbursement_postings(session_factory, setup["contract"]["id"])) == 1


async def test_conflicting_outcome_never_overwrites(approved_instruction, session_factory):
    _, instr = await approved_instruction()
    gateway = FakeGateway(submit=[SubmitResult(SubmitOutcome.SUCCEEDED)])
    await payouts.dispatch_approved(session_factory, gateway)
    async with session_factory() as db:
        row = await payouts.apply_bank_outcome(
            db, client_request_id=instr["client_request_id"], outcome="FAILED", source="WEBHOOK"
        )
        await db.commit()
    assert row.status == "SUCCEEDED"
    async with session_factory() as db:
        conflicts = (
            await db.execute(
                select(PayoutEvent).where(
                    PayoutEvent.instruction_id == instr["id"], PayoutEvent.event_type == "OUTCOME_APPLIED"
                )
            )
        ).scalars().all()
    assert any((e.detail or {}).get("conflict") for e in conflicts)


async def test_outcome_for_unknown_reference_is_rejected(session_factory):
    async with session_factory() as db:
        with pytest.raises(payouts.UnknownPayoutReference):
            await payouts.apply_bank_outcome(db, client_request_id="FLNOSUCHREQUEST000", outcome="SUCCEEDED", source="WEBHOOK")


async def test_unresolved_unknown_escalates_to_manual_review(approved_instruction, session_factory, monkeypatch):
    _, instr = await approved_instruction()
    monkeypatch.setattr(settings, "PAYOUT_UNKNOWN_ESCALATE_AFTER_SECONDS", 0)
    gateway = FakeGateway(submit=[SubmitResult(SubmitOutcome.UNKNOWN)], inquiry=[InquiryResult(InquiryOutcome.NOT_FOUND)])
    await payouts.dispatch_approved(session_factory, gateway)
    await _make_due(session_factory, instr["id"])
    assert await payouts.reconcile_open_instructions(session_factory, gateway) == ["MANUAL_REVIEW"]
    assert len(gateway.submits) == 1


async def test_resend_on_not_found_only_when_enabled(approved_instruction, session_factory, monkeypatch):
    _, instr = await approved_instruction()
    monkeypatch.setattr(settings, "PAYOUT_RESEND_ON_NOT_FOUND", True)
    monkeypatch.setattr(settings, "PAYOUT_NOT_FOUND_RESEND_AFTER_SECONDS", 0)
    gateway = FakeGateway(
        submit=[SubmitResult(SubmitOutcome.UNKNOWN), SubmitResult(SubmitOutcome.SUCCEEDED)],
        inquiry=[InquiryResult(InquiryOutcome.NOT_FOUND)],
    )
    await payouts.dispatch_approved(session_factory, gateway)
    await _make_due(session_factory, instr["id"])
    assert await payouts.reconcile_open_instructions(session_factory, gateway) == ["APPROVED"]
    assert await payouts.dispatch_approved(session_factory, gateway) == ["SUCCEEDED"]
    assert [s.client_request_id for s in gateway.submits] == [instr["client_request_id"]] * 2


async def test_acknowledged_then_not_found_goes_to_manual_review(approved_instruction, session_factory):
    _, instr = await approved_instruction()
    gateway = FakeGateway(submit=[SubmitResult(SubmitOutcome.ACCEPTED)], inquiry=[InquiryResult(InquiryOutcome.NOT_FOUND)])
    await payouts.dispatch_approved(session_factory, gateway)
    await _make_due(session_factory, instr["id"])
    assert await payouts.reconcile_open_instructions(session_factory, gateway) == ["MANUAL_REVIEW"]
    assert len(gateway.submits) == 1
