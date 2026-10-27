"""Maintenance mode switch (PUT /system/maintenance).

Regression: an actual toggle (enabled changes, so an UPDATE is emitted) used
to 500 with MissingGreenlet. The UPDATE expires the server-set `updated_at`,
and rebuilding the response from the still-cached row lazy-loaded it outside
the async context. The change itself had already been committed.
"""


async def test_system_admin_can_toggle_maintenance_on_and_off(client, make_system_admin):
    sysadmin = await make_system_admin()

    # First save inserts the row.
    resp = await client.put(
        "/system/maintenance", json={"enabled": True, "message": "Back soon"}, headers=sysadmin["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True

    # Second save changes the row, so an UPDATE is emitted.
    resp = await client.put("/system/maintenance", json={"enabled": False}, headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["message"] is None
    assert body["updated_at"] is not None
    assert body["updated_by"] == sysadmin["id"]


async def _set_maintenance(client, sysadmin, enabled: bool):
    resp = await client.put(
        "/system/maintenance", json={"enabled": enabled, "message": "Back soon"}, headers=sysadmin["headers"]
    )
    assert resp.status_code == 200, resp.text


async def test_maintenance_never_blocks_a_system_admin(client, make_system_admin):
    # The admin who switched it on must keep a working panel: its pages call
    # routes outside the gate's allowlist, /users/me first.
    sysadmin = await make_system_admin()
    await _set_maintenance(client, sysadmin, True)
    try:
        resp = await client.get("/users/me", headers=sysadmin["headers"])
        assert resp.status_code == 200, resp.text
    finally:
        # Also clears the flag's in-process cache for the tests after this one.
        await _set_maintenance(client, sysadmin, False)


async def test_maintenance_still_blocks_everyone_else(client, make_system_admin, make_admin, make_user):
    sysadmin = await make_system_admin()
    admin = await make_admin()
    sme = await make_user(role="SME")
    await _set_maintenance(client, sysadmin, True)
    try:
        for headers in (admin["headers"], sme["headers"], {}):
            resp = await client.get("/users/me", headers=headers)
            assert resp.status_code == 503, resp.text
            assert resp.json() == {"detail": "Back soon"}
    finally:
        await _set_maintenance(client, sysadmin, False)
