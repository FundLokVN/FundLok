"""HANDOFF-01 §5 + §3.6: ledger append-only invariant at the application layer.

app/ledger/service.py only ever inserts: a second posting adds new rows and
leaves earlier ones as they were. The database-layer trigger that refuses
UPDATE/DELETE is covered in tests/ledger/test_ledger_foundation.py. Posted the
way the direct-transfer path posts (app/direct_transfer/ledger_posting.py):
against the platform EXTERNAL account, never OMNIBUS_CASH (D-026).
"""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.ledger.models import LedgerEntry
from app.ledger.service import LedgerLeg, get_or_create_account, post_transaction, resolve_custodial_account


async def test_a_second_posting_appends_and_leaves_the_first_entry_untouched(db_session, funded_contract):
    setup = await funded_contract(target_amount="10000.00", min_ticket="1000.00")
    contract_id = uuid.UUID(setup["contract"]["id"])
    custodial = await resolve_custodial_account(db_session, contract_id=contract_id)
    external = await get_or_create_account(db_session, account_type="EXTERNAL", custodial_account_id=custodial.id)
    borrower = await get_or_create_account(
        db_session, account_type="BORROWER", custodial_account_id=custodial.id,
        owner_user_id=uuid.UUID(setup["sme"]["id"]), contract_id=contract_id,
    )

    def leg(amount: str) -> LedgerLeg:
        return LedgerLeg(external.id, borrower.id, Decimal(amount), "DISBURSEMENT", contract_id)

    first = await post_transaction(
        db_session, event_type="DISBURSEMENT", legs=[leg("10000")], idempotency_key=f"append-only-{uuid.uuid4().hex}"
    )
    await db_session.commit()
    second = await post_transaction(
        db_session, event_type="DISBURSEMENT", legs=[leg("2500")], idempotency_key=f"append-only-{uuid.uuid4().hex}"
    )
    await db_session.commit()

    assert second.id != first.id
    rows = (
        await db_session.execute(
            select(LedgerEntry.ledger_transaction_id, LedgerEntry.amount).where(LedgerEntry.contract_id == contract_id)
        )
    ).all()
    assert {r.ledger_transaction_id: r.amount for r in rows} == {first.id: Decimal("10000"), second.id: Decimal("2500")}
    assert len(rows) == 2
