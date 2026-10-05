"""POST /auth/reset-password — the change lands, and the owner is told.

This is the flow the profile page's "Change password" button sends users to, so
it's where a real password change happens. The notice exists so a reset the
owner didn't request is visible to them while they can still act on it.
"""
NEW_PASSWORD = "Reset-Me-Passw0rd!"


async def test_reset_password_emails_the_owner(client, make_user, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        "app.auth.service.send_password_changed_notice",
        lambda to_email: sent.append(to_email),
    )

    user = await make_user()
    # The link the user would get by email: the only way to mint a token that
    # reset_password accepts (it is tied to the current password).
    links: list[str] = []
    monkeypatch.setattr(
        "app.auth.service.send_password_reset_email",
        lambda to_email, token: links.append(token),
    )
    await client.post("/auth/forgot-password", json={"email": user["email"]})
    [token] = links

    resp = await client.post(
        "/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    assert sent == [user["email"]]

    # The notice describes something that actually happened.
    login = await client.post(
        "/auth/login", json={"email": user["email"], "password": NEW_PASSWORD}
    )
    assert login.status_code == 200


async def test_a_failed_reset_sends_no_email(client, monkeypatch):
    """A bogus token changes nothing, so it must not fire a 'your password
    changed' alert -- false alarms are what train people to ignore the real
    one."""
    sent: list[str] = []
    monkeypatch.setattr(
        "app.auth.service.send_password_changed_notice",
        lambda to_email: sent.append(to_email),
    )

    resp = await client.post(
        "/auth/reset-password",
        json={"token": "not-a-real-token", "new_password": NEW_PASSWORD},
    )
    assert resp.status_code >= 400
    assert sent == []
