"""Disbursement instructions over HTTP: Idempotency-Key semantics, the
one-live-disbursement-per-contract rule, server-derived amount and
destination, and maker-checker approval.

Spec: docs/specs/payments/payout-idempotency.md, section 8.
"""
import re
import uuid

from sqlalchemy import func, select, update

from app.ledger.models import LedgerTransaction
from app.lending.models import Listing
from app.payments.models import IdempotencyRecord, PayoutEvent, PayoutInstruction


async def _instruction_count(db_session, contract_id: str) -> int:
    return (
        await db_session.execute(
            select(func.count()).select_from(PayoutInstruction).where(PayoutInstruction.contract_id == contract_id)
        )
    ).scalar_one()


async def test_create_disbursement_requires_idempotency_key(payout_ready, create_disbursement):
    setup = await payout_ready()
    missing = await create_disbursement(setup, None)
    assert missing.status_code == 400
    too_short = await create_disbursement(setup, "abc")
    assert too_short.status_code == 400


async def test_create_disbursement_happy_path(payout_ready, create_disbursement, db_session):
    setup = await payout_ready(target_amount="10000.00")
    resp = await create_disbursement(setup, "disb-happy-path-0001")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["amount"] == 10000
    assert body["purpose"] == "SME_DISBURSEMENT"
    assert re.fullmatch(r"FL[0-9A-Z]{26}", body["client_request_id"])
    assert body["beneficiary_account_masked"].startswith("****")
    assert body["approved_by"] is None
    assert resp.headers.get("Idempotent-Replayed") is None

    # Nothing reaches the ledger until the bank confirms the transfer.
    ledger_count = (
        await db_session.execute(
            select(func.count()).select_from(LedgerTransaction).where(
                LedgerTransaction.contract_id == setup["contract"]["id"]
            )
        )
    ).scalar_one()
    assert ledger_count == 0


async def test_create_disbursement_replay_returns_same_response(payout_ready, create_disbursement, db_session):
    setup = await payout_ready()
    first = await create_disbursement(setup, "disb-replay-0001")
    second = await create_disbursement(setup, "disb-replay-0001")
    assert first.status_code == second.status_code == 202
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert await _instruction_count(db_session, setup["contract"]["id"]) == 1


async def test_create_disbursement_same_key_different_body_returns_422(
    payout_ready, create_disbursement, make_beneficiary, db_session
):
    setup = await payout_ready()
    other = await make_beneficiary(setup["sme"]["id"], setup["admin"]["id"])
    first = await create_disbursement(setup, "disb-mismatch-0001")
    assert first.status_code == 202
    second = await create_disbursement(
        setup,
        "disb-mismatch-0001",
        body={"contract_id": setup["contract"]["id"], "beneficiary_id": str(other.id)},
    )
    assert second.status_code == 422
    assert await _instruction_count(db_session, setup["contract"]["id"]) == 1


async def test_create_disbursement_second_key_same_contract_returns_409(payout_ready, create_disbursement, db_session):
    setup = await payout_ready()
    assert (await create_disbursement(setup, "disb-double-click-1")).status_code == 202
    second = await create_disbursement(setup, "disb-double-click-2")
    assert second.status_code == 409
    assert await _instruction_count(db_session, setup["contract"]["id"]) == 1


async def test_create_disbursement_rejects_client_supplied_amount(payout_ready, create_disbursement):
    setup = await payout_ready()
    resp = await create_disbursement(
        setup,
        "disb-client-amount-1",
        body={
            "contract_id": setup["contract"]["id"],
            "beneficiary_id": setup["beneficiary_id"],
            "amount": 50_000_000,
            "bank_account": "999999999",
        },
    )
    assert resp.status_code == 422


async def test_create_disbursement_rejects_unverified_or_foreign_beneficiary(
    payout_ready, create_disbursement, make_beneficiary
):
    setup = await payout_ready()
    pending = await make_beneficiary(setup["sme"]["id"], setup["admin"]["id"], status="PENDING_VERIFICATION")
    foreign = await make_beneficiary(setup["investor"]["id"], setup["admin"]["id"])
    for beneficiary_id, key in ((pending.id, "disb-benef-pending"), (foreign.id, "disb-benef-foreign"), (uuid.uuid4(), "disb-benef-missing")):
        resp = await create_disbursement(
            setup, key, body={"contract_id": setup["contract"]["id"], "beneficiary_id": str(beneficiary_id)}
        )
        assert resp.status_code == 422, resp.text


async def test_create_disbursement_requires_funded_contract(open_listing, make_beneficiary, create_disbursement):
    setup = await open_listing()
    beneficiary = await make_beneficiary(setup["sme"]["id"], setup["admin"]["id"])
    setup["beneficiary_id"] = str(beneficiary.id)
    resp = await create_disbursement(setup, "disb-not-funded-01")
    assert resp.status_code == 409


async def test_create_disbursement_blocks_unreconciled_funding(payout_ready, create_disbursement, db_session):
    """The market's lost-update race can leave listing.funded_amount out of
    step with the holdings; a disbursement must refuse to run on that."""
    setup = await payout_ready(target_amount="10000.00")
    await db_session.execute(
        update(Listing).where(Listing.id == setup["listing"]["id"]).values(funded_amount=6000)
    )
    await db_session.commit()
    resp = await create_disbursement(setup, "disb-unreconciled-1")
    assert resp.status_code == 409
    assert "reconcile" in resp.json()["detail"]


async def test_create_disbursement_rejects_non_admin(payout_ready, create_disbursement):
    setup = await payout_ready()
    resp = await create_disbursement(setup, "disb-investor-0001", actor=setup["investor"])
    assert resp.status_code == 403


async def test_approve_requires_a_different_admin(payout_ready, create_disbursement, approve_disbursement):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "disb-maker-checker-1")).json()

    own = await approve_disbursement(created["id"], setup["admin"], "approve-own-0001")
    assert own.status_code == 403

    checked = await approve_disbursement(created["id"], setup["checker"], "approve-checker-01")
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "APPROVED"
    assert checked.json()["approved_by"] == setup["checker"]["id"]


async def test_approve_replay_and_second_approval(payout_ready, create_disbursement, approve_disbursement):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "disb-approve-replay")).json()
    first = await approve_disbursement(created["id"], setup["checker"], "approve-replay-01")
    replay = await approve_disbursement(created["id"], setup["checker"], "approve-replay-01")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replayed") == "true"

    again = await approve_disbursement(created["id"], setup["checker"], "approve-replay-02")
    assert again.status_code == 409


async def test_cancel_frees_the_contract_for_a_new_instruction(payout_ready, create_disbursement, client):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "disb-cancel-0001")).json()
    cancelled = await client.post(
        f"/payments/disbursements/{created['id']}/cancel",
        headers={**setup["admin"]["headers"], "Idempotency-Key": "cancel-0001-key"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    fresh = await create_disbursement(setup, "disb-cancel-0002")
    assert fresh.status_code == 202
    assert fresh.json()["client_request_id"] != created["client_request_id"]


async def test_idempotency_keys_are_scoped_per_actor(payout_ready, create_disbursement):
    """Another admin sending the same key must not receive the first admin's
    response; their request is judged on its own (here: the contract already
    has an instruction)."""
    setup = await payout_ready()
    assert (await create_disbursement(setup, "disb-shared-key-01")).status_code == 202
    other = await create_disbursement(setup, "disb-shared-key-01", actor=setup["checker"])
    assert other.status_code == 409
    assert other.headers.get("Idempotent-Replayed") is None


async def test_failed_request_leaves_no_claim(payout_ready, create_disbursement, make_beneficiary, db_session):
    """A request that fails is rolled back with its claim, so a retry with the
    same key runs fresh instead of replaying the error."""
    setup = await payout_ready()
    pending = await make_beneficiary(setup["sme"]["id"], setup["admin"]["id"], status="PENDING_VERIFICATION")
    body = {"contract_id": setup["contract"]["id"], "beneficiary_id": str(pending.id)}
    first = await create_disbursement(setup, "disb-retry-after-fix", body=body)
    assert first.status_code == 422
    claims = (
        await db_session.execute(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key == "disb-retry-after-fix"
            )
        )
    ).scalar_one()
    assert claims == 0

    pending.status = "VERIFIED"
    pending.verified_by = uuid.UUID(setup["admin"]["id"])
    pending.verified_at = func.now()
    await db_session.commit()
    retry = await create_disbursement(setup, "disb-retry-after-fix", body=body)
    assert retry.status_code == 202, retry.text


async def test_status_changes_are_logged_with_the_acting_user(
    payout_ready, create_disbursement, approve_disbursement, db_session
):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "disb-event-log-0001")).json()
    await approve_disbursement(created["id"], setup["checker"], "approve-event-log-1")
    events = (
        await db_session.execute(
            select(PayoutEvent)
            .where(PayoutEvent.instruction_id == created["id"], PayoutEvent.event_type == "STATUS_CHANGED")
            .order_by(PayoutEvent.created_at)
        )
    ).scalars().all()
    assert [(e.from_status, e.to_status) for e in events] == [(None, "PENDING_APPROVAL"), ("PENDING_APPROVAL", "APPROVED")]
    assert str(events[0].actor_id) == setup["admin"]["id"]
    assert str(events[1].actor_id) == setup["checker"]["id"]
