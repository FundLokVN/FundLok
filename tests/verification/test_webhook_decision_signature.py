"""A Didit decision is stored only when a signature covers it.

Didit signs each webhook three ways. X-Signature-V2 and X-Signature cover the
whole body; X-Signature-Simple covers only "{timestamp}:{session_id}:{status}:
{webhook_type}". The webhook accepted any of the three, then stored the body's
`decision` regardless, so a delivery captured in flight could be replayed
within the 300s window with a decision of the sender's choosing (vbsec
2026-10-16 hardening note).

These tests pin the fix:
  * a body-signed delivery stores its decision as before;
  * an envelope-signed delivery stores its signed status, never its decision:
    the decision is fetched from Didit's API instead;
  * if that fetch fails the signed status still lands and Didit gets its 200.
"""
import hashlib
import hmac
import json
import time
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.verification import service
from app.verification.client import DiditError
from app.verification.models import Verification

SECRET = "test-didit-webhook-secret"
FORGED = {"status": "Approved", "id_verification": {"status": "Approved"}, "forged": True}
FROM_DIDIT = {"status": "Approved", "id_verification": {"status": "Approved"}, "source": "api"}


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "DIDIT_WEBHOOK_SECRET", SECRET)


@pytest.fixture
def didit_api(monkeypatch):
    """Stands in for GET /v3/session/{id}/decision/ and records each call."""
    calls: list[str] = []

    def _retrieve(session_id):
        calls.append(session_id)
        return FROM_DIDIT

    monkeypatch.setattr("app.verification.client.retrieve_decision", _retrieve)
    return calls


@pytest.fixture
async def verification(make_user, db_session):
    user = await make_user(role="INVESTOR")
    row = Verification(
        user_id=user["id"],
        verification_type="KYC",
        session_id=f"sess-{uuid.uuid4().hex}",
        status="In Progress",
    )
    db_session.add(row)
    await db_session.commit()
    return row


def _payload(session_id: str, *, decision: dict | None = FORGED) -> dict:
    body = {
        "event_id": str(uuid.uuid4()),
        "session_id": session_id,
        "status": "Approved",
        "webhook_type": "status.updated",
    }
    if decision is not None:
        body["decision"] = decision
    return body


def _hmac(message: bytes) -> str:
    return hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()


def _signed(payload: dict, *, v2: bool = True, raw: bool = True, simple: bool = True):
    """The body and headers of a delivery carrying the chosen signatures."""
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Timestamp": timestamp}
    if v2:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        headers["X-Signature-V2"] = _hmac(canonical.encode())
    if raw:
        headers["X-Signature"] = _hmac(body)
    if simple:
        envelope = (
            f"{timestamp}:{payload['session_id']}:{payload['status']}:{payload['webhook_type']}"
        )
        headers["X-Signature-Simple"] = _hmac(envelope.encode())
    return body, headers


async def _deliver(client, body, headers):
    return await client.post("/kyc/webhook", content=body, headers=headers)


async def _reload(db_session, row) -> Verification:
    row_id = row.id  # read before expiring: a lazy load cannot run under asyncio
    db_session.expire_all()
    return (
        await db_session.execute(select(Verification).where(Verification.id == row_id))
    ).scalar_one()


# --- which signature verified ---------------------------------------------------


@pytest.mark.parametrize(
    ("signatures", "expected"),
    [
        ({"v2": True, "raw": True, "simple": True}, service.SIGNED_BODY),
        ({"v2": True, "raw": False, "simple": False}, service.SIGNED_BODY),
        ({"v2": False, "raw": True, "simple": False}, service.SIGNED_BODY),
        ({"v2": False, "raw": False, "simple": True}, service.SIGNED_ENVELOPE),
        ({"v2": False, "raw": False, "simple": False}, None),
    ],
)
def test_verify_webhook_reports_how_much_the_signature_covers(signatures, expected):
    body, headers = _signed(_payload("sess-x"), **signatures)

    assert service.verify_webhook(body, headers) == expected


def test_an_envelope_signature_does_not_vouch_for_a_tampered_decision():
    # The simple signature still matches after the decision is swapped, which
    # is the whole problem: it says nothing about that field.
    original = _payload("sess-x", decision={"status": "Declined"})
    _, headers = _signed(original)
    tampered = json.dumps({**original, "decision": FORGED}).encode()

    assert service.verify_webhook(tampered, headers) == service.SIGNED_ENVELOPE


# --- what gets stored -----------------------------------------------------------


async def test_a_body_signed_delivery_stores_its_decision(
    client, verification, db_session, didit_api
):
    body, headers = _signed(_payload(verification.session_id, decision=FROM_DIDIT))

    resp = await _deliver(client, body, headers)

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, verification)
    assert row.status == "Approved"
    assert row.decision == FROM_DIDIT
    assert didit_api == []


async def test_an_envelope_signed_delivery_takes_the_decision_from_didit(
    client, verification, db_session, didit_api
):
    body, headers = _signed(_payload(verification.session_id), v2=False, raw=False)

    resp = await _deliver(client, body, headers)

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, verification)
    assert row.status == "Approved"
    assert row.decision == FROM_DIDIT
    assert didit_api == [verification.session_id]


async def test_an_envelope_signed_delivery_keeps_its_status_when_didit_is_unreachable(
    client, verification, db_session, monkeypatch
):
    def _down(session_id):
        raise DiditError("Could not reach Didit")

    monkeypatch.setattr("app.verification.client.retrieve_decision", _down)
    body, headers = _signed(_payload(verification.session_id), v2=False, raw=False)

    resp = await _deliver(client, body, headers)

    assert resp.status_code == 200, resp.text
    row = await _reload(db_session, verification)
    assert row.status == "Approved"
    assert row.decision is None


async def test_an_envelope_signed_delivery_without_a_decision_does_not_call_didit(
    client, verification, db_session, didit_api
):
    body, headers = _signed(
        _payload(verification.session_id, decision=None), v2=False, raw=False
    )

    resp = await _deliver(client, body, headers)

    assert resp.status_code == 200, resp.text
    assert (await _reload(db_session, verification)).status == "Approved"
    assert didit_api == []


async def test_an_unsigned_delivery_is_refused(client, verification, db_session, didit_api):
    body, headers = _signed(
        _payload(verification.session_id), v2=False, raw=False, simple=False
    )

    resp = await _deliver(client, body, headers)

    assert resp.status_code == 401
    row = await _reload(db_session, verification)
    assert row.status == "In Progress"
    assert row.decision is None
