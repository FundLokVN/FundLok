"""D-047 / T5 (Appendix M M11): a suspended account takes no new offers or investments, but repayment keeps working."""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import RepaymentDay
from tests.market.test_funding_flow import _reserve
from tests.market.test_round_lifecycle import _approved, _open_project


async def _suspend(client, admin, user):
    resp = await client.patch(
        f"/admin/users/{user['id']}/status", json={"status": "SUSPENDED"}, headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text


async def test_suspended_account_can_still_repay(client, db_session, make_admin, make_user, make_eligible_investor):
    setup = await _open_project(client, db_session, make_admin, make_user)
    admin, sme, window_id = setup["admin"], setup["sme"], setup["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Nguyen Van An"))
    await _approved(client, admin, investor, window_id, 50_000_000)
    await client.post(f"/funding/windows/{window_id}/close-early", headers=sme["headers"])
    advanced = await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=admin["headers"])
    assert advanced.status_code == 200, advanced.text
    start = dt.date.fromisoformat(advanced.json()["repayment_start_date"])

    await _suspend(client, admin, sme)
    await _suspend(client, admin, investor)

    # Everything else is shut: a suspended account cannot even read its profile, and takes no new investment.
    assert (await client.get("/users/me", headers=sme["headers"])).status_code == 403
    assert (await client.get("/users/me", headers=investor["headers"])).status_code == 403
    assert (await _reserve(client, investor, window_id, 10_000_000)).status_code == 403

    # The business still sees what it owes and pays its lenders.
    due = await client.get("/repayment/today", params={"date": start.isoformat()}, headers=sme["headers"])
    assert due.status_code == 200, due.text
    day_ids = [r["repayment_day_id"] for r in due.json()["rows"]]
    assert day_ids
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(day_ids)))).scalars():
        day.due_date = ict_date(dt.datetime.now(dt.timezone.utc))
    await db_session.commit()
    paid = await client.post(
        "/repayment/days/mark-paid", json={"repayment_day_ids": day_ids},
        headers=sme["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
    )
    assert paid.status_code == 200, paid.text

    # The suspended lender can still report a missing day and read receipts.
    reported = await client.post(
        f"/repayment/days/{day_ids[0]}/report-missing", json={"statement": "Nothing arrived today"}, headers=investor["headers"]
    )
    assert reported.status_code == 200, reported.text
    assert (await client.get("/repayment/receipts", headers=investor["headers"])).status_code == 200
