"""OAuth sign-in must not let anonymous traffic stall the API.

POST /auth/oauth/login needs no session and no captcha, and every call ran the
provider's token verification inline: a blocking HTTPS fetch of the discovery
document and the JWKS (10 s timeout each) inside the async handler, before the
token was even parsed. A modest flood of junk tokens froze the worker's event
loop for every other request. Verification now runs in a worker thread, the
token is parsed before any fetch, and the provider documents are cached.
"""
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.oauth import service as oauth_service
from app.oauth.service import GoogleOAuthProvider, MicrosoftOAuthProvider

CLIENT_ID = "fundlok-client.apps.googleusercontent.com"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid: str) -> dict:
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**public, "kid": kid, "use": "sig"}


def _id_token(key, kid: str) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "subject",
        "email": "someone@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 600,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def counted_fetch(monkeypatch):
    """Serve discovery + a JWKS locally and count network fetches per URL."""
    calls: dict[str, int] = {}
    state = {"keys": []}

    def fake_fetch(self, url):
        calls[url] = calls.get(url, 0) + 1
        if url == GoogleOAuthProvider.discovery_url:
            return {"jwks_uri": JWKS_URI, "id_token_signing_alg_values_supported": ["RS256"]}
        return {"keys": list(state["keys"])}

    monkeypatch.setattr(GoogleOAuthProvider, "_fetch_json", fake_fetch)
    return calls, state


def test_provider_documents_are_fetched_once_for_many_logins(signing_key, counted_fetch):
    calls, state = counted_fetch
    state["keys"] = [_jwk(signing_key, "k1")]
    provider = GoogleOAuthProvider(CLIENT_ID)

    for _ in range(5):
        provider.verify_id_token(_id_token(signing_key, "k1"))

    assert calls == {GoogleOAuthProvider.discovery_url: 1, JWKS_URI: 1}


def test_a_token_that_is_not_a_jwt_fails_before_any_fetch(counted_fetch, monkeypatch):
    calls, _ = counted_fetch
    monkeypatch.setattr(MicrosoftOAuthProvider, "_fetch_json", GoogleOAuthProvider._fetch_json)

    with pytest.raises(HTTPException) as exc:
        MicrosoftOAuthProvider("client").verify_id_token("not-a-jwt")

    assert exc.value.status_code == 401
    assert calls == {}


def test_a_new_signing_key_is_picked_up(signing_key, counted_fetch):
    calls, state = counted_fetch
    state["keys"] = [_jwk(signing_key, "old")]
    provider = GoogleOAuthProvider(CLIENT_ID)
    provider.verify_id_token(_id_token(signing_key, "old"))

    # Rotation: a key the cached JWKS has never seen, published since. The
    # cached copy is old enough to refresh, so the new key is found.
    state["keys"].append(_jwk(signing_key, "new"))
    fetched_at, body = oauth_service._JSON_CACHE[JWKS_URI]
    oauth_service._JSON_CACHE[JWKS_URI] = (
        fetched_at - oauth_service._JWKS_MIN_REFRESH_INTERVAL,
        body,
    )

    assert provider.verify_id_token(_id_token(signing_key, "new"))["sub"] == "subject"
    assert calls[JWKS_URI] == 2


def test_unknown_key_ids_cannot_force_a_fetch_per_request(signing_key, counted_fetch):
    calls, state = counted_fetch
    state["keys"] = [_jwk(signing_key, "k1")]
    provider = GoogleOAuthProvider(CLIENT_ID)
    provider.verify_id_token(_id_token(signing_key, "k1"))

    for i in range(10):
        with pytest.raises(HTTPException):
            provider.verify_id_token(_id_token(signing_key, f"random-{i}"))

    assert calls[JWKS_URI] == 1


async def test_login_verifies_off_the_event_loop(client, monkeypatch):
    seen: dict[str, int] = {}

    def verify(self, token):
        seen["thread"] = threading.get_ident()
        return {
            "sub": "google-subject",
            "email": "threaded@example.com",
            "email_verified": True,
            "name": "Threaded",
        }

    monkeypatch.setattr(GoogleOAuthProvider, "verify_token", verify)

    resp = await client.post(
        "/auth/oauth/login", json={"provider": "google", "token": "any"}
    )

    assert resp.status_code == 200, resp.text
    assert seen["thread"] != threading.get_ident()
