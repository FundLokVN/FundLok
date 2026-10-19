"""GVerify eKYC — acceptance tests for docs/specs/gverify/ekyc-kyc-verification.md.

Test names map 1:1 to the spec's acceptance criteria (§8). The GVerify HTTP
client is always mocked (HANDOFF-01 guardrail: no real provider calls).
"""
import base64
import uuid

import pytest

from app.gverify.client import GVerifyError

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

# Minimal bytes that pass the JPEG/PNG magic-byte check.
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fundlok-test-image"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fundlok-test-image"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _body(**overrides) -> dict:
    body = {
        "id_front_b64": _b64(_JPEG_BYTES),
        "id_back_b64": _b64(_JPEG_BYTES),
        "portrait_b64": _b64(_PNG_BYTES),
    }
    body.update(overrides)
    return body


def _ocr_data(**overrides) -> dict:
    data = {
        "transaction_code": f"TX-OCR-{uuid.uuid4().hex[:8]}",
        "person_number": "079203001234",
        "full_name": "NGUYEN VAN A",
        "date_of_birth": "01/01/1990",
        "front_valid": "true",
        "back_valid": "true",
        "person_number_confidence": "0.99",
        "full_name_confidence": "0.98",
    }
    data.update(overrides)
    return data


def _face_data(**overrides) -> dict:
    # Live TPVDEMO shape: match mirrors is_matching as "1"/"0"; matching is
    # the similarity percentage.
    data = {
        "transaction_code": f"TX-FACE-{uuid.uuid4().hex[:8]}",
        "is_matching": True,
        "match": "1",
        "matching": "93.0",
    }
    data.update(overrides)
    return data


@pytest.fixture
def mock_gverify(monkeypatch):
    """Replace the GVerify client calls with configurable fakes.

    Returns a controller object: set `.ocr` / `.face` to a dict (returned) or
    an Exception (raised); `.ocr_calls` / `.face_calls` count invocations.
    """

    class _Controller:
        def __init__(self):
            self.ocr = _ocr_data()
            self.face = _face_data()
            self.ocr_calls = 0
            self.face_calls = 0

    ctl = _Controller()

    async def fake_ocr(*, img_front_b64, img_back_b64):
        ctl.ocr_calls += 1
        if isinstance(ctl.ocr, Exception):
            raise ctl.ocr
        return ctl.ocr

    async def fake_face(*, img1_b64, img2_b64):
        ctl.face_calls += 1
        if isinstance(ctl.face, Exception):
            raise ctl.face
        return ctl.face

    monkeypatch.setattr("app.gverify.client.verify_ocr_id", fake_ocr)
    monkeypatch.setattr("app.gverify.client.face_match", fake_face)
    return ctl


@pytest.fixture
def make_investor(make_user):
    async def _make():
        return await make_user(role="INVESTOR")

    return _make


# --------------------------------------------------------------------------- #
# POST /gverify/kyc/verify
# --------------------------------------------------------------------------- #


async def test_kyc_verify_happy_path_approves(client, make_investor, mock_gverify):
    investor = await make_investor()
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "APPROVED"
    assert data["is_approved"] is True
    assert data["rejection_reason"] is None
    assert data["person_number"] == "079203001234"
    assert data["full_name"] == "NGUYEN VAN A"
    assert data["date_of_birth"] == "01/01/1990"
    assert data["face_match_score"] == pytest.approx(0.93)
    assert mock_gverify.ocr_calls == 1
    assert mock_gverify.face_calls == 1


async def test_kyc_verify_rejects_invalid_id_card(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.ocr = _ocr_data(front_valid="false", front_invalid_message="blurry image")
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert data["is_approved"] is False
    assert "front not valid" in data["rejection_reason"]
    # Fail fast: the billable face-match call must be skipped.
    assert mock_gverify.face_calls == 0


async def test_kyc_verify_rejects_low_ocr_confidence(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.ocr = _ocr_data(person_number_confidence="0.42")
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert "confidence too low" in data["rejection_reason"]
    assert mock_gverify.face_calls == 0


async def test_kyc_verify_rejects_face_mismatch(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.face = _face_data(is_matching=False, match="0.12")
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert data["rejection_reason"] == "Portrait does not match the ID card photo"
    # OCR identity is still persisted on a face rejection.
    assert data["person_number"] == "079203001234"


async def test_kyc_verify_ignores_biometric_warning_when_matched(
    client, make_investor, mock_gverify
):
    """Per Datatrust guidance: comparing the selfie against the ID-card FRONT
    naturally trips the anti-spoof warning (the input is a photo of a
    document), so invalid_code/invalid_message are advisory only — a matched
    face must approve regardless of the warning."""
    investor = await make_investor()
    mock_gverify.face = _face_data(
        is_matching=True,
        match="0.91",
        invalid_code=4,
        invalid_message="Identity document image is either generated by AI or captured on a screen",
    )
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "APPROVED"
    assert data["rejection_reason"] is None


async def test_kyc_verify_warning_does_not_mask_mismatch(client, make_investor, mock_gverify):
    """The warning is ignored, but a genuine mismatch still rejects."""
    investor = await make_investor()
    mock_gverify.face = _face_data(
        is_matching=False,
        match="-1",
        invalid_code=4,
        invalid_message="Identity document image is either generated by AI or captured on a screen",
    )
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert data["rejection_reason"] == "Portrait does not match the ID card photo"


async def test_kyc_verify_approves_on_is_matching_regardless_of_percentage(
    client, make_investor, mock_gverify
):
    """The verdict follows the provider's is_matching flag alone — we do not
    re-threshold the `matching` percentage ourselves."""
    investor = await make_investor()
    mock_gverify.face = _face_data(is_matching=True, match="1", matching="72.5")
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "APPROVED"
    assert data["face_match_score"] == pytest.approx(0.725)


async def test_kyc_verify_conflict_when_already_approved(client, make_investor, mock_gverify):
    investor = await make_investor()
    first = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert first.status_code == 201 and first.json()["status"] == "APPROVED"

    second = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert second.status_code == 409
    # No new provider calls were made for the rejected duplicate.
    assert mock_gverify.ocr_calls == 1


async def test_kyc_verify_allows_retry_after_rejection(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.face = _face_data(is_matching=False)
    first = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert first.status_code == 201 and first.json()["status"] == "REJECTED"

    mock_gverify.face = _face_data()  # matching again
    second = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert second.status_code == 201, second.text
    assert second.json()["status"] == "APPROVED"
    assert second.json()["verification_id"] != first.json()["verification_id"]


async def test_kyc_verify_rejects_unauthorized_role(client, make_user, mock_gverify):
    # SMEs now verify here too (as a step of KYB); a roleless account cannot.
    roleless = await make_user()
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=roleless["headers"])
    assert resp.status_code == 403
    assert mock_gverify.ocr_calls == 0


async def test_kyc_verify_rejects_bad_image(client, make_investor, mock_gverify):
    investor = await make_investor()
    # Valid base64 but not a JPEG/PNG payload.
    resp = await client.post(
        "/gverify/kyc/verify",
        json=_body(id_front_b64=_b64(b"just some text, not an image")),
        headers=investor["headers"],
    )
    assert resp.status_code == 400
    assert "JPEG or PNG" in resp.json()["detail"]
    # Not valid base64 at all.
    resp = await client.post(
        "/gverify/kyc/verify",
        json=_body(portrait_b64="!!!not-base64!!!"),
        headers=investor["headers"],
    )
    assert resp.status_code == 400
    assert mock_gverify.ocr_calls == 0


async def test_kyc_verify_rejects_oversized_image(client, make_investor, mock_gverify):
    investor = await make_investor()
    oversized = _JPEG_BYTES + b"\x00" * (10 * 1024 * 1024)
    resp = await client.post(
        "/gverify/kyc/verify",
        json=_body(id_back_b64=_b64(oversized)),
        headers=investor["headers"],
    )
    assert resp.status_code == 400
    assert "10MB" in resp.json()["detail"]
    assert mock_gverify.ocr_calls == 0


async def test_kyc_verify_provider_error_returns_502_and_marks_failed(
    client, make_investor, mock_gverify
):
    investor = await make_investor()
    mock_gverify.ocr = GVerifyError("GVerify /ekyc/api/base64/verify-ocrid returned an error: unknown")
    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert resp.status_code == 502

    # The FAILED attempt is recorded and visible via /status.
    status = await client.get("/gverify/kyc/status", headers=investor["headers"])
    assert status.status_code == 200
    assert status.json()["status"] == "FAILED"
    assert status.json()["is_terminal"] is True


# --------------------------------------------------------------------------- #
# Phone handoff: POST /gverify/kyc/handoff + /gverify/kyc/handoff/verify
# --------------------------------------------------------------------------- #


async def test_kyc_handoff_token_flow_verifies(client, make_investor, mock_gverify):
    investor = await make_investor()
    handoff = await client.post("/gverify/kyc/handoff", headers=investor["headers"])
    assert handoff.status_code == 201, handoff.text
    token = handoff.json()["token"]
    assert handoff.json()["expires_in_seconds"] == 600

    # The "phone": no session cookie, only the handoff token.
    resp = await client.post(
        "/gverify/kyc/handoff/verify",
        json=_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "APPROVED"

    # The desktop session sees the verdict via its own status endpoint.
    status = await client.get("/gverify/kyc/status", headers=investor["headers"])
    assert status.status_code == 200
    assert status.json()["status"] == "APPROVED"


async def test_kyc_handoff_requires_a_verifying_role(client, make_user):
    roleless = await make_user()
    resp = await client.post("/gverify/kyc/handoff", headers=roleless["headers"])
    assert resp.status_code == 403


async def test_kyc_handoff_verify_rejects_bad_token(client, mock_gverify):
    resp = await client.post(
        "/gverify/kyc/handoff/verify",
        json=_body(),
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401
    assert mock_gverify.ocr_calls == 0

    # Missing header entirely.
    resp = await client.post("/gverify/kyc/handoff/verify", json=_body())
    assert resp.status_code == 401


async def test_kyc_handoff_verify_rejects_wrong_purpose_token(
    client, make_investor, mock_gverify
):
    from app.utils.jwt import create_verification_token

    investor = await make_investor()
    # A valid JWT for the same user but minted for email verification — the
    # purpose claim must not be interchangeable.
    wrong_purpose = create_verification_token(investor["id"])
    resp = await client.post(
        "/gverify/kyc/handoff/verify",
        json=_body(),
        headers={"Authorization": f"Bearer {wrong_purpose}"},
    )
    assert resp.status_code == 401
    assert mock_gverify.ocr_calls == 0


async def test_kyc_handoff_verify_conflict_when_already_approved(
    client, make_investor, mock_gverify
):
    investor = await make_investor()
    first = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    assert first.status_code == 201 and first.json()["status"] == "APPROVED"

    handoff = await client.post("/gverify/kyc/handoff", headers=investor["headers"])
    token = handoff.json()["token"]
    resp = await client.post(
        "/gverify/kyc/handoff/verify",
        json=_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# GET /gverify/kyc/status
# --------------------------------------------------------------------------- #


async def test_kyc_status_returns_latest_attempt(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.face = _face_data(is_matching=False)
    await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])
    mock_gverify.face = _face_data()
    second = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])

    resp = await client.get("/gverify/kyc/status", headers=investor["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["verification_id"] == second.json()["verification_id"]
    assert data["status"] == "APPROVED"
    assert data["is_approved"] is True


async def test_kyc_status_not_started_when_never_attempted(client, make_investor):
    investor = await make_investor()
    resp = await client.get("/gverify/kyc/status", headers=investor["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["verification_id"] is None
    assert data["status"] == "NOT_STARTED"
    assert data["is_terminal"] is False
    assert data["is_approved"] is False



# --------------------------------------------------------------------------- #
# One CCCD, one verified account (vbsec [9])
#
# Face match can't tell a real selfie from the card's own portrait cropped and
# resubmitted, so anyone holding a photo of someone else's ID could verify a
# second account under that identity. A CCCD number already APPROVED on
# another account is parked for manual review instead of auto-approved.
# --------------------------------------------------------------------------- #


async def _verify(client, user):
    return await client.post("/gverify/kyc/verify", json=_body(), headers=user["headers"])


async def test_kyc_same_id_on_a_second_account_goes_to_manual_review(
    client, make_investor, mock_gverify
):
    owner, impostor = await make_investor(), await make_investor()
    assert (await _verify(client, owner)).json()["status"] == "APPROVED"

    resp = await _verify(client, impostor)

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "MANUAL_REVIEW"
    assert data["is_approved"] is False
    assert data["rejection_reason"] == "Your verification needs a manual review"


async def test_kyc_manual_review_reason_does_not_reveal_the_other_account(
    client, make_investor, mock_gverify
):
    # Saying "already verified on another account" would confirm to whoever
    # holds the card photo that its owner has a FundLok account.
    owner, impostor = await make_investor(), await make_investor()
    await _verify(client, owner)

    reason = (await _verify(client, impostor)).json()["rejection_reason"].lower()

    for leak in ("another", "other account", "already", "registered", "duplicate"):
        assert leak not in reason


async def test_kyc_duplicate_check_ignores_ocr_spacing(client, make_investor, mock_gverify):
    owner, impostor = await make_investor(), await make_investor()
    await _verify(client, owner)
    mock_gverify.ocr = _ocr_data(person_number="0792 0300 1234")

    assert (await _verify(client, impostor)).json()["status"] == "MANUAL_REVIEW"


async def test_kyc_different_id_on_a_second_account_still_approves(
    client, make_investor, mock_gverify
):
    first, second = await make_investor(), await make_investor()
    await _verify(client, first)
    mock_gverify.ocr = _ocr_data(person_number="001099009999")

    assert (await _verify(client, second)).json()["status"] == "APPROVED"


@pytest.mark.parametrize("first_outcome", ["REJECTED", "MANUAL_REVIEW"])
async def test_kyc_only_an_approved_attempt_claims_the_id(
    client, make_investor, mock_gverify, db_session, first_outcome
):
    # A rejected (or still-parked) attempt proves nothing about who owns the
    # card, so it must not block the real owner verifying later.
    from app.gverify.models import GVerifyVerification

    squatter, owner = await make_investor(), await make_investor()
    db_session.add(
        GVerifyVerification(
            user_id=squatter["id"], status=first_outcome, person_number="079203001234"
        )
    )
    await db_session.commit()

    assert (await _verify(client, owner)).json()["status"] == "APPROVED"


async def test_kyc_duplicate_id_with_mismatched_face_is_rejected_not_reviewed(
    client, make_investor, mock_gverify
):
    # A failed face match is a plain rejection; review is only for a face
    # match that would otherwise have approved.
    owner, impostor = await make_investor(), await make_investor()
    await _verify(client, owner)
    mock_gverify.face = _face_data(is_matching=False)

    assert (await _verify(client, impostor)).json()["status"] == "REJECTED"


async def test_kyc_cannot_resubmit_while_under_manual_review(
    client, make_investor, mock_gverify
):
    owner, impostor = await make_investor(), await make_investor()
    await _verify(client, owner)
    await _verify(client, impostor)
    calls_before = (mock_gverify.ocr_calls, mock_gverify.face_calls)

    resp = await _verify(client, impostor)

    assert resp.status_code == 409
    assert resp.json() == {"detail": "KYC is under manual review"}
    # Refused before any (billable) provider call.
    assert (mock_gverify.ocr_calls, mock_gverify.face_calls) == calls_before


async def test_kyc_status_reports_manual_review_as_terminal(
    client, make_investor, mock_gverify
):
    owner, impostor = await make_investor(), await make_investor()
    await _verify(client, owner)
    await _verify(client, impostor)

    data = (await client.get("/gverify/kyc/status", headers=impostor["headers"])).json()

    assert data["status"] == "MANUAL_REVIEW"
    assert data["is_terminal"] is True
    assert data["is_approved"] is False


async def test_kyc_manual_review_audit_names_the_cause(
    client, make_investor, mock_gverify, db_session
):
    from sqlalchemy import select

    from app.lending.models import AuditLog

    owner, impostor = await make_investor(), await make_investor()
    await _verify(client, owner)
    verification_id = (await _verify(client, impostor)).json()["verification_id"]

    row = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "GVERIFY_KYC_VERIFICATION",
                AuditLog.entity_id == uuid.UUID(verification_id),
            )
        )
    ).scalar_one()
    assert row.after_state["status"] == "MANUAL_REVIEW"
    assert row.after_state["review_cause"] == "PERSON_NUMBER_APPROVED_ON_ANOTHER_ACCOUNT"


async def test_kyc_502_does_not_leak_the_provider_error(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.ocr = GVerifyError("Could not reach GVerify: [Errno 8] nodename nor servname provided")

    resp = await client.post("/gverify/kyc/verify", json=_body(), headers=investor["headers"])

    assert resp.status_code == 502
    assert "Errno" not in resp.text
    assert resp.json() == {"detail": "The verification provider could not process the request. Please try again."}


# --------------------------------------------------------------------------- #
# Rule 11: a portrait that is the card's own photo is parked, not approved
# --------------------------------------------------------------------------- #
#
# vbsec [9]: with no liveness step, cropping the face off someone else's CCCD
# and submitting it as the "selfie" matches the card perfectly and approved.


async def test_kyc_portrait_scoring_as_the_card_photo_goes_to_manual_review(
    client, make_investor, mock_gverify
):
    investor = await make_investor()
    mock_gverify.face = _face_data(matching="99.6")

    resp = await _verify(client, investor)

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "MANUAL_REVIEW"
    assert data["is_approved"] is False
    # Neutral, like rule 9: it must not teach the trick.
    assert data["rejection_reason"] == "Your verification needs a manual review"


async def test_kyc_card_photo_threshold_is_inclusive(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.face = _face_data(matching="98.0")

    assert (await _verify(client, investor)).json()["status"] == "MANUAL_REVIEW"


async def test_kyc_ordinary_selfie_score_still_approves(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.face = _face_data(matching="97.9")

    assert (await _verify(client, investor)).json()["status"] == "APPROVED"


async def test_kyc_missing_similarity_does_not_block_approval(client, make_investor, mock_gverify):
    investor = await make_investor()
    face = _face_data()
    del face["matching"]
    mock_gverify.face = face

    assert (await _verify(client, investor)).json()["status"] == "APPROVED"


async def test_kyc_card_photo_review_audit_names_the_cause(
    client, make_investor, mock_gverify, db_session
):
    from sqlalchemy import select

    from app.lending.models import AuditLog

    investor = await make_investor()
    mock_gverify.face = _face_data(matching="100")
    verification_id = (await _verify(client, investor)).json()["verification_id"]

    row = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "GVERIFY_KYC_VERIFICATION",
                AuditLog.entity_id == uuid.UUID(verification_id),
            )
        )
    ).scalar_one()
    assert row.after_state["review_cause"] == "PORTRAIT_IS_CARD_PHOTO"


# --------------------------------------------------------------------------- #
# vbsec 2026-10-16: the CCCD check under concurrency, the daily attempt cap,
# and single-use handoff tokens.
# --------------------------------------------------------------------------- #


async def test_kyc_same_id_submitted_at_once_approves_only_one_account(
    client, make_investor, mock_gverify, db_session
):
    # The duplicate check ran under the per-user lock only, so two accounts
    # submitting the same card at once each saw no committed approval and both
    # were APPROVED. Straight at the service, each in its own session (the
    # test client shares one session, so it cannot race), committing like the
    # router does.
    import asyncio

    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.gverify import service
    from app.gverify.models import GVerifyVerification
    from app.users.models import User

    accounts = [await make_investor() for _ in range(3)]

    async def submit(account) -> None:
        async with SessionLocal() as session:
            user = await session.get(User, uuid.UUID(account["id"]))
            await service.run_kyc_verification(
                session,
                user,
                id_front_b64=_body()["id_front_b64"],
                id_back_b64=_body()["id_back_b64"],
                portrait_b64=_body()["portrait_b64"],
            )
            await session.commit()

    await asyncio.gather(*[submit(a) for a in accounts])

    statuses = sorted(
        (await db_session.execute(select(GVerifyVerification.status))).scalars().all()
    )
    assert statuses == ["APPROVED", "MANUAL_REVIEW", "MANUAL_REVIEW"]


async def test_kyc_attempts_are_capped_per_day(client, make_investor, mock_gverify, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "GVERIFY_MAX_ATTEMPTS_PER_DAY", 2)
    investor = await make_investor()
    mock_gverify.face = _face_data(is_matching=False)
    for _ in range(2):
        assert (await _verify(client, investor)).json()["status"] == "REJECTED"

    resp = await _verify(client, investor)

    assert resp.status_code == 429
    assert "24 hours" in resp.json()["detail"]
    # Refused before the provider is called: the cap exists to stop billing.
    assert mock_gverify.ocr_calls == 2


async def test_kyc_cap_counts_failed_attempts(client, make_investor, mock_gverify, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "GVERIFY_MAX_ATTEMPTS_PER_DAY", 1)
    investor = await make_investor()
    mock_gverify.ocr = GVerifyError("provider down")
    assert (await _verify(client, investor)).status_code == 502

    assert (await _verify(client, investor)).status_code == 429


async def test_kyc_cap_is_a_rolling_day(
    client, make_investor, mock_gverify, monkeypatch, db_session
):
    from datetime import timedelta

    from sqlalchemy import update

    from app.core.config import settings
    from app.gverify.models import GVerifyVerification

    monkeypatch.setattr(settings, "GVERIFY_MAX_ATTEMPTS_PER_DAY", 1)
    investor = await make_investor()
    mock_gverify.face = _face_data(is_matching=False)
    await _verify(client, investor)
    await db_session.execute(
        update(GVerifyVerification).values(
            created_at=GVerifyVerification.created_at - timedelta(days=1, seconds=1)
        )
    )
    await db_session.commit()
    mock_gverify.face = _face_data()

    resp = await _verify(client, investor)

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "APPROVED"


async def test_kyc_cap_is_per_user(client, make_investor, mock_gverify, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "GVERIFY_MAX_ATTEMPTS_PER_DAY", 1)
    first, second = await make_investor(), await make_investor()
    mock_gverify.face = _face_data(is_matching=False)
    await _verify(client, first)

    assert (await _verify(client, second)).status_code == 201


async def _handoff_token(client, user) -> str:
    resp = await client.post("/gverify/kyc/handoff", headers=user["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def _phone(client, token: str, **body):
    return await client.post(
        "/gverify/kyc/handoff/verify",
        json=_body(**body),
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_kyc_handoff_token_is_spent_by_its_first_attempt(
    client, make_investor, mock_gverify
):
    investor = await make_investor()
    token = await _handoff_token(client, investor)
    mock_gverify.face = _face_data(is_matching=False)
    assert (await _phone(client, token)).json()["status"] == "REJECTED"

    again = await _phone(client, token)

    assert again.status_code == 401
    assert again.json() == {"detail": "Invalid or expired handoff token"}
    assert mock_gverify.ocr_calls == 1


async def test_kyc_handoff_token_survives_a_submission_that_made_no_attempt(
    client, make_investor, mock_gverify
):
    # A bad image is refused before any attempt exists, so the phone can
    # retake the photo without a new QR.
    investor = await make_investor()
    token = await _handoff_token(client, investor)
    assert (await _phone(client, token, portrait_b64="!!!not-base64!!!")).status_code == 400

    resp = await _phone(client, token)

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "APPROVED"


async def test_kyc_handoff_token_is_spent_by_a_desktop_attempt_too(
    client, make_investor, mock_gverify
):
    investor = await make_investor()
    token = await _handoff_token(client, investor)
    mock_gverify.face = _face_data(is_matching=False)
    await _verify(client, investor)

    assert (await _phone(client, token)).status_code == 401


async def test_kyc_new_handoff_token_after_a_rejection_works(client, make_investor, mock_gverify):
    investor = await make_investor()
    mock_gverify.face = _face_data(is_matching=False)
    await _phone(client, await _handoff_token(client, investor))
    mock_gverify.face = _face_data()

    resp = await _phone(client, await _handoff_token(client, investor))

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "APPROVED"


async def test_kyc_handoff_token_used_twice_at_once_makes_one_attempt(
    client, make_investor, mock_gverify
):
    # Straight at the router's shared path, each in its own session, so the
    # two submissions really race for the per-user lock.
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.core.database import SessionLocal
    from app.gverify.router import _run_verification
    from app.gverify.schemas import KycVerifyRequest
    from app.gverify.tokens import verify_handoff_token
    from app.users.models import User

    investor = await make_investor()
    claims = verify_handoff_token(await _handoff_token(client, investor))
    request = Request({"type": "http", "client": ("127.0.0.1", 0), "headers": []})

    async def submit() -> int:
        async with SessionLocal() as session:
            user = await session.get(User, uuid.UUID(investor["id"]))
            try:
                await _run_verification(
                    request, KycVerifyRequest(**_body()), session, user, handoff=claims
                )
            except HTTPException as exc:
                return exc.status_code
            return 201

    codes = sorted(await asyncio.gather(submit(), submit()))

    assert codes == [201, 401]
    assert mock_gverify.ocr_calls == 1


async def test_kyc_handoff_token_without_the_single_use_claim_is_refused(
    client, make_investor, mock_gverify
):
    # Shaped like a token minted before single use existed.
    from datetime import timedelta

    from app.utils.jwt import create_access_token

    investor = await make_investor()
    legacy = create_access_token(
        data={"sub": investor["id"], "purpose": "gverify_kyc_handoff"},
        expires_delta=timedelta(minutes=10),
    )

    assert (await _phone(client, legacy)).status_code == 401
    assert mock_gverify.ocr_calls == 0
