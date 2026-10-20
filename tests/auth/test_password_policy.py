"""SEC-03: every new password is strong and not known to be breached.

See app/auth/password_policy.py. conftest.py stubs the Have I Been Pwned
lookup to an empty range; tests here that need a breach re-patch it.
"""
import hashlib

import httpx
import pytest
from sqlalchemy import select

from app.auth import password_policy
from app.auth.password_policy import _fetch_range as real_fetch_range
from app.auth.password_policy import check_password_strength, is_breached
from app.users.models import User
from app.utils.password import hash_password
from conftest import DEFAULT_PASSWORD

BREACHED = "Summer2024!"  # strong by the rules, so only the breach check can refuse it
STRONG = "Fresh-Passw0rd-2026"


def _hibp_reports(monkeypatch, *passwords: str) -> None:
    """Make the HIBP range response list these passwords, plus a padding row."""
    suffixes = [hashlib.sha1(p.encode()).hexdigest().upper()[5:] for p in passwords]

    async def fake_range(prefix: str) -> str:
        return "\n".join([f"{s}:42" for s in suffixes] + ["0" * 35 + ":0"])

    monkeypatch.setattr(password_policy, "_fetch_range", fake_range)


# --- the rules --------------------------------------------------------------


@pytest.mark.parametrize(
    "password",
    ["Str0ngPassw0rd!1", "Mật-khẩu-2026", "aB3$efgh", "with Space 9!"],
)
def test_strong_passwords_pass(password):
    assert check_password_strength(password) == password


@pytest.mark.parametrize(
    "password, missing",
    [
        ("aB3$efg", "at least 8 characters"),
        ("AB3$EFGH", "a lowercase letter"),
        ("ab3$efgh", "an uppercase letter"),
        ("aB$$efgh", "a digit"),
        ("aB3defgh", "a symbol"),
        ("aB3 efgh", "a symbol"),  # a space is not a symbol
    ],
)
def test_each_missing_rule_is_named(password, missing):
    with pytest.raises(ValueError, match=missing):
        check_password_strength(password)


def test_everything_missing_is_listed_at_once():
    with pytest.raises(ValueError, match="needs at least 8 characters, an uppercase letter, a digit and a symbol"):
        check_password_strength("short")


def test_more_than_72_bytes_is_refused():
    # 29 characters but 79 bytes: "ậ" takes three bytes in UTF-8.
    with pytest.raises(ValueError, match="at most 72 bytes"):
        check_password_strength("Aa1!" + "ậ" * 25)


# --- the breach check -------------------------------------------------------


async def test_a_password_in_the_range_is_breached(monkeypatch):
    _hibp_reports(monkeypatch, BREACHED)
    assert await is_breached(BREACHED) is True
    assert await is_breached(STRONG) is False


async def test_padding_rows_are_not_matches(monkeypatch):
    suffix = hashlib.sha1(STRONG.encode()).hexdigest().upper()[5:]

    async def padded_only(prefix: str) -> str:
        return f"{suffix}:0"

    monkeypatch.setattr(password_policy, "_fetch_range", padded_only)
    assert await is_breached(STRONG) is False


async def test_only_a_five_character_hash_prefix_leaves_the_server(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(password_policy, "_fetch_range", real_fetch_range)
    monkeypatch.setattr(
        password_policy.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    await is_breached(STRONG)

    [request] = seen
    digest = hashlib.sha1(STRONG.encode()).hexdigest().upper()
    assert request.url.path == f"/range/{digest[:5]}"
    assert request.headers["Add-Padding"] == "true"
    assert digest[5:] not in str(request.url)
    assert STRONG not in str(request.url)


async def test_hibp_being_unreachable_does_not_block_anyone(monkeypatch):
    async def down(prefix: str) -> str:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(password_policy, "_fetch_range", down)
    assert await is_breached(BREACHED) is False


# --- sign-up ----------------------------------------------------------------


async def _register(client, email: str, password: str):
    return await client.post(
        "/auth/register", json={"email": email, "password": password, "full_name": "Policy Test"}
    )


async def test_sign_up_refuses_a_weak_password(client, db_session):
    resp = await _register(client, "weak@example.com", "password1")

    assert resp.status_code == 422, resp.text
    assert "an uppercase letter" in resp.text
    assert await db_session.scalar(select(User.id).where(User.email == "weak@example.com")) is None


async def test_sign_up_refuses_a_breached_password(client, db_session, monkeypatch):
    _hibp_reports(monkeypatch, BREACHED)

    resp = await _register(client, "breached@example.com", BREACHED)

    assert resp.status_code == 400, resp.text
    assert resp.headers["X-Error-Code"] == "PASSWORD_BREACHED"
    assert await db_session.scalar(select(User.id).where(User.email == "breached@example.com")) is None


async def test_sign_up_goes_ahead_when_hibp_is_down(client, monkeypatch):
    async def down(prefix: str) -> str:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(password_policy, "_fetch_range", down)

    resp = await _register(client, "hibp-down@example.com", STRONG)
    assert resp.status_code == 201, resp.text


# --- reset, first password, change ------------------------------------------


@pytest.fixture
def reset_links(monkeypatch):
    links: list[str] = []
    monkeypatch.setattr(
        "app.auth.service.send_password_reset_email", lambda to_email, token: links.append(token)
    )
    return links


async def _login(client, email: str, password: str):
    client.cookies.clear()
    return await client.post("/auth/login", json={"email": email, "password": password})


async def test_reset_refuses_weak_and_breached_passwords(client, make_user, reset_links, monkeypatch):
    user = await make_user()
    assert (await client.post("/auth/forgot-password", json={"email": user["email"]})).status_code == 200
    token = reset_links[-1]
    _hibp_reports(monkeypatch, BREACHED)

    weak = await client.post("/auth/reset-password", json={"token": token, "new_password": "newpassword"})
    breached = await client.post("/auth/reset-password", json={"token": token, "new_password": BREACHED})

    assert weak.status_code == 422, weak.text
    assert breached.status_code == 400, breached.text
    assert breached.headers["X-Error-Code"] == "PASSWORD_BREACHED"
    assert (await _login(client, user["email"], DEFAULT_PASSWORD)).status_code == 200


async def test_a_first_password_must_meet_the_rules(client, make_user, db_session, monkeypatch):
    user = await make_user()
    db_user = (await db_session.execute(select(User).where(User.email == user["email"]))).scalar_one()
    db_user.password_hash = None  # the OAuth case, as tests/users/test_set_password.py does
    await db_session.commit()
    _hibp_reports(monkeypatch, BREACHED)

    weak = await client.post("/users/me/password", json={"new_password": "abcdefgh"}, headers=user["headers"])
    breached = await client.post("/users/me/password", json={"new_password": BREACHED}, headers=user["headers"])

    assert weak.status_code == 422, weak.text
    assert breached.status_code == 400, breached.text
    assert breached.headers["X-Error-Code"] == "PASSWORD_BREACHED"
    await db_session.refresh(db_user)
    assert db_user.password_hash is None


async def test_a_changed_password_must_meet_the_rules(client, make_user, monkeypatch):
    user = await make_user()
    _hibp_reports(monkeypatch, BREACHED)

    def change(new_password: str):
        return client.post(
            "/users/me/password/change",
            json={"current_password": DEFAULT_PASSWORD, "new_password": new_password},
            headers=user["headers"],
        )

    weak = await change("NoDigitsHere!")
    breached = await change(BREACHED)

    assert weak.status_code == 422, weak.text
    assert "a digit" in weak.text
    assert breached.status_code == 400, breached.text
    assert breached.headers["X-Error-Code"] == "PASSWORD_BREACHED"
    assert (await _login(client, user["email"], DEFAULT_PASSWORD)).status_code == 200


async def test_a_password_set_under_the_old_rules_still_signs_in(client, make_user, db_session):
    user = await make_user()
    db_user = (await db_session.execute(select(User).where(User.email == user["email"]))).scalar_one()
    db_user.password_hash = hash_password("oldweak")
    await db_session.commit()

    assert (await _login(client, user["email"], "oldweak")).status_code == 200
