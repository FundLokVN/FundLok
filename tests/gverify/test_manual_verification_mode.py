"""Manual verification mode: a system-admin switch that stops every call to
the KYC/KYB provider and routes submissions to admin review instead.

While the provider is down, investors and SMEs can still submit their ID
card + selfie (KYC) or registration certificate (KYB). The documents are
stored and the attempt waits in MANUAL_REVIEW; an admin reads the documents
and approves or rejects. The provider client is replaced with fakes that fail
the test if they are ever called.
"""
import base64
import uuid

import pytest
from sqlalchemy import func, select

from app.gverify.models import GVerifyKybVerification, GVerifyVerification
from app.gverify.storage import StorageUnavailable
from app.lending.models import AuditLog

_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fundlok-test-image"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fundlok-test-image"
_PDF_BYTES = b"%PDF-1.7\n" + b"fundlok-kyb-test-certificate"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


KYC_BODY = {"id_front_b64": _b64(_JPEG_BYTES), "id_back_b64": _b64(_JPEG_BYTES), "portrait_b64": _b64(_PNG_BYTES)}
KYB_BODY = {"document_b64": _b64(_PDF_BYTES), "document_type": "COMPANY", "tax_code": "0312345678"}


@pytest.fixture
def provider_calls(monkeypatch):
    """Every provider entry point, counting calls. In manual mode the count
    must stay at zero; in automatic mode they return a passing verdict."""
    calls: list[str] = []

    async def ocr(**kw):
        calls.append("verify_ocr_id")
        return {
            "transaction_code": "TX-OCR",
            "person_number": "079203001234",
            "full_name": "NGUYEN VAN A",
            "date_of_birth": "01/01/1990",
            "front_valid": "true",
            "back_valid": "true",
        }

    async def face(**kw):
        calls.append("face_match")
        return {"transaction_code": "TX-FACE", "is_matching": True, "matching": "95.0"}

    async def ocrx(**kw):
        calls.append("ocrx_decode")
        raise AssertionError("ocrx_decode must not run in these tests")

    async def tax(**kw):
        calls.append("taxcode_verify")
        raise AssertionError("taxcode_verify must not run in these tests")

    monkeypatch.setattr("app.gverify.client.verify_ocr_id", ocr)
    monkeypatch.setattr("app.gverify.client.face_match", face)
    monkeypatch.setattr("app.gverify.client.ocrx_decode", ocrx)
    monkeypatch.setattr("app.gverify.client.taxcode_verify", tax)
    return calls


@pytest.fixture
def stored(monkeypatch):
    """Record what is stored and whether storage was asked to be strict."""
    record: list[dict] = []

    async def store_kyc(verification_id, **kw):
        record.append({"kind": "KYC", "id": verification_id, **kw})

    async def store_kyb(verification_id, **kw):
        record.append({"kind": "KYB", "id": verification_id, **kw})

    monkeypatch.setattr("app.gverify.storage.store_kyc_documents", store_kyc)
    monkeypatch.setattr("app.gverify.storage.store_kyb_document", store_kyb)
    return record


@pytest.fixture
def set_mode(client, make_system_admin):
    async def _set(mode: str):
        sysadmin = await make_system_admin()
        resp = await client.put("/system/verification-mode", json={"mode": mode}, headers=sysadmin["headers"])
        assert resp.status_code == 200, resp.text
        return sysadmin

    return _set


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #


async def test_mode_defaults_to_automatic(client, make_user):
    investor = await make_user(role="INVESTOR")
    resp = await client.get("/system/verification-mode", headers=investor["headers"])
    assert resp.status_code == 200
    assert resp.json() == {"mode": "AUTOMATIC"}


async def test_system_admin_switches_mode_and_it_is_audited(client, make_system_admin, db_session):
    sysadmin = await make_system_admin()
    resp = await client.put("/system/verification-mode", json={"mode": "MANUAL"}, headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "MANUAL"
    assert resp.json()["updated_by"] == sysadmin["id"]

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "VERIFICATION_MODE_CHANGED"))
    ).scalar_one()
    assert audit.before_state == {"mode": "AUTOMATIC"}
    assert audit.after_state == {"mode": "MANUAL"}
    assert str(audit.actor_id) == sysadmin["id"]


async def test_only_a_system_admin_can_switch(client, make_admin, make_user):
    admin = await make_admin()
    investor = await make_user(role="INVESTOR")
    for user in (admin, investor):
        resp = await client.put("/system/verification-mode", json={"mode": "MANUAL"}, headers=user["headers"])
        assert resp.status_code == 403
    admin_view = await client.get("/system/verification-mode/admin", headers=admin["headers"])
    assert admin_view.status_code == 403


async def test_mode_requires_sign_in_and_a_known_value(client, make_system_admin):
    assert (await client.get("/system/verification-mode")).status_code == 401
    sysadmin = await make_system_admin()
    resp = await client.put("/system/verification-mode", json={"mode": "OFF"}, headers=sysadmin["headers"])
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# KYC in manual mode
# --------------------------------------------------------------------------- #


async def test_manual_kyc_skips_the_provider_and_waits_for_review(
    client, make_user, set_mode, provider_calls, stored
):
    await set_mode("MANUAL")
    investor = await make_user(role="INVESTOR")
    resp = await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "MANUAL_REVIEW"
    assert resp.json()["is_approved"] is False
    assert provider_calls == []
    assert len(stored) == 1 and stored[0]["kind"] == "KYC" and stored[0]["strict"] is True

    again = await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])
    assert again.status_code == 409


async def test_manual_kyc_is_refused_when_documents_cannot_be_stored(
    client, make_user, set_mode, provider_calls, monkeypatch, db_session
):
    await set_mode("MANUAL")

    async def broken_store(verification_id, **kw):
        raise StorageUnavailable("bucket unreachable")

    monkeypatch.setattr("app.gverify.storage.store_kyc_documents", broken_store)
    investor = await make_user(role="INVESTOR")
    resp = await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])

    assert resp.status_code == 503
    assert "bucket" not in resp.text
    count = (
        await db_session.execute(
            select(func.count()).select_from(GVerifyVerification).where(GVerifyVerification.user_id == investor["id"])
        )
    ).scalar_one()
    assert count == 0
    assert provider_calls == []


async def test_switching_back_to_automatic_uses_the_provider_again(
    client, make_user, set_mode, provider_calls, stored
):
    await set_mode("MANUAL")
    await set_mode("AUTOMATIC")
    investor = await make_user(role="INVESTOR")
    resp = await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])
    assert resp.status_code == 201
    assert resp.json()["status"] == "APPROVED"
    assert provider_calls == ["verify_ocr_id", "face_match"]


async def test_admin_approves_manual_kyc_with_details_from_the_card(
    client, make_user, make_admin, set_mode, provider_calls, stored
):
    await set_mode("MANUAL")
    investor = await make_user(role="INVESTOR")
    attempt = (await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])).json()
    admin = await make_admin()

    queue = await client.get("/admin/kyc-verifications", headers=admin["headers"])
    row = next(r for r in queue.json()["items"] if r["id"] == attempt["verification_id"])
    assert row["provider_checked"] is False
    assert row["person_number"] is None

    url = f"/admin/kyc-verifications/{attempt['verification_id']}/resolve"
    missing = await client.post(url, json={"decision": "APPROVED"}, headers=admin["headers"])
    assert missing.status_code == 400

    approved = await client.post(
        url,
        json={
            "decision": "APPROVED",
            "person_number": "0792 0300 1234",
            "full_name": "NGUYEN VAN A",
            "date_of_birth": "01/01/1990",
        },
        headers=admin["headers"],
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["person_number"] == "079203001234"

    status = await client.get("/gverify/kyc/status", headers=investor["headers"])
    assert status.json()["is_approved"] is True


async def test_manual_kyc_with_an_id_already_verified_elsewhere_needs_a_note(
    client, make_user, make_admin, set_mode, provider_calls, stored, db_session
):
    # Someone else already holds an approved KYC with this ID number.
    other = await make_user(role="INVESTOR")
    other_kyc = GVerifyVerification(
        user_id=uuid.UUID(other["id"]), status="APPROVED", person_number="079203001234", full_name="NGUYEN VAN A"
    )
    db_session.add(other_kyc)
    await db_session.flush()
    other_kyc_id = str(other_kyc.id)
    await db_session.commit()

    await set_mode("MANUAL")
    investor = await make_user(role="INVESTOR")
    attempt = (await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])).json()
    admin = await make_admin()
    url = f"/admin/kyc-verifications/{attempt['verification_id']}/resolve"
    details = {"decision": "APPROVED", "person_number": "079203001234", "full_name": "NGUYEN VAN A"}

    # First attempt without acknowledging the conflict is refused with 409 and conflicts listed
    first_attempt = await client.post(url, json=details, headers=admin["headers"])
    assert first_attempt.status_code == 409
    data = first_attempt.json()["detail"]
    assert data["code"] == "ID_CONFLICT_ACK_REQUIRED"
    assert len(data["conflicts"]) == 1
    conflict_id = data["conflicts"][0]["verification_id"]
    assert conflict_id == other_kyc_id

    # Acknowledging without a sufficient note fails validation (422)
    without_note = await client.post(
        url, json={**details, "acknowledged_conflict_ids": [conflict_id]}, headers=admin["headers"]
    )
    assert without_note.status_code == 422

    # Acknowledging with a substantive note succeeds (200)
    with_note = await client.post(
        url,
        json={
            **details,
            "acknowledged_conflict_ids": [conflict_id],
            "note": "Same person, lost access to the old account",
        },
        headers=admin["headers"],
    )
    assert with_note.status_code == 200, with_note.text


async def test_provider_read_details_cannot_be_overwritten(
    client, make_user, make_admin, provider_calls, stored, db_session
):
    """An attempt the provider flagged keeps the provider's ID details."""
    other = await make_user(role="INVESTOR")
    db_session.add(
        GVerifyVerification(user_id=uuid.UUID(other["id"]), status="APPROVED", person_number="079203001234")
    )
    await db_session.commit()
    investor = await make_user(role="INVESTOR")
    attempt = (await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])).json()
    assert attempt["status"] == "MANUAL_REVIEW"  # automatic mode, duplicate ID

    admin = await make_admin()
    resp = await client.post(
        f"/admin/kyc-verifications/{attempt['verification_id']}/resolve",
        json={"decision": "APPROVED", "note": "ok", "person_number": "001099000001", "full_name": "X"},
        headers=admin["headers"],
    )
    assert resp.status_code == 400


async def test_admin_rejects_manual_kyc_and_the_investor_can_resubmit(
    client, make_user, make_admin, set_mode, provider_calls, stored
):
    await set_mode("MANUAL")
    investor = await make_user(role="INVESTOR")
    attempt = (await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])).json()
    admin = await make_admin()
    resp = await client.post(
        f"/admin/kyc-verifications/{attempt['verification_id']}/resolve",
        json={"decision": "REJECTED", "note": "Photo of the card is blurred"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    retry = await client.post("/gverify/kyc/verify", json=KYC_BODY, headers=investor["headers"])
    assert retry.status_code == 201


# --------------------------------------------------------------------------- #
# KYB in manual mode
# --------------------------------------------------------------------------- #


async def test_manual_kyb_skips_the_provider_and_waits_for_review(
    client, make_user, set_mode, provider_calls, stored
):
    await set_mode("MANUAL")
    sme = await make_user(role="SME")
    resp = await client.post("/gverify/kyb/verify", json=KYB_BODY, headers=sme["headers"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "MANUAL_REVIEW"
    assert resp.json()["tax_code"] == "0312345678"
    assert provider_calls == []
    assert len(stored) == 1 and stored[0]["kind"] == "KYB" and stored[0]["strict"] is True

    again = await client.post("/gverify/kyb/verify", json=KYB_BODY, headers=sme["headers"])
    assert again.status_code == 409


async def test_kyb_queue_lists_manual_attempts_with_review_context(
    client, make_user, make_admin, make_project, set_mode, provider_calls, stored
):
    await set_mode("MANUAL")
    sme = await make_user(role="SME")
    project = await make_project(sme, legal_name="Cong Ty Thu Nghiem")
    attempt = (await client.post("/gverify/kyb/verify", json=KYB_BODY, headers=sme["headers"])).json()
    admin = await make_admin()

    queue = await client.get("/admin/kyb-verifications", headers=admin["headers"])
    assert queue.status_code == 200, queue.text
    row = next(r for r in queue.json()["items"] if r["id"] == attempt["verification_id"])
    assert row["provider_checked"] is False
    assert row["user"]["email"] == sme["email"]
    assert row["projects"] == [project["legal_name"]]
    assert row["document_type"] == "COMPANY"

    detail = await client.get(f"/admin/kyb-verifications/{attempt['verification_id']}", headers=admin["headers"])
    assert detail.status_code == 200


async def test_admin_approves_manual_kyb_with_details_from_the_certificate(
    client, make_user, make_admin, set_mode, provider_calls, stored
):
    await set_mode("MANUAL")
    sme = await make_user(role="SME")
    attempt = (await client.post("/gverify/kyb/verify", json=KYB_BODY, headers=sme["headers"])).json()
    admin = await make_admin()
    url = f"/admin/kyb-verifications/{attempt['verification_id']}/resolve"

    missing = await client.post(url, json={"decision": "APPROVED"}, headers=admin["headers"])
    assert missing.status_code == 400

    approved = await client.post(
        url,
        json={"decision": "APPROVED", "business_name": "CONG TY TNHH THU NGHIEM", "tax_code": "0312345678"},
        headers=admin["headers"],
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["business_name"] == "CONG TY TNHH THU NGHIEM"

    status = await client.get("/gverify/kyb/status", headers=sme["headers"])
    assert status.json()["is_approved"] is True


async def test_kyb_certificate_url_for_admins(client, make_user, make_admin, set_mode, provider_calls, stored, monkeypatch):
    await set_mode("MANUAL")
    sme = await make_user(role="SME")
    attempt = (await client.post("/gverify/kyb/verify", json=KYB_BODY, headers=sme["headers"])).json()
    admin = await make_admin()
    url = f"/admin/kyb-verifications/{attempt['verification_id']}/certificate"

    monkeypatch.setattr("app.gverify.storage.find_kyb_certificate", lambda vid: None)
    assert (await client.get(url, headers=admin["headers"])).status_code == 404

    monkeypatch.setattr(
        "app.gverify.storage.find_kyb_certificate",
        lambda vid: (f"verification/KYB/{vid}/certificate.pdf", "application/pdf"),
    )
    monkeypatch.setattr("app.gverify.storage.presign_kyb_certificate", lambda key: f"https://r2.example/{key}?sig=1")
    resp = await client.get(url, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["content_type"] == "application/pdf"
    assert resp.json()["expires_in"] == 600


async def test_members_cannot_use_the_kyb_review_endpoints(client, make_user, set_mode, provider_calls, stored):
    await set_mode("MANUAL")
    sme = await make_user(role="SME")
    attempt = (await client.post("/gverify/kyb/verify", json=KYB_BODY, headers=sme["headers"])).json()
    vid = attempt["verification_id"]
    for method, path, body in (
        ("get", "/admin/kyb-verifications", None),
        ("get", f"/admin/kyb-verifications/{vid}", None),
        ("get", f"/admin/kyb-verifications/{vid}/certificate", None),
        ("post", f"/admin/kyb-verifications/{vid}/resolve", {"decision": "APPROVED", "business_name": "X", "tax_code": "0312345678"}),
    ):
        resp = await getattr(client, method)(path, headers=sme["headers"], **({"json": body} if body else {}))
        assert resp.status_code == 403, (path, resp.status_code)

    # And the SME's attempt is still waiting.
    status = await client.get("/gverify/kyb/status", headers=sme["headers"])
    assert status.json()["status"] == "MANUAL_REVIEW"
