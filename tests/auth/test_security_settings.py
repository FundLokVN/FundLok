"""Password change and sign-in alerts — the two protections on
/dashboard/security that now have a backend.

The password-change endpoint is deliberately separate from /users/me/password:
that one only ever sets a FIRST password (overwriting an existing one from a
borrowed session would be account takeover), so changing an existing password
requires the current one as proof the session is not borrowed.
"""
import uuid

from sqlalchemy import select

from app.auth.service import MAX_STEP_UP_FAILURES, STEP_UP_FAILED, hash_refresh_token
from app.users.models import RefreshToken
from conftest import DEFAULT_PASSWORD, auth_headers

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
)


# --- change password -------------------------------------------------------


async def test_change_password_requires_the_current_one(client, make_user):
    user = await make_user()
    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": "not-the-password", "new_password": "NewPassw0rd!"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Password is incorrect"


async def test_change_password_shares_the_step_up_attempt_limit(client, make_user):
    """A stolen session must not get unlimited guesses at the password here
    while every other step-up stops it at five."""
    user = await make_user()
    for _ in range(MAX_STEP_UP_FAILURES):
        miss = await client.post(
            "/users/me/password/change",
            json={"current_password": "a-wrong-guess", "new_password": "NewPassw0rd!"},
            headers=auth_headers(user),
        )
        assert miss.status_code == 400, miss.text

    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": user["password"], "new_password": "NewPassw0rd!"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 429, resp.text
    assert resp.headers["X-Error-Code"] == "STEP_UP_LOCKED"

    events = await client.get("/auth/security-events", headers=auth_headers(user))
    actions = [event["action"] for event in events.json()]
    assert actions.count(STEP_UP_FAILED) == MAX_STEP_UP_FAILURES

    login = await client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert login.status_code == 200, "the password must be unchanged"


async def test_change_password_succeeds_and_the_new_one_works(client, make_user):
    user = await make_user()
    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": user["password"], "new_password": "NewPassw0rd!"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text

    old = await client.post(
        "/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert old.status_code == 401, "the old password must stop working"

    new = await client.post(
        "/auth/login", json={"email": user["email"], "password": "NewPassw0rd!"}
    )
    assert new.status_code == 200, new.text


async def test_change_password_revokes_every_other_session(
    client, make_user, db_session
):
    """Changing a password because someone else is in the account achieves
    nothing if their session keeps working — refresh tokens are independent of
    the password."""
    user = await make_user()
    # A second sign-in = a second live session.
    await client.post(
        "/auth/login",
        json={"email": user["email"], "password": user["password"]},
        headers={"user-agent": EDGE_WINDOWS},
    )

    live_before = (
        await db_session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user["id"],
                RefreshToken.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    assert len(live_before) >= 2

    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": user["password"], "new_password": "NewPassw0rd!"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    # The other sign-in only. This used to revoke the session making the
    # change as well (and assert >= 2), contradicting the name of this test
    # and change_password's docstring; the user was then signed out of the
    # device they had just secured.
    assert resp.json()["sessions_revoked"] == len(live_before) - 1

    live_after = (
        await db_session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user["id"],
                RefreshToken.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    assert [row.token_hash for row in live_after] == [
        hash_refresh_token(user["refresh_token"])
    ]


async def test_change_password_rejects_reusing_the_same_password(client, make_user):
    user = await make_user()
    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": user["password"], "new_password": user["password"]},
        headers=auth_headers(user),
    )
    assert resp.status_code == 400, resp.text
    assert "different" in resp.json()["detail"]


async def test_change_password_rejects_a_short_new_password(client, make_user):
    user = await make_user()
    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": user["password"], "new_password": "short"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 422, resp.text


async def test_change_password_requires_authentication(client):
    resp = await client.post(
        "/users/me/password/change",
        json={"current_password": "x", "new_password": "NewPassw0rd!"},
    )
    assert resp.status_code == 401


async def test_password_change_is_audited(client, make_user):
    user = await make_user()
    await client.post(
        "/users/me/password/change",
        json={"current_password": user["password"], "new_password": "NewPassw0rd!"},
        headers=auth_headers(user),
    )
    # The session cookies were revoked with everything else, so re-authenticate.
    await client.post(
        "/auth/login", json={"email": user["email"], "password": "NewPassw0rd!"}
    )
    events = (
        await client.get("/auth/security-events", headers=auth_headers(user))
    ).json()
    assert any(e["action"] == "PASSWORD_CHANGED" for e in events), events


# --- sign-in alerts --------------------------------------------------------


async def test_signin_alerts_default_to_on(client, make_user):
    user = await make_user()
    resp = await client.get(
        "/users/me/security-preferences", headers=auth_headers(user)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["signin_alerts_enabled"] is True


async def test_signin_alerts_can_be_toggled(client, make_user):
    user = await make_user()
    off = await client.patch(
        "/users/me/security-preferences",
        json={"signin_alerts_enabled": False},
        headers=auth_headers(user),
    )
    assert off.status_code == 200, off.text
    assert off.json()["signin_alerts_enabled"] is False

    read_back = await client.get(
        "/users/me/security-preferences", headers=auth_headers(user)
    )
    assert read_back.json()["signin_alerts_enabled"] is False

    on = await client.patch(
        "/users/me/security-preferences",
        json={"signin_alerts_enabled": True},
        headers=auth_headers(user),
    )
    assert on.json()["signin_alerts_enabled"] is True


async def test_toggling_alerts_is_audited(client, make_user):
    """Switching alerts OFF is what an account takeover would do, so the change
    itself has to appear in the security history."""
    user = await make_user()
    await client.patch(
        "/users/me/security-preferences",
        json={"signin_alerts_enabled": False},
        headers=auth_headers(user),
    )
    events = (
        await client.get("/auth/security-events", headers=auth_headers(user))
    ).json()
    assert any(e["action"] == "SIGNIN_ALERTS_CHANGED" for e in events), events


async def test_alert_is_sent_only_for_an_unseen_device(
    client, make_user, db_session, monkeypatch
):
    sent: list[dict] = []

    def _capture(to_email, *, device, browser, ip_address):
        sent.append({"to": to_email, "device": device, "browser": browser})

    # Patch where it is used, not where it is defined.
    monkeypatch.setattr("app.auth.service.send_new_device_signin_alert", _capture)

    # No opt-in: alerts are on for a new account.
    user = await make_user()

    # First sign-in from this UA -> unseen -> alert.
    await client.post(
        "/auth/login",
        json={"email": user["email"], "password": user["password"]},
        headers={"user-agent": EDGE_WINDOWS},
    )
    assert len(sent) == 1, sent
    assert sent[0]["device"] == "Windows PC"
    assert sent[0]["browser"] == "Edge 139"

    # Same UA again -> already seen -> no second alert.
    await client.post(
        "/auth/login",
        json={"email": user["email"], "password": user["password"]},
        headers={"user-agent": EDGE_WINDOWS},
    )
    assert len(sent) == 1, "a familiar device must not alert"


async def test_no_alert_when_the_preference_is_off(client, make_user, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        "app.auth.service.send_new_device_signin_alert",
        lambda *a, **k: sent.append(k),
    )

    user = await make_user()
    await client.patch(
        "/users/me/security-preferences",
        json={"signin_alerts_enabled": False},
        headers=auth_headers(user),
    )
    await client.post(
        "/auth/login",
        json={"email": user["email"], "password": user["password"]},
        headers={"user-agent": EDGE_WINDOWS},
    )
    assert sent == [], "an owner who switched alerts off gets none"


async def test_first_ever_signin_does_not_alert(client, monkeypatch):
    """There is no earlier device to compare the first sign-in with, so with
    alerts on by default it would otherwise mail every new account."""
    sent: list[dict] = []
    monkeypatch.setattr(
        "app.auth.service.send_new_device_signin_alert",
        lambda *a, **k: sent.append(k),
    )

    email = f"first-{uuid.uuid4().hex[:12]}@example.com"
    reg = await client.post(
        "/auth/register",
        json={"email": email, "password": DEFAULT_PASSWORD, "full_name": "First"},
    )
    assert reg.status_code == 201, reg.text
    first = await client.post(
        "/auth/login",
        json={"email": email, "password": DEFAULT_PASSWORD},
        headers={"user-agent": EDGE_WINDOWS},
    )
    assert first.status_code == 200, first.text
    assert sent == [], "the account's first sign-in is not a new device"

    # A second, different device is new -> alert.
    await client.post(
        "/auth/login",
        json={"email": email, "password": DEFAULT_PASSWORD},
        headers={"user-agent": CHROME},
    )
    assert len(sent) == 1, sent


# --- admin self-service -----------------------------------------------------


async def test_admin_can_toggle_their_own_signin_alerts(client, make_admin):
    """Admins are users: the security-settings endpoints have no role gate, so
    the /admin/security tab (which reuses them) works for an admin's own
    account exactly as /dashboard/security does for everyone else."""
    admin = await make_admin()
    resp = await client.get(
        "/users/me/security-preferences", headers=auth_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["signin_alerts_enabled"] is True

    off = await client.patch(
        "/users/me/security-preferences",
        json={"signin_alerts_enabled": False},
        headers=auth_headers(admin),
    )
    assert off.status_code == 200, off.text
    assert off.json()["signin_alerts_enabled"] is False

    events = (
        await client.get("/auth/security-events", headers=auth_headers(admin))
    ).json()
    assert any(e["action"] == "SIGNIN_ALERTS_CHANGED" for e in events), events


async def test_admin_sees_their_own_sessions_and_events(client, make_admin):
    admin = await make_admin()
    sessions = (
        await client.get("/auth/sessions", headers=auth_headers(admin))
    ).json()
    assert len(sessions) >= 1, "the admin's own sign-in must appear"

    events = (
        await client.get("/auth/security-events", headers=auth_headers(admin))
    ).json()
    assert len(events) >= 1, "the admin's own security history must appear"


async def test_security_events_pagination(client, make_admin):
    admin = await make_admin()
    resp_limit = await client.get(
        "/auth/security-events?limit=1", headers=auth_headers(admin)
    )
    assert resp_limit.status_code == 200
    assert len(resp_limit.json()) <= 1

    resp_offset = await client.get(
        "/auth/security-events?limit=1&offset=1000", headers=auth_headers(admin)
    )
    assert resp_offset.status_code == 200
    assert len(resp_offset.json()) == 0


async def test_security_overview_consolidates_security_screen_reads(client, make_user):
    user = await make_user()
    resp = await client.get("/auth/security-overview?limit=5", headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["second_factor_required"] is False
    assert data["two_factor"]["enabled"] is False
    assert data["passkeys"] == []
    assert len(data["sessions"]) >= 1
    assert any(s["current"] for s in data["sessions"])
    assert len(data["security_events"]) >= 1
    assert len(data["security_events"]) <= 5
    assert data["security_preferences"] == {"signin_alerts_enabled": True}


