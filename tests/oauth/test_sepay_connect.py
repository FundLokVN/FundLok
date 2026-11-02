"""docs/specs/oauth/sepay-connect.md section 8: connect a SePay account over OAuth2.

SePay is never called: every request goes through app.oauth.sepay.client._send,
which each test replaces with a fake that records calls and answers from a script.
"""
import datetime as dt
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.lending.models import AuditLog
from app.oauth.sepay import service
from app.oauth.sepay.crypto import decrypt_token
from app.oauth.sepay.models import SepayConnection

FERNET_KEY = "kq7Yb1oX3m0m8f2xC7bYQ9a3oQm2Zl0w4G8c0e9T1rU="


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "SEPAY_CLIENT_ID", "fl-client")
    monkeypatch.setattr(settings, "SEPAY_CLIENT_SECRET", "fl-secret")
    monkeypatch.setattr(settings, "SEPAY_TOKEN_ENCRYPTION_KEY", FERNET_KEY)
    monkeypatch.setattr(settings, "SEPAY_BASE_URL", "https://sepay.test")
    monkeypatch.setattr(settings, "SEPAY_REDIRECT_URI", None)


@pytest.fixture
def sepay(monkeypatch):
    """A scripted SePay. `responses` maps (method, path) to a list of
    (status, json) answers consumed in order; `calls` records what was sent."""

    class Fake:
        def __init__(self):
            self.responses: dict[tuple[str, str], list[tuple[int, dict]]] = {}
            self.calls: list[dict] = []

        def on(self, method, path, status, body):
            self.responses.setdefault((method, path), []).append((status, body))

        async def send(self, method, url, **kwargs):
            path = urlparse(url).path
            self.calls.append({"method": method, "path": path, **kwargs})
            queue = self.responses.get((method, path))
            assert queue, f"unexpected SePay call {method} {path}"
            status, body = queue.pop(0)
            return httpx.Response(status, json=body, request=httpx.Request(method, url))

    fake = Fake()
    monkeypatch.setattr("app.oauth.sepay.client._send", fake.send)
    return fake


def _tokens(n=1):
    return {
        "access_token": f"access-{n}",
        "refresh_token": f"refresh-{n}",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


async def _connect(client, user, sepay):
    resp = await client.post("/integrations/sepay/connect", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    state = parse_qs(urlparse(resp.json()["authorize_url"]).query)["state"][0]
    sepay.on("POST", "/oauth/token", 200, _tokens(1))
    return await client.post(
        "/integrations/sepay/callback", json={"code": "the-code", "state": state}, headers=user["headers"]
    )


async def _row(db_session, user):
    return (
        await db_session.execute(select(SepayConnection).where(SepayConnection.user_id == uuid.UUID(user["id"])))
    ).scalar_one_or_none()


async def test_status_when_not_configured_reports_unconfigured(make_user, client, monkeypatch):
    monkeypatch.setattr(settings, "SEPAY_CLIENT_ID", None)
    user = await make_user(role="SME")
    resp = await client.get("/integrations/sepay", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "configured": False,
        "connected": False,
        "status": None,
        "connected_at": None,
        "scopes": [],
    }


async def test_connect_returns_authorize_url_with_signed_state(make_user, client, configured):
    user = await make_user(role="SME")
    resp = await client.post("/integrations/sepay/connect", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    url = urlparse(resp.json()["authorize_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://sepay.test/oauth/authorize"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "fl-client"
    assert q["redirect_uri"] == settings.sepay_redirect_uri
    assert q["scope"] == settings.SEPAY_SCOPES
    payload = jwt.decode(q["state"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert payload["typ"] == "sepay_oauth_state"
    assert payload["sub"] == user["id"]


async def test_connect_refused_when_not_configured(make_user, client, monkeypatch):
    monkeypatch.setattr(settings, "SEPAY_CLIENT_SECRET", None)
    user = await make_user(role="SME")
    resp = await client.post("/integrations/sepay/connect", headers=user["headers"])
    assert resp.status_code == 503
    assert resp.json() == {"detail": "SePay connection is not available."}


async def test_callback_stores_encrypted_tokens_and_audits(make_user, client, configured, sepay, db_session):
    user = await make_user(role="SME")
    resp = await _connect(client, user, sepay)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is True and body["status"] == "ACTIVE"
    assert "access-1" not in resp.text and "refresh-1" not in resp.text

    sent = sepay.calls[-1]["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert sent["client_secret"] == "fl-secret"
    assert sent["redirect_uri"] == settings.sepay_redirect_uri

    row = await _row(db_session, user)
    assert row.access_token.startswith("v1:") and "access-1" not in row.access_token
    assert decrypt_token(row.access_token) == "access-1"
    assert decrypt_token(row.refresh_token) == "refresh-1"

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_type == "SEPAY_CONNECTION"))
    ).scalars().all()
    assert [a.action for a in audit] == ["CONNECT"]


async def test_callback_rejects_state_for_another_user(make_user, client, configured, sepay, db_session):
    alice = await make_user(role="SME")
    bob = await make_user(role="SME")
    resp = await client.post("/integrations/sepay/connect", headers=alice["headers"])
    state = parse_qs(urlparse(resp.json()["authorize_url"]).query)["state"][0]

    resp = await client.post(
        "/integrations/sepay/callback", json={"code": "c", "state": state}, headers=bob["headers"]
    )
    assert resp.status_code == 400
    assert sepay.calls == []
    assert await _row(db_session, bob) is None


async def test_callback_rejects_expired_state(make_user, client, configured, sepay):
    user = await make_user(role="SME")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)
    state = service.create_state(uuid.UUID(user["id"]), old)
    resp = await client.post(
        "/integrations/sepay/callback", json={"code": "c", "state": state}, headers=user["headers"]
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == service.BAD_STATE
    assert sepay.calls == []


async def test_callback_maps_sepay_invalid_grant_to_400(make_user, client, configured, sepay, db_session):
    user = await make_user(role="SME")
    resp = await client.post("/integrations/sepay/connect", headers=user["headers"])
    state = parse_qs(urlparse(resp.json()["authorize_url"]).query)["state"][0]
    sepay.on("POST", "/oauth/token", 400, {"error": "invalid_grant"})

    resp = await client.post(
        "/integrations/sepay/callback", json={"code": "used", "state": state}, headers=user["headers"]
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == service.CODE_REJECTED
    assert await _row(db_session, user) is None


def _account_page(page, last_page, accounts):
    return {"status": "success", "data": accounts, "meta": {"pagination": {"current_page": page, "last_page": last_page}}}


VIETIN = {
    "id": 7,
    "label": None,
    "account_holder_name": "HUYNH TAN PHAT",
    "account_number": "123123123123",
    "accumulated": 987654321,
    "active": True,
    "bank": {"short_name": "VietinBank", "full_name": "Vietnam Joint Stock Commercial Bank for Industry and Trade",
             "code": "ICB", "bin": "970415", "logo_url": "https://x/l.png", "icon_url": "https://x/i.png"},
}


async def test_bank_accounts_hides_balance(make_user, client, configured, sepay):
    user = await make_user(role="SME")
    await _connect(client, user, sepay)
    sepay.on("GET", "/api/v1/bank-accounts", 200, _account_page(1, 2, [VIETIN]))
    sepay.on("GET", "/api/v1/bank-accounts", 200, _account_page(2, 2, [{**VIETIN, "id": 8}]))

    resp = await client.get("/integrations/sepay/bank-accounts", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    accounts = resp.json()
    assert [a["id"] for a in accounts] == [7, 8]
    assert accounts[0]["bank_bin"] == "970415"
    assert accounts[0]["account_number"] == "123123123123"
    assert "accumulated" not in accounts[0]
    assert "987654321" not in resp.text
    assert sepay.calls[-1]["headers"]["Authorization"] == "Bearer access-1"


async def test_bank_accounts_refreshes_expired_token_and_saves_both_tokens(
    make_user, client, configured, sepay, db_session
):
    user = await make_user(role="SME")
    await _connect(client, user, sepay)
    row = await _row(db_session, user)
    row.access_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    await db_session.commit()

    sepay.on("POST", "/oauth/token", 200, _tokens(2))
    sepay.on("GET", "/api/v1/bank-accounts", 200, _account_page(1, 1, [VIETIN]))
    resp = await client.get("/integrations/sepay/bank-accounts", headers=user["headers"])
    assert resp.status_code == 200, resp.text

    refresh_call = [c for c in sepay.calls if c["path"] == "/oauth/token"][-1]
    assert refresh_call["data"]["grant_type"] == "refresh_token"
    assert refresh_call["data"]["refresh_token"] == "refresh-1"
    assert sepay.calls[-1]["headers"]["Authorization"] == "Bearer access-2"

    await db_session.refresh(row)
    assert decrypt_token(row.access_token) == "access-2"
    assert decrypt_token(row.refresh_token) == "refresh-2"
    assert row.last_refreshed_at is not None


async def test_refresh_invalid_grant_marks_needs_reconnect(make_user, client, configured, sepay, db_session):
    user = await make_user(role="SME")
    await _connect(client, user, sepay)
    # SePay refuses the access token, then refuses the refresh too.
    sepay.on("GET", "/api/v1/bank-accounts", 401, {"error": "unauthorized"})
    sepay.on("POST", "/oauth/token", 400, {"error": "invalid_grant"})

    resp = await client.get("/integrations/sepay/bank-accounts", headers=user["headers"])
    assert resp.status_code == 409
    assert resp.json()["detail"] == service.NEEDS_RECONNECT

    status = (await client.get("/integrations/sepay", headers=user["headers"])).json()
    assert status["status"] == "NEEDS_RECONNECT"


async def test_disconnect_deletes_connection_and_audits(make_user, client, configured, sepay, db_session):
    user = await make_user(role="INVESTOR")
    await _connect(client, user, sepay)

    resp = await client.delete("/integrations/sepay", headers=user["headers"])
    assert resp.status_code == 204
    assert await _row(db_session, user) is None
    actions = (
        await db_session.execute(
            select(AuditLog.action).where(AuditLog.entity_type == "SEPAY_CONNECTION").order_by(AuditLog.created_at)
        )
    ).scalars().all()
    assert actions == ["CONNECT", "DISCONNECT"]

    # Disconnecting again is harmless.
    assert (await client.delete("/integrations/sepay", headers=user["headers"])).status_code == 204
    status = (await client.get("/integrations/sepay", headers=user["headers"])).json()
    assert status["connected"] is False


async def test_investor_and_sme_allowed_admin_refused(make_user, make_admin, client, configured):
    for role in ("SME", "INVESTOR"):
        user = await make_user(role=role)
        assert (await client.get("/integrations/sepay", headers=user["headers"])).status_code == 200
    no_role = await make_user()
    assert (await client.get("/integrations/sepay", headers=no_role["headers"])).status_code == 403
    admin = await make_admin()
    assert (await client.post("/integrations/sepay/connect", headers=admin["headers"])).status_code == 403
