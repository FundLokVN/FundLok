"""The admin Transactions tab's repayments: the receipts businesses upload when they repay an investor.

The business pays the investor directly and uploads its bank receipt, which marks the days paid; staff look at the
receipt and approve it (the days are matched and settled) or deny it with a note (the days are due again, and the
business is told why and pays and uploads again).
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import Dispute, FundingWindow, RepaymentDay
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
    assert receipt["check_status"] == "AWAITING_FIELDS" and receipt["needs_action"] is True
    assert sorted(d["repayment_day_id"] for d in receipt["days"]) == sorted(s["day_ids"])
    assert receipt["net_vnd"] == sum(d["net_vnd"] for d in receipt["days"])
    assert receipt["net_vnd"] == receipt["gross_vnd"] - receipt["tax_withheld_vnd"]
    assert receipt["reference_code"] == s["ticket"]["reference_code"]
    assert receipt["investor"]["full_name"] == "Nguyen Van An" and receipt["business"]["id"] == s["sme"]["id"]
    # What staff check the receipt against: the investor's account the money should reach.
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


async def test_denying_a_receipt_puts_its_days_due_again_and_tells_the_business_why(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    [receipt] = (await _receipts(client, s["admin"]))["items"]
    decide = lambda body: client.post(
        f"/admin/funding/repayments/{receipt['id']}/decision", json=body, headers=s["admin"]["headers"]
    )

    # A denial needs a note: the business reads it.
    blank = await decide({"outcome": "DENIED", "reason": "  "})
    assert blank.status_code == 422 and blank.headers["X-Error-Code"] == "REASON_REQUIRED"

    denied = await decide({"outcome": "DENIED", "reason": "The amount on the receipt is 100,000"})
    assert denied.status_code == 200, denied.text
    assert denied.json()["check_status"] == "REJECTED" and denied.json()["needs_action"] is False
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"DUE"}

    # The business is told, and pays again from the investment's page.
    bell = (await client.get("/notifications", headers=s["sme"]["headers"])).json()["items"]
    [notice] = [n for n in bell if n["event"] == "REPAYMENT_RECEIPT_REJECTED"]
    assert notice["data"]["reason"] == "The amount on the receipt is 100,000"
    assert notice["data"]["tranche_id"] == s["ticket"]["id"]
    due = (await client.get(f"/repayment/tranches/{s['ticket']['id']}/due", headers=s["sme"]["headers"])).json()
    assert sorted(r["repayment_day_id"] for r in due["days"]) == sorted(s["day_ids"])
    assert due["rejection"]["reason"] == "The amount on the receipt is 100,000"

    # Decided once.
    again = await decide({"outcome": "APPROVED"})
    assert again.status_code == 409 and again.headers["X-Error-Code"] == "NOTHING_TO_DECIDE"

    # The audit trail names the admin, the amount repaid and why.
    activity = (
        await client.get("/admin/funding/activity", params={"group": "DECISIONS"}, headers=s["admin"]["headers"])
    ).json()["items"]
    [entry] = [a for a in activity if a["action"] == "REPAYMENT_RECEIPT_DENIED"]
    assert entry["actor"]["id"] == s["admin"]["id"] and entry["reason"] == "The amount on the receipt is 100,000"
    assert entry["amount_vnd"] == receipt["net_vnd"] and entry["tranche_id"] == s["ticket"]["id"]
    marked = (
        await client.get("/admin/funding/activity", params={"group": "BUSINESS"}, headers=s["admin"]["headers"])
    ).json()["items"]
    assert [a["action"] for a in marked] == ["REPAYMENT_DAYS_MARKED_PAID"]


async def test_approving_a_receipt_matches_and_settles_the_days(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    [receipt] = (await _receipts(client, s["admin"]))["items"]

    approved = await client.post(
        f"/admin/funding/repayments/{receipt['id']}/decision", json={"outcome": "APPROVED"},
        headers=s["admin"]["headers"],
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["check_status"] == "MATCHED"
    days = await _days(db_session, s["day_ids"])
    assert {d.status for d in days} == {"MATCHED"} and all(d.settled_at for d in days)
    assert all(d.transfer_id for d in days if d.net_vnd > 0)
    assert [r["id"] for r in (await _receipts(client, s["admin"], status="DECIDED"))["items"]] == [receipt["id"]]
    due = (await client.get(f"/repayment/tranches/{s['ticket']['id']}/due", headers=s["sme"]["headers"])).json()
    assert due["days"] == [] and due["rejection"] is None
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "REPAYMENT_RECEIPT_APPROVED"))
    ).scalar_one()
    assert audit.after_state["reason"] == "Approved after viewing the receipt"


async def test_approving_settles_a_day_the_investor_reported_as_paid(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    reported = await client.post(
        f"/repayment/days/{s['day_ids'][0]}/report-missing", json={"statement": "Nothing arrived"},
        headers=s["investor"]["headers"],
    )
    assert reported.status_code == 200, reported.text
    [receipt] = (await _receipts(client, s["admin"]))["items"]
    assert receipt["open_report"] is True

    approved = await client.post(
        f"/admin/funding/repayments/{receipt['id']}/decision", json={"outcome": "APPROVED", "reason": "Receipt shows it"},
        headers=s["admin"]["headers"],
    )

    assert approved.status_code == 200, approved.text
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"MATCHED"}
    db_session.expire_all()
    dispute = (await db_session.execute(select(Dispute).where(Dispute.repayment_day_id == s["day_ids"][0]))).scalar_one()
    assert dispute.status == "DECIDED" and dispute.outcome == "PAID"


async def test_staff_do_not_decide_a_repayment_they_are_part_of(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    await _pay(client, s)
    [receipt] = (await _receipts(client, s["admin"]))["items"]
    db_session.expire_all()
    window = await db_session.get(FundingWindow, uuid.UUID(s["ticket"]["window_id"]))
    window.sme_user_id = uuid.UUID(s["admin"]["id"])  # staff who are the business repaying
    await db_session.commit()

    resp = await client.post(
        f"/admin/funding/repayments/{receipt['id']}/decision", json={"outcome": "APPROVED"},
        headers=s["admin"]["headers"],
    )

    assert resp.status_code == 403
