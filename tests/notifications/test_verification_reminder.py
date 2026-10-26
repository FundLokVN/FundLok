"""The "verify first" reminder.

Choosing a role is the one step every new SME and investor passes through, so
it leaves a VERIFICATION_REQUIRED notification. Once they have submitted the
verification it asks for and it is approved, or parked with the review team,
the reminder is marked read: the bell must not keep asking someone to do what
they already did. A rejection keeps it, because they still have to act.

Submissions go through the real /gverify flows with the provider mocked,
reusing the GVerify fixtures, so these hold what the engine writes.
"""
import pytest

from tests.admin.test_kyc_review import parked_attempt  # noqa: F401
from tests.gverify.test_kyb_verify import (  # noqa: F401
    _body as _kyb_body,
    make_verified_sme,
    mock_gverify_kyb,
)
from tests.gverify.test_kyc_verify import (  # noqa: F401
    _body as _kyc_body,
    _face_data,
    mock_gverify,
)


async def _bell(client, user):
    resp = await client.get("/notifications", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("role, check", [("SME", "KYB"), ("INVESTOR", "KYC")])
async def test_choosing_a_role_leaves_a_reminder(client, make_user, role, check):
    user = await make_user(role=role)

    bell = await _bell(client, user)
    assert bell["unread"] == 1
    [reminder] = bell["items"]
    assert reminder["event"] == "VERIFICATION_REQUIRED"
    # Which verification, so the client can say "your business" or "your
    # identity". Placeholders only, like every other notification.
    assert reminder["data"] == {"check": check}
    assert reminder["entity_type"] == "USER"
    assert reminder["entity_id"] == user["id"]


async def test_an_approved_kyc_clears_it(client, make_user, mock_gverify):
    investor = await make_user(role="INVESTOR")

    resp = await client.post("/gverify/kyc/verify", json=_kyc_body(), headers=investor["headers"])

    assert resp.json()["status"] == "APPROVED", resp.text
    bell = await _bell(client, investor)
    assert bell["unread"] == 0
    # Read, not deleted: it stays in the list as history.
    assert bell["items"][0]["read_at"] is not None


async def test_a_kyc_parked_for_review_clears_it(client, parked_attempt):
    # Waiting on the review team is out of the user's hands; telling them to
    # verify again would be wrong.
    _, impostor, _ = await parked_attempt()

    assert (await _bell(client, impostor))["unread"] == 0


async def test_a_rejected_kyc_keeps_it(client, make_user, mock_gverify):
    investor = await make_user(role="INVESTOR")
    mock_gverify.face = _face_data(is_matching=False, match="0.12")

    resp = await client.post("/gverify/kyc/verify", json=_kyc_body(), headers=investor["headers"])

    assert resp.json()["status"] == "REJECTED", resp.text
    assert (await _bell(client, investor))["unread"] == 1


async def test_an_approved_kyb_clears_it(client, make_verified_sme, mock_gverify_kyb):
    sme = await make_verified_sme()

    resp = await client.post("/gverify/kyb/verify", json=_kyb_body(), headers=sme["headers"])

    assert resp.json()["status"] == "APPROVED", resp.text
    assert (await _bell(client, sme))["unread"] == 0


async def test_an_smes_own_kyc_does_not_clear_it(client, make_user, mock_gverify):
    """An SME verifies their identity as a step of KYB. That step must not
    clear a reminder that is about the business."""
    sme = await make_user(role="SME")

    resp = await client.post("/gverify/kyc/verify", json=_kyc_body(), headers=sme["headers"])

    assert resp.json()["status"] == "APPROVED", resp.text
    assert (await _bell(client, sme))["unread"] == 1
