"""Google ID tokens cannot be forged with Google's own public key.

An HS256 token whose "secret" is a DER-encoded copy of the provider's public
key (which anyone can download from the JWKS endpoint) verifies wherever a JWT
library accepts raw bytes as an HMAC secret. python-jose 3.5.0 had exactly
this exposure (GHSA-3qf3-8w2g-rqmx, CVE-2026-85394, an incomplete fix for
CVE-2024-33663) and shipped no fixed release — which is why FundLok migrated
to PyJWT.

FundLok is not exposed. verify_id_token is the only place a public key meets
jwt.decode, and two things independently stop the forgery there:
1. _signing_algorithms never allows an HMAC algorithm (RS/ES/PS only), so an
   HS256 header is refused before the key is ever consulted;
2. PyJWT itself refuses a PEM/DER-formatted key as an HMAC secret
   (InvalidKeyError), a second layer python-jose lacked.
Every other decode uses the symmetric SECRET_KEY with algorithms pinned to
HS256. These tests pin both layers and the end result.
"""
import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.oauth.service import GoogleOAuthProvider

CLIENT_ID = "fundlok-client.apps.googleusercontent.com"
KID = "google-test-key"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(number: int) -> str:
    return _b64(number.to_bytes((number.bit_length() + 7) // 8, "big"))


@pytest.fixture(scope="module")
def google_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _claims() -> dict:
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "victim-google-subject",
        "email": "victim@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 600,
    }


@pytest.fixture
def provider(monkeypatch, google_key):
    """GoogleOAuthProvider whose discovery document and JWKS are served
    locally. `advertised` is what the discovery document claims; `jwk_alg` is
    the alg on the published key (Google omits it)."""

    def install(*, advertised=("RS256",), jwk_alg=None):
        numbers = google_key.public_key().public_numbers()
        jwk = {"kty": "RSA", "kid": KID, "use": "sig", "n": _int_b64(numbers.n), "e": _int_b64(numbers.e)}
        if jwk_alg:
            jwk["alg"] = jwk_alg
        discovery = {
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
            "id_token_signing_alg_values_supported": list(advertised),
        }

        def fake_fetch(self, url):
            return discovery if url == GoogleOAuthProvider.discovery_url else {"keys": [jwk]}

        monkeypatch.setattr(GoogleOAuthProvider, "_fetch_json", fake_fetch)
        return GoogleOAuthProvider(CLIENT_ID)

    return install


def _public_key_bytes(google_key, encoding) -> bytes:
    return google_key.public_key().public_bytes(encoding, serialization.PublicFormat.SubjectPublicKeyInfo)


def _forge_hs256(secret: bytes) -> str:
    """What an attacker sends: an HS256 token whose HMAC secret is the
    provider's public key, which anyone can download."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = _b64(json.dumps(_claims()).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def test_a_genuine_rs256_id_token_is_accepted(provider, google_key):
    # Control: the harness verifies a real token, so the rejections below
    # are about the algorithm and not a broken setup.
    private_pem = google_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    token = jwt.encode(_claims(), private_pem, algorithm="RS256", headers={"kid": KID})

    claims = provider().verify_id_token(token)

    assert claims["email"] == "victim@example.com"


def test_even_where_hs256_is_allowed_pyjwt_refuses_the_der_keyed_forgery(google_key):
    # Layer 2 on its own: even if HS256 were ever allowed, PyJWT refuses a
    # PEM/DER-formatted key as an HMAC secret — the exact behavior
    # python-jose 3.5.0 lacked (GHSA-3qf3-8w2g-rqmx). Pinned so an upgrade
    # that relaxes it is noticed.
    der = _public_key_bytes(google_key, serialization.Encoding.DER)
    forged = _forge_hs256(der)

    with pytest.raises(jwt.InvalidKeyError):
        jwt.decode(forged, der, algorithms=["HS256"], audience=CLIENT_ID)


@pytest.mark.parametrize("encoding", [serialization.Encoding.DER, serialization.Encoding.PEM])
@pytest.mark.parametrize(
    ("advertised", "jwk_alg"),
    [
        (("RS256",), None),  # what Google publishes
        (("RS256", "HS256"), None),  # a permissive discovery document
        (("RS256",), "HS256"),  # a published key claiming HS256
    ],
)
def test_an_hs256_token_forged_with_the_public_key_is_rejected(
    provider, google_key, encoding, advertised, jwk_alg
):
    forged = _forge_hs256(_public_key_bytes(google_key, encoding))

    with pytest.raises(HTTPException) as exc:
        provider(advertised=advertised, jwk_alg=jwk_alg).verify_id_token(forged)

    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    ("advertised", "jwk_alg"),
    [
        (["RS256"], None),
        (["RS256", "HS256", "HS512", "none"], None),
        (["HS256"], None),
        ([], None),
        (["RS256"], "HS256"),
        (["ES256"], "PS256"),
    ],
)
def test_signing_algorithms_never_include_hmac(advertised, jwk_alg):
    # Layer 1 on its own: whatever the provider publishes, only asymmetric
    # algorithms reach jwt.decode (RS256 when nothing usable is published).
    key = {"kty": "RSA", "kid": KID, **({"alg": jwk_alg} if jwk_alg else {})}
    algorithms = GoogleOAuthProvider(CLIENT_ID)._signing_algorithms(
        {"id_token_signing_alg_values_supported": advertised}, key
    )

    assert algorithms
    assert all(alg[:2] in ("RS", "ES", "PS") for alg in algorithms), algorithms
