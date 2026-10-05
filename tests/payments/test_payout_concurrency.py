"""Real concurrency: each task gets its own session and transaction, so the
PostgreSQL locks and unique indexes are what is under test (the shared
`client`/`db_session` used by the API tests serialises everything).

Spec: docs/specs/payments/payout-idempotency.md, section 6.2.
"""
import asyncio
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select

from app.ledger.models import CustodialAccount, LedgerAccount, LedgerTransaction
from app.ledger.service import LedgerLeg, get_or_create_account, post_transaction
from app.payments import payouts
from app.payments.idempotency import idempotent_request
from app.payments.models import IdempotencyRecord, PayoutInstruction
from app.payments.schemas import PayoutInstructionOut

SCOPE = "POST /payments/disbursements"


async def _count_instructions(session_factory, contract_id) -> int:
    async with session_factory() as db:
        return (
            await db.execute(
                select(func.count()).select_from(PayoutInstruction).where(PayoutInstruction.contract_id == contract_id)
            )
        ).scalar_one()


async def test_concurrent_creates_with_different_keys_create_one_instruction(payout_ready, session_factory):
    """Ten admins (or ten double-clicks with fresh keys) at once."""
    setup = await payout_ready()
    contract_id = uuid.UUID(setup["contract"]["id"])
    beneficiary_id = uuid.UUID(setup["beneficiary_id"])
    maker_id = uuid.UUID(setup["admin"]["id"])

    async def attempt() -> str:
        async with session_factory() as db:
            try:
                await payouts.create_disbursement_instruction(
                    db, contract_id=contract_id, beneficiary_id=beneficiary_id, maker_id=maker_id
                )
                await db.commit()
                return "created"
            except HTTPException as exc:
                await db.rollback()
                return str(exc.status_code)

    results = await asyncio.gather(*(attempt() for _ in range(10)))
    assert sorted(results) == ["409"] * 9 + ["created"]
    assert await _count_instructions(session_factory, contract_id) == 1


async def test_concurrent_same_key_requests_replay_one_result(payout_ready, session_factory):
    """Ten retries of the same request in flight at once: one runs, nine wait
    on the claim's unique index and replay its stored response."""
    setup = await payout_ready()
    actor_id = uuid.UUID(setup["admin"]["id"])
    body = {"contract_id": setup["contract"]["id"], "beneficiary_id": setup["beneficiary_id"]}

    async def request() -> tuple[str, dict]:
        async with session_factory() as db:
            async with idempotent_request(db, actor_id=actor_id, scope=SCOPE, key="race-same-key-0001", body=body) as claim:
                if claim.replay:
                    return "replay", claim.replay.body
                instruction = await payouts.create_disbursement_instruction(
                    db,
                    contract_id=uuid.UUID(body["contract_id"]),
                    beneficiary_id=uuid.UUID(body["beneficiary_id"]),
                    maker_id=actor_id,
                )
                await db.refresh(instruction)
                out = PayoutInstructionOut.from_instruction(instruction).model_dump(mode="json")
                await asyncio.sleep(0.05)  # hold the claim open while the others pile up
                await claim.complete(status_code=202, body=out)
                await db.commit()
                return "fresh", out

    results = await asyncio.gather(*(request() for _ in range(10)))
    kinds = sorted(kind for kind, _ in results)
    assert kinds == ["fresh"] + ["replay"] * 9
    assert len({body["id"] for _, body in results}) == 1
    assert await _count_instructions(session_factory, setup["contract"]["id"]) == 1
    async with session_factory() as db:
        claims = (
            await db.execute(
                select(func.count()).select_from(IdempotencyRecord).where(
                    IdempotencyRecord.idempotency_key == "race-same-key-0001"
                )
            )
        ).scalar_one()
    assert claims == 1


async def test_concurrent_first_use_creates_one_omnibus_account(session_factory):
    """Before the NULLS NOT DISTINCT fix this created several OMNIBUS_CASH
    rows, after which every payment failed with MultipleResultsFound."""
    async with session_factory() as db:
        platform_id = (
            await db.execute(select(CustodialAccount.id).where(CustodialAccount.scope == "PLATFORM"))
        ).scalar_one()

    async def first_use() -> uuid.UUID:
        async with session_factory() as db:
            account = await get_or_create_account(db, account_type="OMNIBUS_CASH", custodial_account_id=platform_id)
            await db.commit()
            return account.id

    ids = await asyncio.gather(*(first_use() for _ in range(10)))
    assert len(set(ids)) == 1
    async with session_factory() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(LedgerAccount).where(LedgerAccount.account_type == "OMNIBUS_CASH")
            )
        ).scalar_one()
    assert count == 1


async def test_concurrent_post_transaction_same_key_returns_one_transaction(payout_ready, session_factory):
    """Used to fail the loser with a 500 (UniqueViolation at flush)."""
    setup = await payout_ready()
    contract_id = uuid.UUID(setup["contract"]["id"])
    async with session_factory() as db:
        platform_id = (
            await db.execute(select(CustodialAccount.id).where(CustodialAccount.scope == "PLATFORM"))
        ).scalar_one()
        omnibus = await get_or_create_account(db, account_type="OMNIBUS_CASH", custodial_account_id=platform_id)
        borrower = await get_or_create_account(
            db,
            account_type="BORROWER",
            custodial_account_id=platform_id,
            owner_user_id=uuid.UUID(setup["sme"]["id"]),
            contract_id=contract_id,
        )
        await db.commit()

    async def post() -> uuid.UUID:
        async with session_factory() as db:
            txn = await post_transaction(
                db,
                event_type="DISBURSEMENT",
                legs=[
                    LedgerLeg(
                        debit_account_id=omnibus.id,
                        credit_account_id=borrower.id,
                        amount=10000,
                        type="DISBURSEMENT",
                        contract_id=contract_id,
                    )
                ],
                idempotency_key="race-ledger-key-0001",
            )
            await asyncio.sleep(0.02)
            await db.commit()
            return txn.id

    ids = await asyncio.gather(*(post() for _ in range(5)))
    assert len(set(ids)) == 1
    async with session_factory() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(LedgerTransaction).where(
                    LedgerTransaction.idempotency_key == "race-ledger-key-0001"
                )
            )
        ).scalar_one()
    assert count == 1
