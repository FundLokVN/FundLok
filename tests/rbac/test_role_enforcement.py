"""HANDOFF-01 §5: app/utils/rbac.py::require_roles enforcement.

POST /market/listings is gated by require_roles(Role.ADMIN, Role.SYSTEM_ADMIN).
We send a syntactically valid body (a real UUID, valid decimals) so the 403 can
only be coming from the role check, not from request body validation (422).
"""
import uuid

import pytest

# The operator steps that take an approved application to the marketplace.
# SYSTEM_ADMIN may run them too: the admin panel shows it the same buttons.
# Every id is random, so passing the role check ends in a 404/400, never a 403.
OPERATOR_STEPS = [
    ("/underwriting/score-runs", lambda: {"application_id": str(uuid.uuid4())}),
    ("/contracts/", lambda: {"application_id": str(uuid.uuid4()), "score_run_id": str(uuid.uuid4())}),
    (f"/lending/documents/{uuid.uuid4()}/approve", lambda: {"rationale": "checked"}),
    ("/market/listings", lambda: {"contract_id": str(uuid.uuid4()), "target_amount": 1000, "min_ticket": 100}),
]


@pytest.mark.parametrize("path,body", OPERATOR_STEPS, ids=[p for p, _ in OPERATOR_STEPS])
@pytest.mark.parametrize("fixture", ["make_admin", "make_system_admin"])
async def test_both_admin_roles_pass_the_operator_role_check(client, request, fixture, path, body):
    admin = await request.getfixturevalue(fixture)()
    resp = await client.post(path, json=body(), headers=admin["headers"])
    assert resp.status_code in (400, 404), resp.text


@pytest.mark.parametrize("role", ["SME", "INVESTOR"])
async def test_role_protected_route_rejects_wrong_role_403(client, make_user, role):
    user = await make_user(role=role)
    resp = await client.post(
        "/market/listings",
        json={"contract_id": str(uuid.uuid4()), "target_amount": "1000.00", "min_ticket": "100.00"},
        headers=user["headers"],
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Not authorized for this action"}


async def test_role_protected_route_rejects_unauthenticated(client):
    resp = await client.post(
        "/market/listings",
        json={"contract_id": str(uuid.uuid4()), "target_amount": "1000.00", "min_ticket": "100.00"},
    )
    assert resp.status_code == 401
