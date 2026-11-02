"""A funding round from investment to daily repayment, over the API.

Investors pay the business directly and staff approve each transfer, which means the money is received; the business
sees its investors (masked); it closes the round early once the minimum is confirmed; staff advance the round, which
forms the loan and creates the daily repayment days; the business then pays each investor every working day, one
VietQR per investor, and marks the days paid.
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import FundingWindow, RepaymentDay
from app.lending.models import AuditLog
from tests.market.test_funding_flow import PNG, _open_project, _reserve, _upload


async def _approved(client, admin, investor, window_id, amount):
    """A ticket the investor paid and staff approved by looking at the receipt: received (CONFIRMED)."""
    ticket = (await _reserve(client, investor, window_id, amount)).json()
    upload = (await _upload(client, investor, ticket["id"], PNG + uuid.uuid4().bytes)).json()
    resp = await client.post(
        f"/admin/funding/transfers/{upload['transfer_id']}/decision",
        json={"outcome": "APPROVED"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200 and resp.json()["status"] == "CONFIRMED", resp.text
    return ticket


async def _round(client, sme):
    resp = await client.get("/funding/me/rounds", headers=sme["headers"])
    assert resp.status_code == 200, resp.text
    [round_] = resp.json()
    return round_


async def test_a_round_goes_from_investment_to_daily_repayment(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)  # target 80M, minimum 40M
    admin, sme = setup["admin"], setup["sme"]
    window_id = setup["window"].id
    an = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Nguyen Van An"))
    binh = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Tran Thi Binh"))
    first = await _approved(client, admin, an, window_id, 30_000_000)

    # The business sees its round and each investor, masked; staff approval already counts as received.
    round_ = await _round(client, sme)
    assert round_["id"] == str(window_id) and round_["status"] == "OPEN"
    assert round_["project_name"].startswith("ABC Co") and round_["confirmed_vnd"] == 30_000_000
    [ticket] = round_["tickets"]
    assert ticket["investor_display"] == "N. A***" and ticket["status"] == "CONFIRMED"

    # Too early to close: 30M confirmed, the minimum is 40M.
    early = await client.post(f"/funding/windows/{window_id}/close-early", headers=sme["headers"])
    assert early.status_code == 409 and early.headers["X-Error-Code"] == "BELOW_MIN_RAISE_FOR_EARLY_CLOSE"

    second = await _approved(client, admin, binh, window_id, 20_000_000)
    assert (await _round(client, sme))["confirmed_vnd"] == 50_000_000

    closing = await client.post(f"/funding/windows/{window_id}/close-early", headers=sme["headers"])
    assert closing.status_code == 200 and closing.json()["status"] == "CLOSING", closing.text

    # Staff advance it: nothing is pending, so the loan forms and the daily days are created.
    advanced = await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=admin["headers"])
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["status"] == "REPAYING" and advanced.json()["repayment_start_date"]
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "FUNDING_ROUND_ADVANCED"))).scalar_one()
    assert audit.before_state == {"status": "CLOSING"} and audit.after_state["status"] == "REPAYING"
    round_ = await _round(client, sme)
    assert round_["status"] == "REPAYING"
    assert {t["status"] for t in round_["tickets"]} == {"ACTIVE"}

    # The first day's payments: one row per investor, each with its own VietQR for the exact amount.
    start = dt.date.fromisoformat(advanced.json()["repayment_start_date"])
    due = (
        await client.get("/repayment/today", params={"date": start.isoformat()}, headers=sme["headers"])
    ).json()
    assert due["rows"] and {r["tranche_id"] for r in due["rows"]} == {first["id"], second["id"]}
    assert all(r["vietqr_payload"] and r["status"] == "DUE" for r in due["rows"] if r["net_vnd"] > 0)
    assert due["totals"]["net_vnd"] == sum(r["net_vnd"] for r in due["rows"])

    # A day not yet due cannot be marked paid; one due today can.
    day_ids = [r["repayment_day_id"] for r in due["rows"]]
    if start > ict_date(dt.datetime.now(dt.timezone.utc)):
        not_yet = await client.post(
            "/repayment/days/mark-paid", json={"repayment_day_ids": day_ids[:1]},
            headers=sme["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
        )
        assert not_yet.status_code == 409 and not_yet.headers["X-Error-Code"] == "NOT_DUE_YET"
        db_session.expire_all()
        for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(day_ids)))).scalars():
            day.due_date = ict_date(dt.datetime.now(dt.timezone.utc))
        await db_session.commit()
    paid = await client.post(
        "/repayment/days/mark-paid", json={"repayment_day_ids": day_ids},
        headers=sme["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
    )
    assert paid.status_code == 200, paid.text
    assert {r["status"] for r in paid.json()} == {"PRESUMED_PAID"} and len(paid.json()) == len(day_ids)

    # Each investor sees their own schedule; another investor does not.
    schedule = await client.get(f"/repayment/tranches/{first['id']}/schedule", headers=an["headers"])
    assert schedule.status_code == 200 and len(schedule.json()) > 1
    assert (await client.get(f"/repayment/tranches/{first['id']}/schedule", headers=binh["headers"])).status_code == 404

    # The investor's list names the project; their schedule shows the day the business marked paid.
    mine = (await client.get("/funding/me/tranches", headers=an["headers"])).json()
    assert mine[0]["status"] == "ACTIVE" and mine[0]["project_name"].startswith("ABC Co")
    [paid_day] = [r for r in paid.json() if r["tranche_id"] == first["id"]][:1]
    assert paid_day["status"] == "PRESUMED_PAID"

    # It never arrived: the investor reports it, and it stops counting as paid.
    reported = await client.post(
        f"/repayment/days/{paid_day['repayment_day_id']}/report-missing",
        json={"statement": "Nothing arrived today"},
        headers=an["headers"],
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["status"] == "REPORTED_MISSED"
    again = await client.post(
        f"/repayment/days/{paid_day['repayment_day_id']}/report-missing", json={}, headers=an["headers"]
    )
    assert again.status_code == 409 and again.headers["X-Error-Code"] == "ALREADY_REPORTED"
    other = await client.post(
        f"/repayment/days/{paid_day['repayment_day_id']}/report-missing", json={}, headers=binh["headers"]
    )
    assert other.status_code == 404
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "REPAYMENT_DAY_REPORTED_MISSING"))
    ).scalar_one()
    assert str(audit.actor_id) == an["id"] and audit.after_state["statement"] == "Nothing arrived today"


async def test_only_the_business_sees_and_acts_on_its_round(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    await _approved(client, setup["admin"], investor, setup["window"].id, 10_000_000)
    other_sme = await make_user(role="SME")

    assert (await client.get("/funding/me/rounds", headers=other_sme["headers"])).json() == []
    assert (
        await client.post(f"/funding/windows/{setup['window'].id}/close-early", headers=other_sme["headers"])
    ).status_code == 404
    assert (await client.get("/funding/me/rounds", headers=investor["headers"])).status_code == 403
    assert (await client.get("/repayment/today", headers=investor["headers"])).status_code == 403
    assert (
        await client.post(f"/admin/funding/rounds/{setup['window'].id}/advance", headers=setup["sme"]["headers"])
    ).status_code == 403


async def test_advancing_a_round_with_nothing_due_changes_nothing(
    client, db_session, make_admin, make_user
):
    setup = await _open_project(client, db_session, make_admin, make_user)

    resp = await client.post(f"/admin/funding/rounds/{setup['window'].id}/advance", headers=setup["admin"]["headers"])

    assert resp.status_code == 200 and resp.json()["status"] == "OPEN"
    db_session.expire_all()
    assert (await db_session.execute(select(FundingWindow.status))).scalar_one() == "OPEN"
