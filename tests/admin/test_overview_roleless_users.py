"""GET /admin/overview with users who have not picked a role yet.

Registration no longer assigns a role (migration f2b3c4d5e6f7) -- a user has
`role = NULL` from signup until they choose at /select-role. That is the normal
state of every fresh account, so production accumulates them immediately.

Two schema fields still assumed a role was always present, and both 500'd the
endpoint rather than degrading:

  * `AdminStats.users_by_role` is `dict[str, int]`, and `group_by(User.role)`
    yields a None key. get_stats() runs in BOTH modes, so this took the whole
    overview down, not just the users table.
  * `UserRow.role` was `str`, so listing such a user failed validation too.

These are regression tests for a live production 500.
"""

from conftest import auth_headers


async def test_overview_survives_a_user_with_no_role(client, make_admin, make_user):
    """The bug: one role-less user 500'd the entire endpoint."""
    admin = await make_admin()
    await make_user(role=None)

    resp = await client.get("/admin/overview", headers=auth_headers(admin))

    assert resp.status_code == 200, resp.text


async def test_roleless_users_are_counted_under_unassigned(
    client, make_admin, make_user
):
    """Counted under a real bucket, not silently dropped -- "signed up but never
    chose a role" is a number an operator needs."""
    admin = await make_admin()
    await make_user(role=None)
    await make_user(role="SME")

    body = (await client.get("/admin/overview", headers=auth_headers(admin))).json()
    by_role = body["stats"]["users_by_role"]

    assert by_role.get("UNASSIGNED", 0) >= 1
    assert by_role.get("SME", 0) >= 1
    # Every user is accounted for somewhere.
    assert sum(by_role.values()) == body["stats"]["total_users"]


async def test_projects_mode_also_survives(client, make_admin, make_user):
    """get_stats() runs in both modes, so the projects table went down too --
    which is what the production log showed."""
    admin = await make_admin()
    await make_user(role=None)

    resp = await client.get(
        "/admin/overview",
        params={"mode": "projects", "page": 1, "page_size": 14},
        headers=auth_headers(admin),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "projects"


async def test_a_roleless_user_can_be_listed(client, make_admin, make_user):
    """UserRow.role was non-optional, so the row itself failed to serialise."""
    admin = await make_admin()
    target = await make_user(role=None)

    body = (
        await client.get(
            "/admin/overview",
            params={"mode": "users", "page": 1, "page_size": 50},
            headers=auth_headers(admin),
        )
    ).json()

    rows = {row["email"]: row for row in body["table"]["items"]}
    assert target["email"] in rows
    assert rows[target["email"]]["role"] is None
