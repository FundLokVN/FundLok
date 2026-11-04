"""The admin Transactions tab's repayments: the receipts businesses upload when they repay an investor.

The business pays the investor directly and uploads its bank receipt, which marks the days paid; the investor
confirms it arrived (tests/market/test_repayment_receipt_confirmation.py). Staff can read every receipt, each view
audited, but no longer approve or deny one.
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import RepaymentDay
from app.lending.models import AuditLog
from tests.market.test_funding_flow import PNG, _open_project
from tests.market.test_round_lifecycle import _approved


async def _repaying(client, db_session, make_admin, make_user, make_eligible_investor):
    """A repaying investment whose first day (its instalment and the first-period interest) is due today."""
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Nguyen Van An"))
    ticket = await _approved(client, setup["admin"], investor, setup["window"].id, 30_000_000)
    schedule = (
        await client.get(f"/repayment/tranches/{ticket['id']}/schedule", headers=setup["sme"]["headers"])
    ).json()
    first_ids = [r["repayment_day_id"] for r in schedule if r["due_date"] == schedule[0]["due_date"]]
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(first_ids)))).scalars():
        day.due_date = ict_date(dt.datetime.now(dt.timezone.utc))
    await db_session.commit()
    return setup | {"investor": investor, "ticket": ticket, "day_ids": first_ids}


async def _pay(client, s):
    """The business uploads a receipt for what is due on the investment."""
    resp = await client.post(
        f"/repayment/tranches/{s['ticket']['id']}/pay",
        data={"repayment_day_ids": s["day_ids"]},
        files={"file": ("receipt.png", PNG + uuid.uuid4().bytes, "image/png")},
        headers=s["sme"]["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text


async def _receipts(client, admin, **params):
    resp = await client.get("/admin/funding/repayments", params=params, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _days(db_session, ids):
    db_session.expire_all()
    return (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(ids)))).scalars().all()


async def test_staff_see_the_business_s_receipt_with_the_days_it_pays(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    assert (await _receipts(client, s["admin"]))["items"] == []
    await _pay(client, s)

    page = await _receipts(client, s["admin"])

    [receipt] = page["items"]
    # Waiting on the investor's confirmation, not on staff.
    assert receipt["check_status"] == "AWAITING_FIELDS" and receipt["needs_action"] is True
    assert sorted(d["repayment_day_id"] for d in receipt["days"]) == sorted(s["day_ids"])
    assert receipt["net_vnd"] == sum(d["net_vnd"] for d in receipt["days"])
    assert receipt["net_vnd"] == receipt["gross_vnd"] - receipt["tax_withheld_vnd"]
    assert receipt["reference_code"] == s["ticket"]["reference_code"]
    assert receipt["investor"]["full_name"] == "Nguyen Van An" and receipt["business"]["id"] == s["sme"]["id"]
    assert receipt["payee"]["account_number"]
    assert (await _receipts(client, s["admin"], status="DECIDED"))["items"] == []

    # Opening the receipt is a 10-minute link, and audited.
    monkeypatch.setattr("app.utils.r2.presign_get", lambda key, **kw: f"https://r2.test/{key}")
    link = await client.get(f"/admin/funding/repayments/{receipt['id']}/receipt-url", headers=s["admin"]["headers"])
    assert link.status_code == 200 and link.json()["url"].startswith("https://r2.test/evidence/RECEIPT/")
    viewed = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "REPAYMENT_RECEIPT_VIEWED"))
    ).scalar_one()
    assert viewed.after_state["tranche_id"] == s["ticket"]["id"]

    # Staff only.
    assert (await client.get("/admin/funding/repayments", headers=s["sme"]["headers"])).status_code == 403


async def test_staff_no_longer_approve_or_deny_a_repayment_receipt(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    [receipt] = (await _receipts(client, s["admin"]))["items"]

    resp = await client.post(
        f"/admin/funding/repayments/{receipt['id']}/decision", json={"outcome": "APPROVED"},
        headers=s["admin"]["headers"],
    )

    assert resp.status_code in (404, 405)
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"PRESUMED_PAID"}


async def test_a_receipt_the_investor_confirmed_shows_as_decided_for_staff(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    [receipt] = (await _receipts(client, s["admin"]))["items"]

    confirmed = await client.post(f"/repayment/receipts/{receipt['id']}/confirm", headers=s["investor"]["headers"])

    assert confirmed.status_code == 200, confirmed.text
    assert (await _receipts(client, s["admin"]))["items"] == []
    [decided] = (await _receipts(client, s["admin"], status="DECIDED"))["items"]
    assert decided["id"] == receipt["id"] and decided["check_status"] == "MATCHED"
    assert decided["needs_action"] is False
    # The Activity tab shows who confirmed it, and for how much.
    activity = (
        await client.get("/admin/funding/activity", params={"group": "INVESTOR"}, headers=s["admin"]["headers"])
    ).json()["items"]
    [entry] = [a for a in activity if a["action"] == "REPAYMENT_RECEIPT_CONFIRMED"]
    assert entry["actor"]["id"] == s["investor"]["id"] and entry["amount_vnd"] == receipt["net_vnd"]
    assert entry["tranche_id"] == s["ticket"]["id"]
