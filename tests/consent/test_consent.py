"""Sign-up acceptance of the Terms & Services and data consent (Contract 1).
Spec: docs/specs/consent/sign-up-terms-consent.md

Users here are made with consent=False: conftest's make_user otherwise records
the acceptance a real sign-up makes, so that the rest of the suite behaves
like users who have consented.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.consent import documents, service
from app.consent.models import ConsentCode, UserConsent
from app.lending.models import AuditLog

STATUS = "/consent/status"
CODE = "/consent/code"
ACCEPT = "/consent/accept"


@pytest.fixture
def outbox(monkeypatch):
    """Codes 'emailed': (address, code)."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(service, "_deliver_code", lambda to, code: sent.append((to, code)))
    return sent


def _accept_body(code: str, **overrides) -> dict:
    body = {
        "version": documents.CURRENT_VERSION,
        "terms": True,
        "data_processing": True,
        "data_sharing_tpv": True,
        "code": code,
    }
    body.update(overrides)
    return body


async def _code(client, user, outbox) -> str:
    resp = await client.post(CODE, headers=user["headers"])
    assert resp.status_code == 200, resp.text
    assert outbox[-1][0] == user["email"]
    return outbox[-1][1]


async def _rows(db_session, user) -> list[UserConsent]:
    return (
        await db_session.execute(
            select(UserConsent)
            .where(UserConsent.user_id == UUID(user["id"]))
            .execution_options(populate_existing=True)
        )
    ).scalars().all()


async def _latest_code(db_session, user) -> ConsentCode:
    return (
        await db_session.execute(
            select(ConsentCode)
            .where(ConsentCode.user_id == UUID(user["id"]))
            .order_by(ConsentCode.created_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# Who is asked
# --------------------------------------------------------------------------- #


async def test_a_new_user_must_consent(client, make_user):
    user = await make_user(consent=False)
    resp = await client.get(STATUS, headers=user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "version": documents.CURRENT_VERSION,
        "required": True,
        "accepted_at": None,
    }


async def test_admins_are_never_asked(client, make_admin, db_session):
    admin = await make_admin()
    await db_session.execute(delete(UserConsent).where(UserConsent.user_id == UUID(admin["id"])))
    await db_session.commit()

    assert (await client.get(STATUS, headers=admin["headers"])).json()["required"] is False
    me = await client.get("/users/me", headers=admin["headers"])
    assert me.json()["consent_required"] is False


async def test_users_me_reports_consent_required(client, make_user, outbox):
    user = await make_user(consent=False)
    assert (await client.get("/users/me", headers=user["headers"])).json()["consent_required"] is True

    code = await _code(client, user, outbox)
    assert (await client.post(ACCEPT, json=_accept_body(code), headers=user["headers"])).status_code == 200
    assert (await client.get("/users/me", headers=user["headers"])).json()["consent_required"] is False


# --------------------------------------------------------------------------- #
# The code
# --------------------------------------------------------------------------- #


async def test_the_code_is_emailed_to_the_account_and_stored_hashed(
    client, make_user, db_session, outbox
):
    user = await make_user(consent=False, email="phat.consent@fundlok.com")
    resp = await client.post(CODE, headers=user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "sent_to": "ph***@fundlok.com",
        "expires_in_seconds": 600,
        "resend_after_seconds": 60,
    }
    [(to, code)] = outbox
    assert to == "phat.consent@fundlok.com"
    assert len(code) == 6 and code.isdigit()
    assert code not in resp.text

    row = await _latest_code(db_session, user)
    assert code not in row.code_hash and len(row.code_hash) == 64
    assert row.consumed_at is None and row.attempts == 0
    assert timedelta(minutes=9) < row.expires_at - datetime.now(timezone.utc) <= timedelta(minutes=10)


async def test_the_code_email_renders_the_code(monkeypatch):
    sent = []
    monkeypatch.setattr("app.utils.email.send_email", lambda **kw: sent.append(kw))
    service._deliver_code("someone@fundlok.com", "482913")
    [mail] = sent
    assert mail["to_email"] == "someone@fundlok.com"
    assert "482913" in mail["html_content"] and "482913" in mail["text_content"]
    assert "10 minutes" in mail["html_content"]


async def test_codes_are_rate_limited(client, make_user, monkeypatch, outbox):
    user = await make_user(consent=False)
    await _code(client, user, outbox)

    again = await client.post(CODE, headers=user["headers"])
    assert again.status_code == 429
    assert again.headers["X-Error-Code"] == "CONSENT_CODE_RATE_LIMITED"

    # Past the cooldown, the daily ceiling still holds.
    monkeypatch.setattr(service, "RESEND_COOLDOWN", timedelta(0))
    monkeypatch.setattr(service, "MAX_CODES_PER_DAY", 2)
    await _code(client, user, outbox)
    third = await client.post(CODE, headers=user["headers"])
    assert third.status_code == 429
    assert third.headers["X-Error-Code"] == "CONSENT_CODE_RATE_LIMITED"
    assert len(outbox) == 2


# --------------------------------------------------------------------------- #
# Accepting
# --------------------------------------------------------------------------- #


async def test_accepting_records_one_row_with_all_three_boxes(
    client, make_user, db_session, outbox
):
    user = await make_user(consent=False)
    code = await _code(client, user, outbox)

    resp = await client.post(
        ACCEPT,
        json=_accept_body(code),
        headers=user["headers"] | {"User-Agent": "consent-test-browser"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["required"] is False and body["accepted_at"] is not None
    assert body["version"] == documents.CURRENT_VERSION

    [row] = await _rows(db_session, user)
    assert row.consents == {"TERMS": True, "DATA_PROCESSING": True, "DATA_SHARING_TPV": True}
    assert (row.version, row.verified_by) == (documents.CURRENT_VERSION, "EMAIL_OTP")
    assert row.user_agent == "consent-test-browser"
    assert row.ip_address

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "CONSENT_ACCEPTED"))
    ).scalar_one()
    assert str(audit.entity_id) == user["id"] and str(audit.actor_id) == user["id"]
    assert audit.after_state["version"] == documents.CURRENT_VERSION
    assert audit.after_state["consents"] == row.consents
    assert code not in json.dumps(audit.after_state)


@pytest.mark.parametrize("unticked", ["TERMS", "DATA_PROCESSING", "DATA_SHARING_TPV"])
async def test_consent_counts_only_when_all_three_are_true(client, make_user, db_session, unticked):
    # A row with one box false, e.g. a later withdrawal of that box
    # (Art. 10.4), is no consent; nor is an earlier full acceptance under it.
    user = await make_user()  # accepted, all three true
    db_session.add(
        UserConsent(
            user_id=UUID(user["id"]),
            version=documents.CURRENT_VERSION,
            consents={doc: doc != unticked for doc in documents.DOCUMENTS},
            verified_by="EMAIL_OTP",
            created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    await db_session.commit()

    assert (await client.get(STATUS, headers=user["headers"])).json()["required"] is True
    assert (await client.get("/users/me", headers=user["headers"])).json()["consent_required"] is True


@pytest.mark.parametrize(
    "consents",
    [
        {"TERMS": True, "DATA_PROCESSING": True},  # a box missing
        {"TERMS": True, "DATA_PROCESSING": True, "DATA_SHARING_TPV": True, "MARKETING": True},  # extra key
        {"TERMS": "true", "DATA_PROCESSING": True, "DATA_SHARING_TPV": True},  # not a boolean
        ["TERMS", "DATA_PROCESSING", "DATA_SHARING_TPV"],  # not an object
    ],
)
async def test_the_database_refuses_a_malformed_consents_row(make_user, db_session, consents):
    user = await make_user(consent=False)
    db_session.add(
        UserConsent(
            user_id=UUID(user["id"]),
            version=documents.CURRENT_VERSION,
            consents=consents,
            verified_by="EMAIL_OTP",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.parametrize("box", ["terms", "data_processing", "data_sharing_tpv"])
async def test_every_box_is_required(client, make_user, db_session, outbox, box):
    user = await make_user(consent=False)
    code = await _code(client, user, outbox)

    unticked = await client.post(ACCEPT, json=_accept_body(code, **{box: False}), headers=user["headers"])
    assert unticked.status_code == 422
    missing = _accept_body(code)
    del missing[box]
    assert (await client.post(ACCEPT, json=missing, headers=user["headers"])).status_code == 422
    assert await _rows(db_session, user) == []


async def test_a_stale_version_is_refused(client, make_user, db_session, outbox):
    user = await make_user(consent=False)
    code = await _code(client, user, outbox)
    resp = await client.post(ACCEPT, json=_accept_body(code, version="tos-v0.2"), headers=user["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "CONSENT_VERSION_CHANGED"
    assert await _rows(db_session, user) == []


async def test_a_wrong_code_is_refused_and_counts_an_attempt(client, make_user, db_session, outbox):
    user = await make_user(consent=False)
    code = await _code(client, user, outbox)
    wrong = "000000" if code != "000000" else "111111"

    resp = await client.post(ACCEPT, json=_accept_body(wrong), headers=user["headers"])
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "CONSENT_CODE_INVALID"
    assert (await _latest_code(db_session, user)).attempts == 1

    for _ in range(service.MAX_ATTEMPTS_PER_CODE - 1):
        await client.post(ACCEPT, json=_accept_body(wrong), headers=user["headers"])
    # Out of tries: even the right code no longer works.
    resp = await client.post(ACCEPT, json=_accept_body(code), headers=user["headers"])
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "CONSENT_CODE_EXPIRED"
    assert await _rows(db_session, user) == []


async def test_a_code_is_single_use_and_expires(client, make_user, db_session, outbox, monkeypatch):
    user = await make_user(consent=False)
    # No code asked for yet.
    resp = await client.post(ACCEPT, json=_accept_body("123456"), headers=user["headers"])
    assert resp.headers["X-Error-Code"] == "CONSENT_CODE_EXPIRED"

    code = await _code(client, user, outbox)
    assert (await client.post(ACCEPT, json=_accept_body(code), headers=user["headers"])).status_code == 200
    reused = await client.post(ACCEPT, json=_accept_body(code), headers=user["headers"])
    assert reused.status_code == 400 and reused.headers["X-Error-Code"] == "CONSENT_CODE_EXPIRED"

    # A code past its 10 minutes.
    monkeypatch.setattr(service, "RESEND_COOLDOWN", timedelta(0))
    code = await _code(client, user, outbox)
    row = await _latest_code(db_session, user)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    expired = await client.post(ACCEPT, json=_accept_body(code), headers=user["headers"])
    assert expired.status_code == 400 and expired.headers["X-Error-Code"] == "CONSENT_CODE_EXPIRED"


async def test_a_new_code_retires_the_previous_one(client, make_user, db_session, outbox, monkeypatch):
    user = await make_user(consent=False)
    monkeypatch.setattr(service, "RESEND_COOLDOWN", timedelta(0))
    first = await _code(client, user, outbox)
    second = await _code(client, user, outbox)
    if first == second:  # one in a million: nothing to tell apart
        return

    old = await client.post(ACCEPT, json=_accept_body(first), headers=user["headers"])
    assert old.status_code == 400
    assert (await client.post(ACCEPT, json=_accept_body(second), headers=user["headers"])).status_code == 200


async def test_a_new_version_asks_again(client, make_user, outbox, monkeypatch):
    user = await make_user(consent=False)
    code = await _code(client, user, outbox)
    await client.post(ACCEPT, json=_accept_body(code), headers=user["headers"])
    assert (await client.get(STATUS, headers=user["headers"])).json()["required"] is False

    monkeypatch.setattr(documents, "CURRENT_VERSION", "tos-v0.4")
    status_now = (await client.get(STATUS, headers=user["headers"])).json()
    assert status_now == {"version": "tos-v0.4", "required": True, "accepted_at": None}


# --------------------------------------------------------------------------- #
# What it gates
# --------------------------------------------------------------------------- #


async def test_choosing_a_role_needs_consent(client, make_user, outbox):
    user = await make_user(consent=False)
    refused = await client.patch("/users/me/role", json={"role": "INVESTOR"}, headers=user["headers"])
    assert refused.status_code == 403
    assert refused.headers["X-Error-Code"] == "CONSENT_REQUIRED"

    code = await _code(client, user, outbox)
    await client.post(ACCEPT, json=_accept_body(code), headers=user["headers"])
    chosen = await client.patch("/users/me/role", json={"role": "INVESTOR"}, headers=user["headers"])
    assert chosen.status_code == 200, chosen.text


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


async def test_ekyc_needs_consent(client, make_user, db_session):
    """Nothing reaches TPV Datatrust (GVerify) before the data-sharing box:
    an investor or SME from before consent existed is refused at the door."""
    jpeg = b"\xff\xd8\xff\xe0" + b"fundlok-test-image"
    png = b"\x89PNG\r\n\x1a\n" + b"fundlok-test-image"
    investor = await make_user(role="INVESTOR")
    sme = await make_user(role="SME")
    for who in (investor, sme):
        await db_session.execute(delete(UserConsent).where(UserConsent.user_id == UUID(who["id"])))
    await db_session.commit()

    kyc = await client.post(
        "/gverify/kyc/verify",
        json={"id_front_b64": _b64(jpeg), "id_back_b64": _b64(jpeg), "portrait_b64": _b64(png)},
        headers=investor["headers"],
    )
    assert kyc.status_code == 403, kyc.text
    assert kyc.headers["X-Error-Code"] == "CONSENT_REQUIRED"

    kyb = await client.post(
        "/gverify/kyb/verify",
        json={"document_b64": _b64(jpeg), "document_type": "COMPANY"},
        headers=sme["headers"],
    )
    assert kyb.status_code == 403, kyb.text
    assert kyb.headers["X-Error-Code"] == "CONSENT_REQUIRED"
