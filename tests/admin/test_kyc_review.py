"""Admin review of KYC attempts parked in MANUAL_REVIEW.

The GVerify KYC engine parks an attempt when the face match would approve but
another account already holds an APPROVED KYC with the same ID number (a
cropped card portrait passes face match). These tests hold the operator side:
the queue, the evidence a reviewer sees, and the rules on settling one.

Parked attempts are produced through the real /gverify/kyc/verify flow with
the provider mocked, so the review works on exactly what the engine writes.
"""
import uuid

import pytest
from sqlalchemy import select

from app.gverify import storage as gverify_storage
from app.gverify.models import STATUS_MANUAL_REVIEW, GVerifyVerification
from app.lending.models import AuditLog
from tests.gverify.test_kyc_verify import _body, mock_gverify  # noqa: F401

REJECTED_MESSAGE = "We could not complete your identity verification. Please contact support."


@pytest.fixture
def parked_attempt(client, make_user, mock_gverify):  # noqa: F811
    """An owner verified first; an impostor with the same card is parked."""

    async def _make():
        owner = await make_user(role="INVESTOR")
        impostor = await make_user(role="INVESTOR")
        first = await client.post("/gverify/kyc/verify", json=_body(), headers=owner["headers"])
        assert first.json()["status"] == "APPROVED", first.text
        parked = await client.post("/gverify/kyc/verify", json=_body(), headers=impostor["headers"])
        assert parked.json()["status"] == "MANUAL_REVIEW", parked.text
        return owner, impostor, parked.json()["verification_id"]

    return _make


async def _resolve(client, admin, verification_id, decision, note=None, acknowledged_conflict_ids=None):
    body = {"decision": decision}
    if note is not None:
        body["note"] = note
    if acknowledged_conflict_ids is not None:
        body["acknowledged_conflict_ids"] = acknowledged_conflict_ids
    return await client.post(
        f"/admin/kyc-verifications/{verification_id}/resolve",
        json=body,
        headers=admin["headers"],
    )


# --- the queue -----------------------------------------------------------------


async def test_queue_lists_parked_attempts_with_the_conflicting_account(
    client, make_admin, parked_attempt
):
    owner, impostor, verification_id = await parked_attempt()
    admin = await make_admin()

    resp = await client.get("/admin/kyc-verifications", headers=admin["headers"])

    assert resp.status_code == 200, resp.text
    [row] = resp.json()["items"]
    assert row["id"] == verification_id
    assert row["status"] == "MANUAL_REVIEW"
    assert row["user"]["email"] == impostor["email"]
    assert row["person_number"] == "079203001234"
    assert row["face_match_score"] == pytest.approx(0.93)
    [conflict] = row["conflicts"]
    assert conflict["user"]["email"] == owner["email"]
    assert conflict["approved_at"] is not None


async def test_queue_is_oldest_first(client, make_admin, make_user, parked_attempt, mock_gverify):  # noqa: F811
    _, _, first_id = await parked_attempt()
    late = await make_user(role="INVESTOR")
    second = await client.post("/gverify/kyc/verify", json=_body(), headers=late["headers"])
    admin = await make_admin()

    rows = (await client.get("/admin/kyc-verifications", headers=admin["headers"])).json()["items"]

    assert [r["id"] for r in rows] == [first_id, second.json()["verification_id"]]


async def test_queue_can_be_filtered_by_status(client, make_admin, parked_attempt):
    owner, _, _ = await parked_attempt()
    admin = await make_admin()

    rows = (
        await client.get("/admin/kyc-verifications?status=APPROVED", headers=admin["headers"])
    ).json()["items"]

    assert [r["user"]["email"] for r in rows] == [owner["email"]]


async def test_all_lists_the_attempts_that_need_action_first(client, make_admin, parked_attempt):
    _, _, parked_id = await parked_attempt()
    admin = await make_admin()

    page = (
        await client.get("/admin/kyc-verifications?status=ALL", headers=admin["headers"])
    ).json()

    assert page["total"] == 2
    assert [r["status"] for r in page["items"]] == ["MANUAL_REVIEW", "APPROVED"]
    assert page["items"][0]["id"] == parked_id


async def test_the_list_is_paged(client, make_admin, parked_attempt):
    owner, _, _ = await parked_attempt()
    admin = await make_admin()

    second = (
        await client.get(
            "/admin/kyc-verifications?status=ALL&page=2&page_size=1", headers=admin["headers"]
        )
    ).json()

    assert (second["total"], second["total_pages"], second["page"]) == (2, 2, 2)
    assert [r["user"]["email"] for r in second["items"]] == [owner["email"]]


async def test_queue_rejects_an_unknown_status(client, make_admin):
    admin = await make_admin()
    resp = await client.get("/admin/kyc-verifications?status=BOGUS", headers=admin["headers"])
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/admin/kyc-verifications",
        f"/admin/kyc-verifications/{uuid.uuid4()}",
        f"/admin/kyc-verifications/{uuid.uuid4()}/images/portrait",
    ],
)
async def test_non_admins_cannot_read_kyc_reviews(client, make_user, path):
    # These rows hold ID numbers and faces.
    investor = await make_user(role="INVESTOR")
    resp = await client.get(path, headers=investor["headers"])
    assert resp.status_code == 403


async def test_non_admins_cannot_resolve(client, make_user, parked_attempt):
    _, impostor, verification_id = await parked_attempt()
    resp = await _resolve(client, impostor, verification_id, "APPROVED", note="it's me")
    assert resp.status_code == 403


async def test_unknown_verification_is_404(client, make_admin):
    admin = await make_admin()
    resp = await client.get(
        f"/admin/kyc-verifications/{uuid.uuid4()}", headers=admin["headers"]
    )
    assert resp.status_code == 404


# --- settling an attempt -------------------------------------------------------


async def test_approving_with_unacknowledged_conflict_returns_409(client, make_admin, parked_attempt):
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()

    resp = await _resolve(
        client, admin, verification_id, "APPROVED", note="Verified the investor manually"
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ID_CONFLICT_ACK_REQUIRED"
    assert len(resp.json()["detail"]["conflicts"]) == 1

    # Still parked: a refused approval changes nothing.
    row = (await client.get(f"/admin/kyc-verifications/{verification_id}", headers=admin["headers"])).json()
    assert row["status"] == "MANUAL_REVIEW"


async def test_approving_requires_a_note(client, make_admin, parked_attempt):
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()
    conflicts = (
        await client.get(f"/admin/kyc-verifications/{verification_id}", headers=admin["headers"])
    ).json()["conflicts"]
    ack_ids = [c["verification_id"] for c in conflicts]

    # Without note or with short/empty note, schema rejects validation (422)
    for note in (None, "", "   ", "too short"):
        resp = await _resolve(
            client, admin, verification_id, "APPROVED", note=note, acknowledged_conflict_ids=ack_ids
        )
        assert resp.status_code == 422, resp.text

    # Still parked: a refused approval changes nothing.
    row = (await client.get(f"/admin/kyc-verifications/{verification_id}", headers=admin["headers"])).json()
    assert row["status"] == "MANUAL_REVIEW"


async def test_approval_verifies_the_investor(client, make_admin, parked_attempt):
    _, impostor, verification_id = await parked_attempt()
    admin = await make_admin()
    conflicts = (
        await client.get(f"/admin/kyc-verifications/{verification_id}", headers=admin["headers"])
    ).json()["conflicts"]
    ack_ids = [c["verification_id"] for c in conflicts]

    resp = await _resolve(
        client,
        admin,
        verification_id,
        "APPROVED",
        note="Same person, lost access to old account",
        acknowledged_conflict_ids=ack_ids,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED"
    assert resp.json()["rejection_reason"] is None
    status = (await client.get("/gverify/kyc/status", headers=impostor["headers"])).json()
    assert status["is_approved"] is True
    # Out of the queue.
    queue = (await client.get("/admin/kyc-verifications", headers=admin["headers"])).json()
    assert queue["items"] == [] and queue["total"] == 0


async def test_rejection_shows_a_fixed_message_never_the_note(
    client, make_admin, parked_attempt
):
    owner, impostor, verification_id = await parked_attempt()
    admin = await make_admin()
    note = f"Impostor: card belongs to {owner['email']}"

    resp = await _resolve(client, admin, verification_id, "REJECTED", note=note)

    assert resp.status_code == 200, resp.text
    status = (await client.get("/gverify/kyc/status", headers=impostor["headers"])).json()
    assert status["status"] == "REJECTED"
    assert status["rejection_reason"] == REJECTED_MESSAGE
    assert owner["email"] not in str(status)


async def test_rejection_needs_no_note(client, make_admin, parked_attempt):
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()

    resp = await _resolve(client, admin, verification_id, "REJECTED")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "REJECTED"


async def test_a_rejected_investor_can_resubmit_and_is_parked_again(
    client, make_admin, parked_attempt, mock_gverify  # noqa: F811
):
    _, impostor, verification_id = await parked_attempt()
    admin = await make_admin()
    await _resolve(client, admin, verification_id, "REJECTED")

    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=impostor["headers"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "MANUAL_REVIEW"


async def test_only_a_parked_attempt_can_be_resolved(client, make_admin, parked_attempt):
    owner, _, _ = await parked_attempt()
    admin = await make_admin()
    [approved] = (
        await client.get("/admin/kyc-verifications?status=APPROVED", headers=admin["headers"])
    ).json()["items"]

    resp = await _resolve(client, admin, approved["id"], "REJECTED")

    assert resp.status_code == 409, resp.text


async def test_a_resolved_attempt_cannot_be_resolved_again(client, make_admin, parked_attempt):
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()
    await _resolve(client, admin, verification_id, "REJECTED")

    resp = await _resolve(client, admin, verification_id, "APPROVED", note="changed my mind")

    assert resp.status_code == 409


async def test_nobody_can_resolve_their_own_attempt(client, make_admin, db_session):
    admin = await make_admin()
    row = GVerifyVerification(
        user_id=uuid.UUID(admin["id"]), status=STATUS_MANUAL_REVIEW, person_number="001099009999"
    )
    db_session.add(row)
    await db_session.commit()

    resp = await _resolve(client, admin, row.id, "APPROVED", note="trust me")

    assert resp.status_code == 403, resp.text


async def test_resolution_is_audited_with_the_note_and_the_conflict(
    client, make_admin, parked_attempt, db_session
):
    owner, impostor, verification_id = await parked_attempt()
    admin = await make_admin()
    conflict_id = (
        await client.get(f"/admin/kyc-verifications/{verification_id}", headers=admin["headers"])
    ).json()["conflicts"][0]["verification_id"]

    await _resolve(
        client,
        admin,
        verification_id,
        "APPROVED",
        note="Checked by phone and confirmed account transition",
        acknowledged_conflict_ids=[conflict_id],
    )

    resolved_log = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "KYC_MANUAL_REVIEW_RESOLVED")
        )
    ).scalar_one()
    assert str(resolved_log.entity_id) == verification_id
    assert str(resolved_log.actor_id) == admin["id"]
    assert resolved_log.before_state == {"status": "MANUAL_REVIEW"}
    assert resolved_log.after_state["status"] == "APPROVED"
    assert resolved_log.after_state["note"] == "Checked by phone and confirmed account transition"
    assert resolved_log.after_state["user_id"] == impostor["id"]
    assert resolved_log.after_state["conflicting_verification_ids"] == [conflict_id]

    override_log = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "KYC_ID_CONFLICT_OVERRIDDEN")
        )
    ).scalar_one()
    assert str(override_log.entity_id) == verification_id
    assert str(override_log.actor_id) == admin["id"]
    assert override_log.after_state["person_number"] == "079203001234"
    assert override_log.after_state["acknowledged_verification_ids"] == [conflict_id]
    assert override_log.after_state["conflicting_user_ids"] == [owner["id"]]


# --- submitted images ------------------------------------------------------------


async def test_image_url_is_presigned_when_stored(client, make_admin, parked_attempt, monkeypatch):
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()
    seen = {}

    def fake_find(vid, name):
        seen["args"] = (str(vid), name)
        return f"verification/KYC/{vid}/{name}.jpg", "image/jpeg"

    monkeypatch.setattr(gverify_storage, "find_kyc_document", fake_find)
    monkeypatch.setattr(gverify_storage, "presign_kyc_document", lambda key: f"https://r2.test/{key}?sig=x")

    resp = await client.get(
        f"/admin/kyc-verifications/{verification_id}/images/portrait", headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    assert seen["args"] == (verification_id, "portrait")
    assert resp.json() == {
        "url": f"https://r2.test/verification/KYC/{verification_id}/portrait.jpg?sig=x",
        "expires_in": 600,
        "content_type": "image/jpeg",
    }


async def test_image_url_is_404_when_nothing_was_stored(
    client, make_admin, parked_attempt, monkeypatch
):
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()
    monkeypatch.setattr(gverify_storage, "find_kyc_document", lambda vid, name: None)

    resp = await client.get(
        f"/admin/kyc-verifications/{verification_id}/images/id_front", headers=admin["headers"]
    )

    assert resp.status_code == 404
    assert resp.json() == {"detail": "This image was not stored"}


async def test_image_name_must_be_one_of_the_three(client, make_admin, parked_attempt):
    # No path tricks: the name is an allowlist, not part of an arbitrary key.
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()

    resp = await client.get(
        f"/admin/kyc-verifications/{verification_id}/images/..%2F..%2Fsecret",
        headers=admin["headers"],
    )

    assert resp.status_code in (404, 422)


def test_find_kyc_document_ignores_unknown_names(monkeypatch):
    monkeypatch.setattr(gverify_storage.r2, "is_configured", lambda: True)
    monkeypatch.setattr(
        gverify_storage.r2, "head_object", lambda key, bucket=None: pytest.fail(f"probed {key}")
    )
    assert gverify_storage.find_kyc_document(uuid.uuid4(), "../../secret") is None


async def test_approving_with_partial_or_wrong_acknowledgement_fails(
    client, make_admin, parked_attempt
):
    _, _, verification_id = await parked_attempt()
    admin = await make_admin()
    random_id = str(uuid.uuid4())

    resp = await _resolve(
        client,
        admin,
        verification_id,
        "APPROVED",
        note="Valid explanation for overriding conflict",
        acknowledged_conflict_ids=[random_id],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ID_CONFLICT_ACK_REQUIRED"


async def test_conflicts_evaluated_is_false_when_no_id_number(client, make_admin, make_user, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.put("/system/verification-mode", json={"mode": "MANUAL"}, headers=sysadmin["headers"])
    assert resp.status_code == 200

    investor = await make_user(role="INVESTOR")
    attempt = (await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])).json()
    admin = await make_admin()

    row = (await client.get(f"/admin/kyc-verifications/{attempt['verification_id']}", headers=admin["headers"])).json()
    assert row["conflicts_evaluated"] is False
    assert row["person_number"] is None

