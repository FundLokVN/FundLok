"""Phone verification codes by Zalo ZBS: the client, its token and its refusals.

Spec: docs/specs/users/phone-verification.md. Pins the ZBS request, the OA
token lifecycle (Zalo rotates the refresh token on every use, so losing the
new one would brick sending), and what the user is told when Zalo cannot
deliver. No test reaches Zalo: zalo._post is replaced.
"""
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import settings
from app.phone_verification import service, zalo
from app.phone_verification.models import ZaloOaToken
from tests.phone_verification.test_phone_verification import _codes, _new_user, _send, _set, _verify

REAL_ZALO_POST = zalo._post
REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def zalo_api(monkeypatch):
    """Turns on the gate with Zalo configured, and fakes Zalo's two endpoints."""
    calls: list[tuple[str, dict, dict]] = []
    state = SimpleNamespace(refreshes=0, send_errors=[], refuse_refresh=False)

    async def _zalo(url, *, headers, **kwargs):
        calls.append((url, headers, kwargs))
        if url == zalo.TOKEN_URL:
            if state.refuse_refresh:
                return {"error": -14014, "error_name": "Invalid refresh token"}
            state.refreshes += 1
            n = state.refreshes
            return {"access_token": f"access-{n}", "refresh_token": f"refresh-{n}", "expires_in": "90000"}
        assert url == zalo.SEND_URL
        error = state.send_errors.pop(0) if state.send_errors else 0
        return {"error": error, "message": "Success" if error == 0 else "Refused", "data": {"msg_id": "m-1"}}

    monkeypatch.setattr(zalo, "_post", _zalo)
    for name, value in {
        "PHONE_VERIFICATION_REQUIRED": True,
        "PHONE_OTP_CHANNEL": "zalo",
        "ZALO_APP_ID": "zalo-app",
        "ZALO_APP_SECRET": "zalo-app-secret",
        "ZALO_OA_REFRESH_TOKEN": "seed-refresh",
        "ZALO_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ZALO_ZBS_OTP_TEMPLATE_ID": "tpl-otp",
        "ZALO_ZBS_OTP_PARAM": "otp",
    }.items():
        monkeypatch.setattr(settings, name, value)
    return SimpleNamespace(calls=calls, state=state)


def _sends(api):
    return [kwargs["json"] for url, _, kwargs in api.calls if url == zalo.SEND_URL]


def _refreshes(api):
    return [(headers, kwargs["data"]) for url, headers, kwargs in api.calls if url == zalo.TOKEN_URL]


async def _token_row(db_session) -> ZaloOaToken:
    db_session.expire_all()
    return (await db_session.execute(select(ZaloOaToken))).scalar_one()


async def _another_send(client, make_user, db_session):
    user = await _new_user(make_user, db_session)
    assert (await _send(client, user, "0987654321")).status_code == 200
    return user


# --- Sending ----------------------------------------------------------------


async def test_zalo_sends_the_code_through_the_otp_template(client, make_user, db_session, zalo_api):
    user = await _new_user(make_user, db_session)

    resp = await _send(client, user)

    assert resp.status_code == 200, resp.text
    assert resp.json()["channel"] == "zalo"
    [(headers, form)] = _refreshes(zalo_api)
    assert headers == {"secret_key": "zalo-app-secret"}
    assert form == {"refresh_token": "seed-refresh", "app_id": "zalo-app", "grant_type": "refresh_token"}
    [(url, headers, kwargs)] = [c for c in zalo_api.calls if c[0] == zalo.SEND_URL]
    assert headers == {"access_token": "access-1"}
    [row] = await _codes(db_session, user["id"])
    assert kwargs["json"] == {
        "phone": "84912345678",
        "template_id": "tpl-otp",
        "template_data": {"otp": kwargs["json"]["template_data"]["otp"]},
        "tracking_id": row.id.hex,
    }
    assert row.channel == "zalo"

    code = kwargs["json"]["template_data"]["otp"]
    assert (await _verify(client, user, code)).json()["phone_verified"] is True


async def test_development_mode_marks_the_send(client, make_user, db_session, zalo_api, monkeypatch):
    monkeypatch.setattr(settings, "ZALO_ZBS_DEVELOPMENT_MODE", True)
    await _send(client, await _new_user(make_user, db_session))

    [body] = _sends(zalo_api)
    assert body["mode"] == "development"


@pytest.mark.parametrize("error", [-108, -140])
async def test_number_zalo_cannot_send_to_is_the_users_to_fix(client, make_user, db_session, zalo_api, error):
    zalo_api.state.send_errors = [error]
    resp = await _send(client, await _new_user(make_user, db_session))
    assert resp.status_code == 400, resp.text


async def test_template_parameter_name_is_configurable(client, make_user, db_session, zalo_api, monkeypatch):
    monkeypatch.setattr(settings, "ZALO_ZBS_OTP_PARAM", "ma_xac_thuc")
    await _send(client, await _new_user(make_user, db_session))

    [body] = _sends(zalo_api)
    assert list(body["template_data"]) == ["ma_xac_thuc"]


# --- The OA token -----------------------------------------------------------


async def test_tokens_are_stored_encrypted(client, make_user, db_session, zalo_api):
    await _send(client, await _new_user(make_user, db_session))

    row = await _token_row(db_session)
    assert row.app_id == "zalo-app"
    assert row.access_token.startswith("v1:") and "access-1" not in row.access_token
    assert row.refresh_token.startswith("v1:") and "refresh-1" not in row.refresh_token
    assert zalo._decrypt(row.refresh_token) == "refresh-1"
    assert row.seeded_from == hashlib.sha256(b"seed-refresh").hexdigest()


async def test_a_live_token_is_reused(client, make_user, db_session, zalo_api):
    await _send(client, await _new_user(make_user, db_session))
    await _another_send(client, make_user, db_session)

    assert len(_refreshes(zalo_api)) == 1
    assert [call[1] for call in zalo_api.calls if call[0] == zalo.SEND_URL] == [{"access_token": "access-1"}] * 2


async def test_an_expiring_token_is_refreshed_with_the_rotated_refresh_token(client, make_user, db_session, zalo_api):
    await _send(client, await _new_user(make_user, db_session))
    row = await _token_row(db_session)
    row.access_expires_at = row.access_expires_at.replace(year=2000)
    await db_session.commit()

    await _another_send(client, make_user, db_session)

    # The seed was spent by the first refresh; only the rotated token works now.
    assert [form["refresh_token"] for _, form in _refreshes(zalo_api)] == ["seed-refresh", "refresh-1"]
    assert zalo._decrypt((await _token_row(db_session)).refresh_token) == "refresh-2"


async def test_a_rejected_token_is_refreshed_once_and_retried(client, make_user, db_session, zalo_api):
    zalo_api.state.send_errors = [zalo.TOKEN_INVALID]

    resp = await _send(client, await _new_user(make_user, db_session))

    assert resp.json()["channel"] == "zalo"
    assert [url for url, _, _ in zalo_api.calls] == [zalo.TOKEN_URL, zalo.SEND_URL, zalo.TOKEN_URL, zalo.SEND_URL]
    assert [form["refresh_token"] for _, form in _refreshes(zalo_api)] == ["seed-refresh", "refresh-1"]


async def test_a_new_seed_in_the_environment_starts_a_new_chain(
    client, make_user, db_session, zalo_api, monkeypatch
):
    await _send(client, await _new_user(make_user, db_session))
    # The OA was re-authorised and the operator pasted the new refresh token.
    monkeypatch.setattr(settings, "ZALO_OA_REFRESH_TOKEN", "seed-two")

    await _another_send(client, make_user, db_session)

    assert [form["refresh_token"] for _, form in _refreshes(zalo_api)] == ["seed-refresh", "seed-two"]
    assert (await _token_row(db_session)).seeded_from == hashlib.sha256(b"seed-two").hexdigest()


async def test_the_token_survives_a_failed_send(client, make_user, db_session, zalo_api):
    # The send rolls back, but the refresh already rotated Zalo's token: it
    # must be kept, or the next send would present a spent refresh token.
    zalo_api.state.send_errors = [-115]  # out of quota
    user = await _new_user(make_user, db_session)
    assert (await _send(client, user)).status_code == 502

    assert zalo._decrypt((await _token_row(db_session)).refresh_token) == "refresh-1"


# --- When Zalo cannot deliver -----------------------------------------------


async def test_a_number_without_zalo_is_refused_with_a_reason(client, make_user, db_session, zalo_api):
    zalo_api.state.send_errors = [-118]  # Zalo account does not exist
    user = await _new_user(make_user, db_session)

    resp = await _send(client, user)

    assert resp.status_code == 400
    assert resp.json()["detail"] == service.NO_ZALO
    assert await _codes(db_session, user["id"]) == []


async def test_other_zalo_refusals_are_a_502(client, make_user, db_session, zalo_api):
    zalo_api.state.send_errors = [-131]  # template not approved
    user = await _new_user(make_user, db_session)

    resp = await _send(client, user)

    assert resp.status_code == 502
    assert await _codes(db_session, user["id"]) == []


@pytest.mark.parametrize(
    "setting",
    ["ZALO_ZBS_OTP_TEMPLATE_ID", "ZALO_APP_ID", "ZALO_APP_SECRET", "ZALO_TOKEN_ENCRYPTION_KEY"],
)
async def test_missing_zalo_configuration_is_a_503(client, make_user, db_session, zalo_api, monkeypatch, setting):
    monkeypatch.setattr(settings, setting, None)

    resp = await _send(client, await _new_user(make_user, db_session))

    assert resp.status_code == 503
    assert zalo_api.calls == []


async def test_a_refused_refresh_is_a_502(client, make_user, db_session, zalo_api):
    zalo_api.state.refuse_refresh = True

    resp = await _send(client, await _new_user(make_user, db_session))

    assert resp.status_code == 502
    assert _sends(zalo_api) == []


async def test_no_refresh_token_anywhere_is_a_503(client, make_user, db_session, zalo_api, monkeypatch):
    monkeypatch.setattr(settings, "ZALO_OA_REFRESH_TOKEN", None)

    resp = await _send(client, await _new_user(make_user, db_session))

    assert resp.status_code == 503
    assert zalo_api.calls == []


# --- The HTTP layer -----------------------------------------------------------


async def test_post_sends_headers_and_body_as_given(monkeypatch):
    monkeypatch.setattr(zalo, "_post", REAL_ZALO_POST)
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"error": 0, "message": "Success"})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw)
    )

    data = await zalo._post(zalo.SEND_URL, headers={"access_token": "tok"}, json={"phone": "84912345678"})

    assert data == {"error": 0, "message": "Success"}
    [request] = seen
    assert str(request.url) == "https://business.openapi.zalo.me/message/template"
    assert request.headers["access_token"] == "tok"
    assert request.headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("response", [httpx.Response(500, text="down"), httpx.Response(200, text="not json")])
async def test_post_raises_on_http_failure(monkeypatch, response):
    monkeypatch.setattr(zalo, "_post", REAL_ZALO_POST)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(lambda r: response), **kw)
    )
    with pytest.raises(zalo.ZaloError):
        await zalo._post(zalo.TOKEN_URL, headers={"secret_key": "s"}, data={"a": "b"})
