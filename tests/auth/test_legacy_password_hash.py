"""A stored password_hash that isn't a real hash must fail a login, not crash it.

Google/Microsoft sign-ups made before 2026-08-09 stored a raw token_urlsafe(32)
placeholder in users.password_hash (see app/oauth/service.py). passlib raised
UnknownHashError on it, so POST /auth/login answered 500 instead of 401
(seen on prod, 2026-10-12).
"""

import secrets
from uuid import UUID

import pytest
from sqlalchemy import update

from app.users.models import User
from app.utils.password import hash_password, verify_password

LEGACY_PLACEHOLDER = secrets.token_urlsafe(32)


@pytest.mark.parametrize(
    "stored",
    [
        LEGACY_PLACEHOLDER,  # what pre-2026-08-09 OAuth sign-ups stored
        "$argon2id$v=19$m=65536,t=3,p=4$broken",  # right prefix, malformed body
        "$2b$12$notourscheme",  # a scheme this app doesn't use
        "",
        None,
    ],
)
def test_verify_password_treats_an_unusable_hash_as_a_wrong_password(stored):
    assert verify_password("AnyPassw0rd!1", stored) is False


def test_verify_password_still_checks_real_hashes():
    stored = hash_password("Str0ngPassw0rd!1")
    assert verify_password("Str0ngPassw0rd!1", stored) is True
    assert verify_password("WrongPassword!1", stored) is False


async def test_login_on_account_with_legacy_placeholder_hash_returns_401_not_500(
    client, db_session, make_user, caplog
):
    user = await make_user()
    await db_session.execute(
        update(User).where(User.id == UUID(user["id"])).values(password_hash=LEGACY_PLACEHOLDER)
    )
    await db_session.commit()
    db_session.expire_all()

    resp = await client.post("/auth/login", json={"email": user["email"], "password": "WhateverPassw0rd!1"})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Incorrect email or password"}
    assert LEGACY_PLACEHOLDER not in caplog.text  # the stored value is never logged
