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
