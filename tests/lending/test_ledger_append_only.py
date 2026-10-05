"""HANDOFF-01 §5 + §3.6: ledger append-only invariant.

The service layer only ever inserts into ledger_entries (app/payments/service.py
has no update/delete path), and no route anywhere in the app exposes a way to
mutate or delete an existing ledger entry.
"""
from app.main import app
from conftest import iter_endpoint_routes


def test_ledger_entry_has_no_exposed_mutation_route():
    """Pure route introspection -- no DB/client needed, so this stays sync."""
    ledger_mutation_routes = [
        route
        for route in iter_endpoint_routes(app.routes)
        if route.path.startswith("/payments") and any(m in route.methods for m in ("PUT", "PATCH", "DELETE"))
    ]
    assert ledger_mutation_routes == []


def test_payments_routes_are_create_only():
    """Every registered /payments route is a POST (create); there is nothing
    to GET/PUT/PATCH/DELETE an individual ledger entry by id. Pure route
    introspection -- no DB/client needed, so this stays sync."""
    payments_routes = [route for route in iter_endpoint_routes(app.routes) if route.path.startswith("/payments")]
    assert payments_routes, "expected at least the disbursements/repayments routes to be registered"
    for route in payments_routes:
        assert route.methods <= {"POST", "HEAD", "OPTIONS"}


async def test_disbursement_cannot_be_recorded_twice_for_one_contract(payout_ready, create_disbursement):
    """Used to assert the opposite: a second disbursement with a new key was
    accepted and posted a second DISBURSEMENT for the same contract. A
    contract now holds at most one live payout instruction, and nothing
    reaches the ledger until the bank confirms the transfer."""
    setup = await payout_ready()
    first = await create_disbursement(setup, "append-only-check-1")
    assert first.status_code == 202

    second = await create_disbursement(setup, "append-only-check-2")
    assert second.status_code == 409
