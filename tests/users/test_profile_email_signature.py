"""PATCH /users/me persists email_signature and GET /users/me returns it.

email_signature is a free-text signature the user sets on their own profile
(see app/users/models.py::User). It follows the same PATCH semantics as bio:
sending a value sets it, an explicit null clears it, and omitting it leaves
the value unchanged. The field round-trips through the /me payload the
frontend reads.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def test_email_signature_is_unset_by_default(client, make_user):
    user = await make_user()
    me = await client.get("/users/me", headers=user["headers"])
    assert me.status_code == 200
    assert me.json()["email_signature"] is None


async def test_setting_email_signature_round_trips(client, make_user):
    user = await make_user()
    sig = "Best,\nJane Doe\nFundLok — +84 28 0000 0000"
    resp = await client.patch(
        "/users/me", json={"email_signature": sig}, headers=user["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_signature"] == sig

    # A fresh read returns the persisted value.
    me = await client.get("/users/me", headers=user["headers"])
    assert me.json()["email_signature"] == sig


async def test_a_blank_email_signature_clears_it(client, make_user):
    user = await make_user()
    await client.patch(
        "/users/me",
        json={"email_signature": "Was here"},
        headers=user["headers"],
    )

    # An explicit null clears it (PATCH semantics: null = remove).
    resp = await client.patch(
        "/users/me", json={"email_signature": None}, headers=user["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_signature"] is None


async def test_omitting_email_signature_leaves_it_unchanged(client, make_user):
    user = await make_user()
    await client.patch(
        "/users/me",
        json={"email_signature": "Keep me"},
        headers=user["headers"],
    )
    # A PATCH for an unrelated field must not touch the signature.
    await client.patch(
        "/users/me", json={"full_name": "Renamed"}, headers=user["headers"]
    )
    me = await client.get("/users/me", headers=user["headers"])
    assert me.json()["email_signature"] == "Keep me"


async def test_email_signature_over_2000_chars_is_rejected(client, make_user):
    user = await make_user()
    resp = await client.patch(
        "/users/me",
        json={"email_signature": "x" * 2001},
        headers=user["headers"],
    )
    assert resp.status_code == 422
