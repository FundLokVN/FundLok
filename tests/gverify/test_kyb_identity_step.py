"""KYB with the SME's own identity check (KYC) as a step.

The KYB screen now asks the SME to verify their own identity (the same GVerify
KYC investors use: ID card front/back + selfie, optionally via the phone QR
handoff) right after the certificate upload. With GVERIFY_KYB_REQUIRE_REP_MATCH
on, the verified CCCD must be one of the certificate's legal representatives.

Before this, the KYC endpoints accepted INVESTOR only, so with the flag on an
SME could never pass KYB: every attempt spent a billable OCR call and was then
rejected with "complete KYC first".
"""
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.gverify.models import STATUS_MANUAL_REVIEW, GVerifyVerification
from tests.gverify.test_kyb_verify import _body as kyb_body, _ocr_data as kyb_ocr  # noqa: F401
from tests.gverify.test_kyb_verify import make_sme, mock_gverify_kyb  # noqa: F401
from tests.gverify.test_kyc_verify import _body as kyc_body, _ocr_data as kyc_ocr  # noqa: F401
from tests.gverify.test_kyc_verify import mock_gverify  # noqa: F401

# The mock certificate lists one legal representative with this CCCD, and the
# mock KYC OCR reads the same number by default.
REPRESENTATIVE_CCCD = "079203001234"


@pytest.fixture
def rep_match_on(monkeypatch):
    monkeypatch.setattr(settings, "GVERIFY_KYB_REQUIRE_REP_MATCH", True)


async def _kyc(client, user):
    return await client.post("/gverify/kyc/verify", json=kyc_body(), headers=user["headers"])


async def _kyb(client, user):
    return await client.post("/gverify/kyb/verify", json=kyb_body(), headers=user["headers"])


# --- SMEs can do the identity step -------------------------------------------------


async def test_an_sme_can_verify_their_identity(client, make_sme, mock_gverify):  # noqa: F811
    sme = await make_sme()

    resp = await _kyc(client, sme)

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "APPROVED"
    status = await client.get("/gverify/kyc/status", headers=sme["headers"])
    assert status.status_code == 200
    assert status.json()["person_number"] == REPRESENTATIVE_CCCD


async def test_an_sme_can_use_the_phone_handoff(client, make_sme, mock_gverify):  # noqa: F811
    sme = await make_sme()
    handoff = await client.post("/gverify/kyc/handoff", headers=sme["headers"])
    assert handoff.status_code == 201, handoff.text

    client.cookies.clear()
    resp = await client.post(
        "/gverify/kyc/handoff/verify",
        json=kyc_body(),
        headers={"Authorization": f"Bearer {handoff.json()['token']}"},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "APPROVED"


# --- KYB with the representative check on -------------------------------------------


async def test_kyb_without_identity_is_refused_before_any_provider_call(
    client, make_sme, mock_gverify_kyb, rep_match_on  # noqa: F811
):
    sme = await make_sme()

    resp = await _kyb(client, sme)

    assert resp.status_code == 409
    assert resp.headers["x-error-code"] == "KYC_REQUIRED"
    assert (mock_gverify_kyb.ocr_calls, mock_gverify_kyb.tax_calls) == (0, 0)


async def test_kyb_approves_when_the_verified_person_is_a_legal_representative(
    client, make_sme, mock_gverify, mock_gverify_kyb, rep_match_on  # noqa: F811
):
    sme = await make_sme()
    assert (await _kyc(client, sme)).json()["status"] == "APPROVED"

    resp = await _kyb(client, sme)

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "APPROVED"
    assert resp.json()["rejection_code"] is None


async def test_kyb_rejects_when_the_verified_person_is_not_a_representative(
    client, make_sme, mock_gverify, mock_gverify_kyb, rep_match_on  # noqa: F811
):
    sme = await make_sme()
    mock_gverify.ocr = kyc_ocr(person_number="001099009999")  # someone else's card
    assert (await _kyc(client, sme)).json()["status"] == "APPROVED"

    resp = await _kyb(client, sme)

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert data["rejection_code"] == "NOT_A_REPRESENTATIVE"
    # The status endpoint carries the code too, for the result screen.
    status = (await client.get("/gverify/kyb/status", headers=sme["headers"])).json()
    assert status["rejection_code"] == "NOT_A_REPRESENTATIVE"


async def test_the_cccd_comparison_ignores_spacing(
    client, make_sme, mock_gverify, mock_gverify_kyb, rep_match_on  # noqa: F811
):
    sme = await make_sme()
    mock_gverify.ocr = kyc_ocr(person_number="0792 0300 1234")
    await _kyc(client, sme)

    assert (await _kyb(client, sme)).json()["status"] == "APPROVED"


async def test_an_identity_still_under_review_does_not_count(
    client, make_sme, mock_gverify_kyb, rep_match_on, db_session  # noqa: F811
):
    import uuid

    sme = await make_sme()
    db_session.add(
        GVerifyVerification(
            user_id=uuid.UUID(sme["id"]), status=STATUS_MANUAL_REVIEW, person_number=REPRESENTATIVE_CCCD
        )
    )
    await db_session.commit()

    resp = await _kyb(client, sme)

    assert resp.status_code == 409
    assert mock_gverify_kyb.ocr_calls == 0


# --- flag off: unchanged ---------------------------------------------------------


async def test_with_the_flag_off_kyb_is_accepted_without_identity_but_parked(
    client, make_sme, mock_gverify_kyb  # noqa: F811
):
    # Flag off: no identity step is demanded up front (no 409), but without
    # an approved identity matching a representative the attempt is parked
    # for an operator instead of approved automatically.
    sme = await make_sme()

    resp = await _kyb(client, sme)

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "MANUAL_REVIEW"


async def test_a_plain_rejection_has_no_code(client, make_sme, mock_gverify_kyb):  # noqa: F811
    from tests.gverify.test_kyb_verify import _tax_data

    sme = await make_sme()
    mock_gverify_kyb.tax = _tax_data(is_valid=False)

    data = (await _kyb(client, sme)).json()

    assert data["status"] == "REJECTED"
    assert data["rejection_code"] is None


async def test_approved_kyc_person_number_ignores_other_users(
    client, make_sme, mock_gverify, db_session  # noqa: F811
):
    from app.gverify import kyb_service

    owner, other = await make_sme(), await make_sme()
    await _kyc(client, owner)

    import uuid

    assert await kyb_service.approved_kyc_person_number(db_session, uuid.UUID(owner["id"])) == REPRESENTATIVE_CCCD
    assert await kyb_service.approved_kyc_person_number(db_session, uuid.UUID(other["id"])) is None
