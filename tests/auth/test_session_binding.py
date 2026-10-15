"""Access tokens die with their session; refresh tokens detect reuse.

The "JWT still works after logout" problem: a signed JWT is valid until `exp`
by construction, so revoking only the refresh token — which is all logout and
every "sign out" path used to do — left the access token beside it working for
up to 30 more minutes. These tests pin the fix:

  * the access token carries its session id (`sid`) and get_current_user
    refuses it once the session has no live refresh token;
  * logout ends the whole session, not one token;
  * a rotated refresh token replayed after the grace window revokes the whole
    session (reuse detection, RFC 9700 §4.14.2), while a replay inside it —
    a browser racing itself — is answered normally;
  * no session outlives SESSION_MAX_LIFETIME_DAYS, however often refreshed;
  * changing the password ends the OTHER sessions and spares this one.
"""
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import update

from app.auth.service import REFRESH_REUSE_GRACE, hash_refresh_token
from app.core.config import settings
from app.users.models import RefreshToken
from app.utils.jwt import create_access_token
from conftest import DEFAULT_PASSWORD


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _second_login(client, user: dict) -> dict:
    """Sign the same account in again — a second device."""
    resp = await client.post(
        "/auth/login", json={"email": user["email"], "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return {
        "access_token": resp.cookies.get("access_token"),
        "refresh_token": resp.cookies.get("refresh_token"),
    }


async def _age_rotation(db_session, refresh_token: str, by: timedelta) -> None:
    """Pretend the given token was rotated `by` ago."""
    await db_session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
        .values(revoked_at=datetime.now(timezone.utc) - by)
    )
    await db_session.commit()


async def test_access_token_carries_its_session_id(make_user):
    user = await make_user()
    claims = jwt.decode(
        user["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert claims["sid"]


async def test_access_token_rejected_after_logout(client, make_user):
    user = await make_user()
    assert (await client.get("/users/me", headers=user["headers"])).status_code == 200

    logout = await client.post(
        "/auth/logout", headers={"Cookie": f"refresh_token={user['refresh_token']}"}
    )
    assert logout.status_code == 200

    # The very token a thief would have copied before the user signed out.
    after = await client.get("/users/me", headers=user["headers"])
    assert after.status_code == 401


async def test_access_token_without_session_id_rejected(client, make_user):
    """A token minted before session binding (or forged without it) is not a
    session. The browser's refresh replaces it with one that is."""
    user = await make_user()
    legacy = create_access_token(data={"sub": user["id"]})
    assert (await client.get("/users/me", headers=_bearer(legacy))).status_code == 401


async def test_sign_out_other_devices_kills_their_access_tokens(client, make_user):
    user = await make_user()
    other = await _second_login(client, user)
    assert (await client.get("/users/me", headers=_bearer(other["access_token"]))).status_code == 200

    resp = await client.post("/auth/sessions/revoke-others", headers=user["headers"])
    assert resp.status_code == 200
    assert resp.json()["revoked"] == 1

    assert (await client.get("/users/me", headers=_bearer(other["access_token"]))).status_code == 401
    # The device that pressed the button stays signed in.
    assert (await client.get("/users/me", headers=user["headers"])).status_code == 200


async def test_revoking_one_device_kills_its_access_token(client, make_user):
    user = await make_user()
    other = await _second_login(client, user)

    sessions = (await client.get("/auth/sessions", headers=user["headers"])).json()
    assert [s["current"] for s in sessions].count(True) == 1
    target = next(s for s in sessions if not s["current"])

    resp = await client.post(
        f"/auth/sessions/{target['session_id']}/revoke", headers=user["headers"]
    )
    assert resp.status_code == 200
    assert (await client.get("/users/me", headers=_bearer(other["access_token"]))).status_code == 401


async def test_cannot_revoke_own_session_from_the_list(client, make_user):
    user = await make_user()
    sessions = (await client.get("/auth/sessions", headers=user["headers"])).json()
    mine = next(s for s in sessions if s["current"])

    resp = await client.post(
        f"/auth/sessions/{mine['session_id']}/revoke", headers=user["headers"]
    )
    assert resp.status_code == 400


async def test_password_change_ends_other_sessions_and_keeps_this_one(client, make_user):
    user = await make_user()
    other = await _second_login(client, user)

    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": DEFAULT_PASSWORD, "new_password": "N3wStr0ngPassw0rd!2"},
        headers=user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sessions_revoked"] == 1

    assert (await client.get("/users/me", headers=user["headers"])).status_code == 200
    assert (await client.get("/users/me", headers=_bearer(other["access_token"]))).status_code == 401
    # And this device can still refresh past the 30-minute mark.
    refreshed = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert refreshed.status_code == 200


async def test_replay_inside_grace_window_is_answered(client, make_user):
    """Two tabs whose access tokens lapse together both refresh with the same
    cookie. The second is the same browser, not a thief."""
    user = await make_user()
    first = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert first.status_code == 200

    second = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert second.status_code == 200
    # Both pairs belong to the one session, which still lists once.
    sessions = (
        await client.get("/auth/sessions", headers=_bearer(second.json()["access_token"]))
    ).json()
    assert len(sessions) == 1


async def test_replay_after_grace_window_revokes_the_session(client, make_user, db_session):
    user = await make_user()
    rotated = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert rotated.status_code == 200
    new_pair = rotated.json()

    await _age_rotation(db_session, user["refresh_token"], REFRESH_REUSE_GRACE + timedelta(seconds=5))

    replay = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json() == {"detail": "Invalid or expired refresh token"}

    # Whoever holds the newer pair — user or thief — is out too.
    assert (
        await client.post("/auth/refresh", json={"refresh_token": new_pair["refresh_token"]})
    ).status_code == 401
    assert (await client.get("/users/me", headers=_bearer(new_pair["access_token"]))).status_code == 401


async def test_reuse_is_recorded_on_the_security_history(client, make_user, db_session):
    user = await make_user()
    await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    await _age_rotation(db_session, user["refresh_token"], REFRESH_REUSE_GRACE + timedelta(seconds=5))
    await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})

    # The account is signed out everywhere now, so read it from a fresh login.
    fresh = await _second_login(client, user)
    events = (
        await client.get("/auth/security-events", headers=_bearer(fresh["access_token"]))
    ).json()
    reuse = [e for e in events if e["action"] == "SESSION_REUSE_DETECTED"]
    assert len(reuse) == 1
    assert reuse[0]["severity"] == "critical"


async def test_grace_window_cannot_revive_a_logged_out_session(client, make_user):
    """Logout revokes the whole session, so a token it revoked a second ago
    finds no live session and is refused — never re-issued."""
    user = await make_user()
    await client.post(
        "/auth/logout", headers={"Cookie": f"refresh_token={user['refresh_token']}"}
    )
    replay = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert replay.status_code == 401


async def test_logout_ends_every_token_in_the_session(client, make_user):
    """A grace-window race leaves a session two live tokens; logging out with
    either must end both."""
    user = await make_user()
    one = (await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})).json()
    two = (await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})).json()

    await client.post("/auth/logout", headers={"Cookie": f"refresh_token={two['refresh_token']}"})

    assert (
        await client.post("/auth/refresh", json={"refresh_token": one["refresh_token"]})
    ).status_code == 401
    assert (await client.get("/users/me", headers=_bearer(one["access_token"]))).status_code == 401


async def test_session_cannot_outlive_its_maximum_lifetime(client, make_user, db_session):
    user = await make_user()
    # Signed in longer ago than any session may last, refreshed all along.
    await db_session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user["id"])
        .values(
            created_at=datetime.now(timezone.utc)
            - timedelta(days=settings.SESSION_MAX_LIFETIME_DAYS, minutes=1)
        )
    )
    await db_session.commit()

    resp = await client.post("/auth/refresh", json={"refresh_token": user["refresh_token"]})
    assert resp.status_code == 401
    # Ended, not just refused: its access token stops working too.
    assert (await client.get("/users/me", headers=user["headers"])).status_code == 401


async def test_refresh_token_never_outlives_the_session_deadline(
    client, make_user, get_refresh_token_row, monkeypatch
):
    monkeypatch.setattr(settings, "SESSION_MAX_LIFETIME_DAYS", 3)
    user = await make_user()
    (row,) = await get_refresh_token_row(user["id"])
    # Capped at the 3-day session deadline, not the 14-day refresh lifetime.
    assert row.expires_at - datetime.now(timezone.utc) <= timedelta(days=3)

    claims = jwt.decode(
        user["refresh_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert claims["exp"] <= row.expires_at.timestamp() + 1


async def test_auth_cookie_lifetimes_follow_the_token_lifetimes(client, make_user):
    user = await make_user()
    resp = await client.post(
        "/auth/login",
        json={"email": user["email"], "password": DEFAULT_PASSWORD, "remember_me": True},
    )
    cookies = {h.split("=", 1)[0]: h for h in resp.headers.get_list("set-cookie")}
    assert f"Max-Age={settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60}" in cookies["access_token"]
    assert f"Max-Age={settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400}" in cookies["refresh_token"]
