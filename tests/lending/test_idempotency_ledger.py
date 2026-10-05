"""HANDOFF-01 §5 + §3.5: ledger idempotency (highest-value coverage).

Repayments are recorded through the generic Idempotency-Key layer
(app/payments/idempotency.py): the header is required, a replay returns the
stored response, and a reused key with a different body is refused.
Disbursement idempotency moved with disbursements themselves to
tests/payments/ (payout instructions, docs/specs/payments/payout-idempotency.md).
"""
from sqlalchemy import func, select

from app.ledger.models import LedgerTransaction


def _repayment_body(setup: dict, amount: str = "500.00") -> dict:
    return {
        "contract_id": setup["contract"]["id"],
        "amount": amount,
        "paid_at": "2026-07-04T00:00:00Z",
        "reference": "test-repayment",
    }


async def test_repayment_requires_idempotency_key(funded_contract, client):
    setup = await funded_contract(target_amount="10000.00", min_ticket="1000.00")
    resp = await client.post("/payments/repayments", json=_repayment_body(setup), headers=setup["admin"]["headers"])
    assert resp.status_code == 400
    assert "Idempotency-Key" in resp.json()["detail"]


async def test_duplicate_idempotency_key_on_repayment_returns_existing_row(funded_contract, client, db_session):
    setup = await funded_contract(target_amount="10000.00", min_ticket="1000.00")
    headers = {**setup["admin"]["headers"], "Idempotency-Key": "idem-repayment-key-1"}

    first = await client.post("/payments/repayments", json=_repayment_body(setup), headers=headers)
    assert first.status_code == 201
    second = await client.post("/payments/repayments", json=_repayment_body(setup), headers=headers)
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replayed") == "true"

    count = (
        await db_session.execute(
            select(func.count()).select_from(LedgerTransaction).where(LedgerTransaction.contract_id == setup["contract"]["id"])
        )
    ).scalar_one()
    assert count == 1


async def test_repayment_key_reused_with_different_body_returns_422(funded_contract, client):
    setup = await funded_contract(target_amount="10000.00", min_ticket="1000.00")
    headers = {**setup["admin"]["headers"], "Idempotency-Key": "idem-repayment-key-2"}

    first = await client.post("/payments/repayments", json=_repayment_body(setup, "500.00"), headers=headers)
    assert first.status_code == 201
    second = await client.post("/payments/repayments", json=_repayment_body(setup, "700.00"), headers=headers)
    assert second.status_code == 422
