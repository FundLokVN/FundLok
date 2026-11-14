"""GET /admin/overview shows each user's mobile and whether it was proven by
a one-time code, so staff can see who still owes the phone step
(docs/specs/users/phone-verification.md)."""
import datetime as dt
from uuid import UUID

from conftest import auth_headers
from app.users.models import User


async def _set_phone(db_session, user: dict, phone: str | None, verified: bool) -> None:
    row = await db_session.get(User, UUID(user["id"]))
    row.phone = phone
    row.phone_verified_at = dt.datetime(2026, 11, 12, 3, 0, tzinfo=dt.timezone.utc) if verified else None
    await db_session.commit()


async def test_the_users_table_shows_each_phone_and_whether_it_is_verified(
    client, db_session, make_admin, make_user
):
    admin = await make_admin()
    verified = await make_user(role="SME")
    unverified = await make_user(role="INVESTOR")
    missing = await make_user(role="INVESTOR")
    await _set_phone(db_session, verified, "+84912345678", verified=True)
    await _set_phone(db_session, unverified, "+84987654321", verified=False)
    await _set_phone(db_session, missing, None, verified=False)

    resp = await client.get(
        "/admin/overview", params={"page_size": 100}, headers=auth_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    rows = {row["id"]: row for row in resp.json()["table"]["items"]}

    assert rows[verified["id"]]["phone"] == "+84912345678"
    assert rows[verified["id"]]["phone_verified_at"].startswith("2026-11-12T03:00")
    assert rows[unverified["id"]]["phone"] == "+84987654321"
    assert rows[unverified["id"]]["phone_verified_at"] is None
    assert rows[missing["id"]]["phone"] is None
    assert rows[missing["id"]]["phone_verified_at"] is None
