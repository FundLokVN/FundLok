"""PartyKit push: token shape, and that publishing can never break a caller."""
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from jose import jwt

from app.core.config import settings
from app.notifications import realtime


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "PARTYKIT_HOST", "x.partykit.dev")
    monkeypatch.setattr(settings, "PARTYKIT_JWT_SECRET", "jwt-secret")
    monkeypatch.setattr(settings, "PARTYKIT_PUBLISH_SECRET", "pub-secret")


async def test_token_is_scoped_to_the_user_and_short_lived(monkeypatch):
    _enable(monkeypatch)
    uid = uuid.uuid4()
    token = realtime.mint_token(uid)
    claims = jwt.decode(token, "jwt-secret", algorithms=["HS256"], audience="partykit")
    assert claims["sub"] == str(uid)
    assert claims["exp"] - claims["iat"] == realtime.TOKEN_TTL_SECONDS


async def test_publish_is_a_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "PARTYKIT_HOST", None)
    with patch("app.notifications.realtime.httpx.AsyncClient") as client:
        await realtime.publish([uuid.uuid4()])
    client.assert_not_called()


async def test_publish_swallows_network_errors(monkeypatch):
    _enable(monkeypatch)
    with patch(
        "app.notifications.realtime.httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ConnectError("down")),
    ):
        await realtime.publish([uuid.uuid4()])  # must not raise


async def test_token_endpoint_404s_when_disabled(client, make_user, monkeypatch):
    monkeypatch.setattr(settings, "PARTYKIT_HOST", None)
    user = await make_user(role="SME")
    resp = await client.post("/notifications/realtime-token", headers=user["headers"])
    assert resp.status_code == 404


async def test_token_endpoint_returns_a_token_when_enabled(client, make_user, monkeypatch):
    _enable(monkeypatch)
    user = await make_user(role="SME")
    resp = await client.post("/notifications/realtime-token", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_in"] == realtime.TOKEN_TTL_SECONDS


async def test_publish_url_is_http_for_local_partykit(monkeypatch):
    monkeypatch.setattr(settings, "PARTYKIT_HOST", "localhost:1999")
    assert realtime._publish_url("u1") == "http://localhost:1999/parties/main/u1"
    monkeypatch.setattr(settings, "PARTYKIT_HOST", "x.partykit.dev")
    assert realtime._publish_url("u1") == "https://x.partykit.dev/parties/main/u1"
