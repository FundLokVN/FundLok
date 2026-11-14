"""Phone verification by Zalo ZBS one-time code: the gate and the code.

Spec: docs/specs/users/phone-verification.md. A new account, whether it signed
up with a password or with Google, proves a Vietnamese mobile number before it
may choose a role. These tests pin the gate, the code lifecycle and the send
limits (each send is a paid ZBS message to a number the caller picks), with
Zalo replaced by a recorder at zalo.send_otp. The Zalo client itself, its
token handling and its refusals are pinned in test_zalo.py.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.lending.models import AuditLog
from app.oauth.schema import OAuthProvider
from app.oauth.service import GoogleOAuthProvider
from app.phone_verification import service, zalo
from app.phone_verification.models import PhoneVerificationCode
from app.users.models import User
from app.users.service import user_me_payload
from conftest import record_consent

SEND = "/users/me/phone/send-code"
VERIFY = "/users/me/phone/verify"
PHONE = "0912345678"
CANONICAL = "+84912345678"


@pytest.fixture
def outbox(monkeypatch):
    """Turns the gate on and records every code handed to Zalo."""
    sent: list[dict] = []

    async def _record(to, code, *, tracking_id):
        sent.append({"to": to, "code": code, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_otp", _record)
    monkeypatch.setattr(settings, "PHONE_VERIFICATION_REQUIRED", True)
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "zalo")
    return sent


def _code_from(sent) -> str:
    return sent[-1]["code"]


async def _set(db_session, user_id, **fields) -> None:
    user = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    for name, value in fields.items():
        setattr(user, name, value)
    await db_session.commit()


async def _new_user(make_user, db_session, **fields) -> dict:
    """A registered, signed-in user whose email is verified, as after the link."""
    user = await make_user()
    await _set(db_session, user["id"], email_verified=True, **fields)
    return user


async def _codes(db_session, user_id) -> list[PhoneVerificationCode]:
    db_session.expire_all()
    rows = await db_session.execute(
        select(PhoneVerificationCode)
        .where(PhoneVerificationCode.user_id == user_id)
        .order_by(PhoneVerificationCode.created_at)
    )
    return list(rows.scalars().all())


async def _age_codes(db_session, user_id, by: timedelta) -> None:
    for row in await _codes(db_session, user_id):
        row.created_at -= by
        row.expires_at -= by
    await db_session.commit()


async def _send(client, user, phone=PHONE):
    return await client.post(SEND, json={"phone": phone}, headers=user["headers"])


async def _verify(client, user, code):
    return await client.post(VERIFY, json={"code": code}, headers=user["headers"])


# --- Sending ------------------------------------------------------------------


async def test_send_code_sends_a_code_by_zalo(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)

    resp = await _send(client, user)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"phone": CANONICAL, "channel": "zalo", "expires_in": 300, "resend_in": 60}
    [sent] = outbox
    assert sent["to"] == "84912345678"
    assert len(sent["code"]) == 6 and sent["code"].isascii() and sent["code"].isdigit()
    # Stored as a keyed hash, never the code itself.
    [row] = await _codes(db_session, user["id"])
    assert row.phone == CANONICAL
    assert row.channel == "zalo"
    assert sent["tracking_id"] == row.id.hex
    assert sent["code"] not in row.code_hash


async def test_send_code_requires_sign_in(client, outbox):
    client.cookies.clear()
    resp = await client.post(SEND, json={"phone": PHONE})
    assert resp.status_code == 401
    assert outbox == []


async def test_send_code_requires_verified_email(client, make_user, outbox):
    user = await make_user()  # registered, link not yet clicked

    resp = await _send(client, user)

    assert resp.status_code == 403
    assert outbox == []


@pytest.mark.parametrize(
    "phone",
    ["+14155550123", "02838123456", "0212345678", "091234567", "09123456789", "phone", "０９１２３４５６７８"],
)
async def test_send_code_rejects_non_vietnamese_mobiles(client, make_user, db_session, outbox, phone):
    user = await _new_user(make_user, db_session)

    resp = await _send(client, user, phone)

    assert resp.status_code == 422
    assert outbox == []


@pytest.mark.parametrize("phone", ["0912345678", "+84912345678", "84912345678", "0912 345 678", "091.234.5678"])
async def test_send_code_accepts_every_vietnamese_spelling(client, make_user, db_session, outbox, phone):
    user = await _new_user(make_user, db_session)

    resp = await _send(client, user, phone)

    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == CANONICAL
    assert outbox[-1]["to"] == "84912345678"


async def test_resend_waits_out_the_cooldown(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    assert (await _send(client, user)).status_code == 200

    again = await _send(client, user)
    assert again.status_code == 429
    assert 1 <= int(again.headers["Retry-After"]) <= 60
    assert len(outbox) == 1

    await _age_codes(db_session, user["id"], timedelta(seconds=61))
    assert (await _send(client, user)).status_code == 200
    assert len(outbox) == 2


async def test_daily_limit_per_account(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    for _ in range(service.MAX_SENDS_PER_USER_PER_DAY):
        assert (await _send(client, user)).status_code == 200
        await _age_codes(db_session, user["id"], timedelta(minutes=2))

    refused = await _send(client, user, "0987654321")  # a new number does not reset it

    assert refused.status_code == 429
    assert len(outbox) == service.MAX_SENDS_PER_USER_PER_DAY


async def test_daily_limit_per_number_across_accounts(client, make_user, db_session, outbox):
    for _ in range(service.MAX_SENDS_PER_PHONE_PER_DAY):
        assert (await _send(client, await _new_user(make_user, db_session))).status_code == 200

    refused = await _send(client, await _new_user(make_user, db_session))

    assert refused.status_code == 429
    assert len(outbox) == service.MAX_SENDS_PER_PHONE_PER_DAY


async def test_number_proven_by_another_account_cannot_be_sent_to(client, make_user, db_session, outbox):
    owner = await _new_user(make_user, db_session)
    await _send(client, owner)
    assert (await _verify(client, owner, _code_from(outbox))).status_code == 200

    other = await _new_user(make_user, db_session)
    resp = await _send(client, other, "84912345678")

    assert resp.status_code == 400
    assert resp.json()["detail"] == service.ALREADY_REGISTERED
    assert len(outbox) == 1


async def test_zalo_failure_returns_502_and_is_not_counted(client, make_user, db_session, outbox, monkeypatch):
    user = await _new_user(make_user, db_session)

    async def _refused(to, code, *, tracking_id):
        raise zalo.ZaloError("out of quota", code=-115)

    monkeypatch.setattr(zalo, "send_otp", _refused)
    resp = await _send(client, user)
    assert resp.status_code == 502
    assert await _codes(db_session, user["id"]) == []

    # No cooldown for a code that never arrived.
    async def _ok(to, code, *, tracking_id):
        outbox.append({"to": to, "code": code, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_otp", _ok)
    assert (await _send(client, user)).status_code == 200


@pytest.mark.parametrize("error", sorted(service.NO_ZALO_ERRORS))
async def test_a_number_without_zalo_is_told_so(client, make_user, db_session, outbox, monkeypatch, error):
    user = await _new_user(make_user, db_session)

    async def _no_zalo(to, code, *, tracking_id):
        raise zalo.ZaloError("cannot receive", code=error)

    monkeypatch.setattr(zalo, "send_otp", _no_zalo)
    resp = await _send(client, user)

    assert resp.status_code == 400
    assert resp.json()["detail"] == service.NO_ZALO
    assert await _codes(db_session, user["id"]) == []


async def test_missing_zalo_configuration_returns_503(client, make_user, db_session, outbox, monkeypatch):
    user = await _new_user(make_user, db_session)

    async def _unconfigured(to, code, *, tracking_id):
        raise zalo.ZaloNotConfigured("ZALO_ZBS_OTP_TEMPLATE_ID not configured")

    monkeypatch.setattr(zalo, "send_otp", _unconfigured)
    resp = await _send(client, user)

    assert resp.status_code == 503
    assert await _codes(db_session, user["id"]) == []


async def test_log_channel_only_with_debug(client, make_user, db_session, outbox, monkeypatch):
    user = await _new_user(make_user, db_session)
    monkeypatch.setattr(settings, "PHONE_OTP_CHANNEL", "log")

    monkeypatch.setattr(settings, "DEBUG", False)
    assert (await _send(client, user)).status_code == 503

    monkeypatch.setattr(settings, "DEBUG", True)
    resp = await _send(client, user)
    assert resp.status_code == 200
    assert resp.json()["channel"] == "log"
    assert outbox == []


# --- Verifying ----------------------------------------------------------------


async def test_correct_code_verifies_the_phone(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    await _send(client, user, "0912 345 678")

    resp = await _verify(client, user, _code_from(outbox))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phone"] == CANONICAL
    assert body["phone_verified"] is True
    assert body["phone_verification_required"] is False
    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == user["id"], AuditLog.action == service.PHONE_VERIFIED)
        )
    ).scalar_one()
    assert audit.after_state["phone"] == CANONICAL


async def test_wrong_code_is_refused_and_counted(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    await _send(client, user)
    wrong = "000000" if _code_from(outbox) != "000000" else "111111"

    resp = await _verify(client, user, wrong)

    assert resp.status_code == 400
    assert resp.json()["detail"] == service.CODE_INCORRECT
    [row] = await _codes(db_session, user["id"])
    assert row.attempts == 1


async def test_code_dies_after_five_wrong_attempts(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    await _send(client, user)
    code = _code_from(outbox)
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(service.MAX_ATTEMPTS_PER_CODE - 1):
        assert (await _verify(client, user, wrong)).json()["detail"] == service.CODE_INCORRECT
    assert (await _verify(client, user, wrong)).json()["detail"] == service.TOO_MANY_ATTEMPTS

    resp = await _verify(client, user, code)
    assert resp.status_code == 400
    assert resp.json()["detail"] == service.CODE_EXPIRED


async def test_expired_code_is_refused(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    await _send(client, user)
    await _age_codes(db_session, user["id"], service.CODE_TTL + timedelta(seconds=1))

    resp = await _verify(client, user, _code_from(outbox))

    assert resp.status_code == 400
    assert resp.json()["detail"] == service.CODE_EXPIRED


async def test_only_the_latest_code_counts(client, make_user, db_session, outbox, monkeypatch):
    user = await _new_user(make_user, db_session)
    codes = iter(["111111", "222222"])
    monkeypatch.setattr(service, "_new_code", lambda: next(codes))
    await _send(client, user)
    await _age_codes(db_session, user["id"], timedelta(minutes=2))
    await _send(client, user)

    assert (await _verify(client, user, "111111")).status_code == 400
    assert (await _verify(client, user, "222222")).status_code == 200


async def test_code_cannot_be_used_twice(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    await _send(client, user)
    code = _code_from(outbox)

    assert (await _verify(client, user, code)).status_code == 200
    again = await _verify(client, user, code)
    assert again.status_code == 400
    assert again.json()["detail"] == service.CODE_EXPIRED


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", "١٢٣٤٥٦"])
async def test_malformed_code_is_rejected(client, make_user, db_session, outbox, code):
    user = await _new_user(make_user, db_session)
    await _send(client, user)

    assert (await _verify(client, user, code)).status_code == 422
    [row] = await _codes(db_session, user["id"])
    assert row.attempts == 0


async def test_verifying_releases_the_number_from_an_unverified_holder(client, make_user, db_session, outbox):
    # Registration accepts a number unproven; that must not lock out its owner.
    squatter = await _new_user(make_user, db_session, phone=PHONE)
    owner = await _new_user(make_user, db_session)
    await _send(client, owner)

    resp = await _verify(client, owner, _code_from(outbox))

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    held = (await db_session.execute(select(User).where(User.id == squatter["id"]))).scalar_one()
    assert held.phone is None
    released = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == squatter["id"], AuditLog.action == service.PHONE_RELEASED)
        )
    ).scalar_one()
    assert released.before_state == {"phone": PHONE}


# --- The onboarding gate ------------------------------------------------------


async def test_role_selection_waits_for_phone_verification(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)

    me = (await client.get("/users/me", headers=user["headers"])).json()
    assert me["phone_verified"] is False
    assert me["phone_verification_required"] is True
    refused = await client.patch("/users/me/role", json={"role": "INVESTOR"}, headers=user["headers"])
    assert refused.status_code == 403

    await _send(client, user)
    await _verify(client, user, _code_from(outbox))

    chosen = await client.patch("/users/me/role", json={"role": "INVESTOR"}, headers=user["headers"])
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["role"] == "INVESTOR"


async def test_google_sign_up_is_gated_the_same_way(client, db_session, outbox, monkeypatch):
    monkeypatch.setattr(
        GoogleOAuthProvider,
        "verify_token",
        lambda self, token: {
            "sub": "google-subject",
            "email": "google-signup@example.com",
            "email_verified": True,
            "name": "Google User",
        },
    )
    client.cookies.clear()
    login = await client.post("/auth/oauth/login", json={"provider": OAuthProvider.GOOGLE.value, "token": "t"})
    assert login.status_code == 200, login.text
    user = {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}

    me = (await client.get("/users/me", headers=user["headers"])).json()
    assert me["email_verified"] is True
    assert me["phone_verification_required"] is True
    assert (await client.patch("/users/me/role", json={"role": "SME"}, headers=user["headers"])).status_code == 403

    # No email step for a provider-verified address: straight to the code.
    assert (await _send(client, user)).status_code == 200
    assert (await _verify(client, user, _code_from(outbox))).status_code == 200
    # The Terms & Services gate (docs/specs/consent/) also stands before a role.
    await record_consent(db_session, UUID(me["id"]))
    assert (await client.patch("/users/me/role", json={"role": "SME"}, headers=user["headers"])).status_code == 200


async def test_existing_smes_and_investors_are_gated_until_verified(monkeypatch):
    monkeypatch.setattr(settings, "PHONE_VERIFICATION_REQUIRED", True)
    for role in ("SME", "INVESTOR"):
        existing = User(email=f"old-{role}@example.com", role=role, email_verified=True, status="ACTIVE")
        assert user_me_payload(existing)["phone_verification_required"] is True, role
        existing.phone_verified_at = datetime.now(timezone.utc)
        assert user_me_payload(existing)["phone_verification_required"] is False, role


async def test_staff_are_never_gated(monkeypatch):
    monkeypatch.setattr(settings, "PHONE_VERIFICATION_REQUIRED", True)
    for role in ("ADMIN", "SYSTEM_ADMIN"):
        staff = User(email=f"{role.lower()}@example.com", role=role, email_verified=True, status="ACTIVE")
        assert user_me_payload(staff)["phone_verification_required"] is False, role


async def test_an_existing_account_with_a_role_verifies_its_phone(client, make_user, db_session, outbox):
    # Chose its role before the gate existed.
    investor = await _new_user(make_user, db_session, role="INVESTOR")

    me = (await client.get("/users/me", headers=investor["headers"])).json()
    assert me["role"] == "INVESTOR" and me["phone_verification_required"] is True

    assert (await _send(client, investor)).status_code == 200
    verified = await _verify(client, investor, _code_from(outbox))
    assert verified.status_code == 200, verified.text
    me = (await client.get("/users/me", headers=investor["headers"])).json()
    assert me["phone_verified"] is True and me["phone_verification_required"] is False
    assert me["role"] == "INVESTOR"  # the role is untouched


async def test_gate_is_off_when_not_required(monkeypatch):
    monkeypatch.setattr(settings, "PHONE_VERIFICATION_REQUIRED", False)
    new = User(email="new@example.com", role=None, email_verified=True, status="ACTIVE")
    assert user_me_payload(new)["phone_verification_required"] is False


async def test_changing_the_profile_number_clears_verification(client, make_user, db_session, outbox):
    user = await _new_user(make_user, db_session)
    await _send(client, user)
    await _verify(client, user, _code_from(outbox))

    same = await client.patch("/users/me", json={"phone": "0912 345 678"}, headers=user["headers"])
    assert same.json()["phone_verified"] is True

    changed = await client.patch("/users/me", json={"phone": "0987654321"}, headers=user["headers"])
    assert changed.json()["phone_verified"] is False
    assert changed.json()["phone_verification_required"] is True
