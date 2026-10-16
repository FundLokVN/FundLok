"""A passkey without user verification must not get past enrolled 2FA.

Passkey sign-in never asks for the TOTP code: a passkey counts as both
factors, the device held and the PIN or biometric that unlocked it. Sign-in
asks for user verification (UV) only as PREFERRED, though, so an authenticator
that skips it (a security key with no PIN, say) produced a valid assertion
that proved possession alone. On an account with 2FA enrolled that was a
one-factor way around the second factor (vbsec 2026-10-16 hardening note).

These tests pin the fix:
  * a 2FA account refuses an assertion whose UV flag is clear, and accepts one
    whose flag is set;
  * an account without 2FA keeps accepting presence-only passkeys;
  * registration on a 2FA account asks for UV and refuses a passkey without
    it, so nobody adds a passkey that can never sign them in.

The authenticator below is a real one in software: a P-256 key, CBOR, the
flags byte. py_webauthn verifies every signature for real, so the UV flag is
read from signed authenticator data exactly as it would be from a device.
"""
import base64
import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from app.auth.passkeys import UV_REQUIRED_FOR_REGISTRATION, UV_REQUIRED_FOR_SIGN_IN
from app.core.config import settings
from app.users.models import User, WebAuthnCredential
from tests.auth.test_totp import enrol

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class SoftAuthenticator:
    """One passkey: an ES256 key pair plus the bytes a browser would send."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0

    def _cose_public_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, ceremony: str, challenge: str) -> bytes:
        return json.dumps(
            {
                "type": ceremony,
                "challenge": challenge,
                "origin": settings.webauthn_origins[0],
                "crossOrigin": False,
            }
        ).encode()

    def _auth_data_head(self, flags: int) -> bytes:
        rp_id_hash = hashlib.sha256(settings.webauthn_rp_id.encode()).digest()
        return rp_id_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big")

    def attestation(self, challenge: str, *, user_verified: bool) -> dict:
        flags = FLAG_UP | FLAG_AT | (FLAG_UV if user_verified else 0)
        attested = (
            bytes(16)  # AAGUID: unknown, as "none" attestation reports it
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_public_key()
        )
        attestation_object = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": self._auth_data_head(flags) + attested}
        )
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(self._client_data("webauthn.create", challenge)),
                "attestationObject": _b64(attestation_object),
            },
            "clientExtensionResults": {},
        }

    def assertion(self, challenge: str, *, user_verified: bool) -> dict:
        self.sign_count += 1
        flags = FLAG_UP | (FLAG_UV if user_verified else 0)
        auth_data = self._auth_data_head(flags)
        client_data = self._client_data("webauthn.get", challenge)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(client_data),
                "authenticatorData": _b64(auth_data),
                "signature": _b64(signature),
            },
            "clientExtensionResults": {},
        }


async def _verified_user(make_user, db_session) -> dict:
    """A passkey can only be added once the address is proven."""
    user = await make_user()
    row = (await db_session.execute(select(User).where(User.id == user["id"]))).scalar_one()
    row.email_verified = True
    await db_session.commit()
    return user


async def _register_options(client, user) -> dict:
    resp = await client.post("/auth/passkeys/register/options", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _register(client, user, authenticator, *, user_verified: bool):
    options = await _register_options(client, user)
    return await client.post(
        "/auth/passkeys/register/verify",
        json={"credential": authenticator.attestation(options["challenge"], user_verified=user_verified)},
        headers=user["headers"],
    )


async def _sign_in(client, authenticator, *, user_verified: bool):
    options = await client.post("/auth/passkeys/login/options")
    assert options.status_code == 200, options.text
    client.cookies.clear()
    return await client.post(
        "/auth/passkeys/login/verify",
        json={"credential": authenticator.assertion(options.json()["challenge"], user_verified=user_verified)},
    )


async def _stored_sign_count(db_session, authenticator) -> int:
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.credential_id == _b64(authenticator.credential_id)
            )
        )
    ).scalar_one()
    return row.sign_count


# --- sign-in ----------------------------------------------------------------------


async def test_a_2fa_account_refuses_a_passkey_that_skipped_user_verification(
    client, make_user, db_session
):
    # The realistic path: a PIN-less key added before 2FA, then 2FA enrolled.
    user = await _verified_user(make_user, db_session)
    key = SoftAuthenticator()
    assert (await _register(client, user, key, user_verified=False)).status_code == 201
    await enrol(client, user)

    resp = await _sign_in(client, key, user_verified=False)

    assert resp.status_code == 401
    assert resp.json() == {"detail": UV_REQUIRED_FOR_SIGN_IN}
    assert "access_token" not in resp.cookies
    # Refused before the success bookkeeping: the stored counter is untouched.
    assert await _stored_sign_count(db_session, key) == 0


async def test_a_2fa_account_accepts_a_user_verified_passkey(client, make_user, db_session):
    user = await _verified_user(make_user, db_session)
    key = SoftAuthenticator()
    assert (await _register(client, user, key, user_verified=True)).status_code == 201
    await enrol(client, user)

    resp = await _sign_in(client, key, user_verified=True)

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == user["id"]
    assert resp.cookies.get("access_token")


async def test_an_account_without_2fa_still_accepts_a_presence_only_passkey(
    client, make_user, db_session
):
    user = await _verified_user(make_user, db_session)
    key = SoftAuthenticator()
    assert (await _register(client, user, key, user_verified=False)).status_code == 201

    resp = await _sign_in(client, key, user_verified=False)

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == user["id"]


# --- registration -----------------------------------------------------------------


async def test_registration_asks_a_2fa_account_for_user_verification(
    client, make_user, db_session
):
    user = await _verified_user(make_user, db_session)
    before = await _register_options(client, user)
    await enrol(client, user)
    after = await _register_options(client, user)

    assert before["authenticatorSelection"]["userVerification"] == "preferred"
    assert after["authenticatorSelection"]["userVerification"] == "required"


async def test_a_2fa_account_cannot_add_a_passkey_without_user_verification(
    client, make_user, db_session
):
    user = await _verified_user(make_user, db_session)
    await enrol(client, user)

    resp = await _register(client, user, SoftAuthenticator(), user_verified=False)

    assert resp.status_code == 400
    assert resp.json() == {"detail": UV_REQUIRED_FOR_REGISTRATION}
    rows = (
        await db_session.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.user_id == user["id"])
        )
    ).scalars().all()
    assert rows == []


async def test_a_2fa_account_can_add_a_user_verified_passkey(client, make_user, db_session):
    user = await _verified_user(make_user, db_session)
    await enrol(client, user)

    resp = await _register(client, user, SoftAuthenticator(), user_verified=True)

    assert resp.status_code == 201, resp.text
