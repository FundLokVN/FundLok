"""Guarantees the database enforces on its own -- they must hold for a
buggy code path or a person with psql, not just for app/payments.

Spec: docs/specs/payments/payout-idempotency.md, sections 3 and 6.3.
"""
import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.payments.payouts import new_client_request_id


async def _expect_db_error(session_factory, sql: str, params: dict, match: str) -> None:
    async with session_factory() as db:
        with pytest.raises(DBAPIError) as excinfo:
            await db.execute(text(sql), params)
            await db.flush()
        await db.rollback()
    assert re.search(match, str(excinfo.value)), str(excinfo.value)


async def test_db_rejects_a_second_live_disbursement_for_a_contract(
    payout_ready, create_disbursement, session_factory
):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "guard-second-live-1")).json()
    await _expect_db_error(
        session_factory,
        """
        INSERT INTO payout_instructions (client_request_id, purpose, contract_id, custodial_account_id,
            beneficiary_id, beneficiary_bank_bin, beneficiary_account_number, beneficiary_account_name,
            amount, description, created_by)
        SELECT :crid, purpose, contract_id, custodial_account_id, beneficiary_id, beneficiary_bank_bin,
               beneficiary_account_number, beneficiary_account_name, amount, description, created_by
        FROM payout_instructions WHERE id = :id
        """,
        {"crid": new_client_request_id(), "id": created["id"]},
        "uq_payout_instructions_active_disbursement",
    )


async def test_db_rejects_skipping_approval(payout_ready, create_disbursement, session_factory):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "guard-skip-approval")).json()
    await _expect_db_error(
        session_factory,
        "UPDATE payout_instructions SET status = 'SUBMITTING' WHERE id = :id",
        {"id": created["id"]},
        "invalid status transition PENDING_APPROVAL -> SUBMITTING",
    )


async def test_db_rejects_moving_a_settled_payout_backwards(approved_instruction, session_factory):
    from app.banking.gateway import SubmitOutcome, SubmitResult
    from app.payments import payouts
    from tests.payments.fakes import FakeGateway

    _, instr = await approved_instruction()
    await payouts.dispatch_approved(session_factory, FakeGateway(submit=[SubmitResult(SubmitOutcome.SUCCEEDED)]))
    await _expect_db_error(
        session_factory,
        "UPDATE payout_instructions SET status = 'APPROVED' WHERE id = :id",
        {"id": instr["id"]},
        "invalid status transition SUCCEEDED -> APPROVED",
    )


@pytest.mark.parametrize(
    "assignment",
    ["amount = amount + 1", "client_request_id = 'FLTAMPERED0000000000'", "beneficiary_account_number = '000000001'"],
)
async def test_db_rejects_changing_amount_destination_or_request_id(
    payout_ready, create_disbursement, session_factory, assignment
):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "guard-immutable-cols")).json()
    await _expect_db_error(
        session_factory,
        f"UPDATE payout_instructions SET {assignment} WHERE id = :id",
        {"id": created["id"]},
        "immutable",
    )


async def test_db_rejects_self_approval(payout_ready, create_disbursement, session_factory):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "guard-self-approval")).json()
    await _expect_db_error(
        session_factory,
        "UPDATE payout_instructions SET status = 'APPROVED', approved_by = created_by, approved_at = now() WHERE id = :id",
        {"id": created["id"]},
        "ck_payout_instructions_maker_ne_checker",
    )


async def test_db_rejects_deleting_an_instruction(payout_ready, create_disbursement, session_factory):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "guard-delete-instr")).json()
    await _expect_db_error(
        session_factory,
        "DELETE FROM payout_instructions WHERE id = :id",
        {"id": created["id"]},
        "DELETE on payout_instructions is not allowed",
    )


async def test_payout_events_are_append_only(payout_ready, create_disbursement, session_factory):
    setup = await payout_ready()
    created = (await create_disbursement(setup, "guard-events-append")).json()
    await _expect_db_error(
        session_factory,
        "UPDATE payout_events SET to_status = 'SUCCEEDED' WHERE instruction_id = :id",
        {"id": created["id"]},
        "append-only",
    )


def test_client_request_ids_are_unique_and_bank_safe():
    ids = [new_client_request_id() for _ in range(20_000)]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"FL[0-9A-HJKMNP-TV-Z]{26}", i) for i in ids)
    assert max(len(i) for i in ids) == 28  # inside Circular 64's 60-char Request-ID limit
