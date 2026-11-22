"""Platform-wide funding analytics on the admin overview (GET /admin/funding/analytics; app/admin/analytics.py).

Every account at once: what investors have put into businesses and what businesses have repaid them, with the
last 12 months of each. An investment counts once the business has received it; a repayment day counts once the
business marked it paid; a day is late on the late-repayments list's rule. Totals only, no names or accounts.
"""
import datetime as dt

from sqlalchemy import select

from app.admin.analytics import trend_months
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.models import RepaymentDay, Tranche
from tests.admin.test_overdue_repayments import _due_on, _working_days_before
from tests.admin.test_repayment_receipts import _pay, _repaying


async def _analytics(client, admin):
    resp = await client.get("/admin/funding/analytics", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def _today() -> dt.date:
    return ict_date(dt.datetime.now(dt.timezone.utc))


def _this_month(monthly: list[dict]) -> int:
    assert monthly[-1]["month"] == _today().strftime("%Y-%m")
    return monthly[-1]["amount_vnd"]


def test_the_trend_is_the_last_twelve_months_ending_with_this_one():
    months = trend_months(dt.date(2026, 11, 21))
    assert len(months) == 12 and months[0] == "2025-12" and months[-1] == "2026-11"
    assert trend_months(dt.date(2026, 1, 5))[:2] == ["2025-02", "2025-03"]


async def test_an_empty_platform_has_zero_totals_and_twelve_empty_months(client, make_admin, make_user):
    admin = await make_admin()
    data = await _analytics(client, admin)

    assert data["as_of"] == _today().isoformat()
    inv, rep = data["investments"], data["repayments"]
    assert {k: v for k, v in inv.items() if k != "monthly"} == {
        "invested_vnd": 0, "investments": 0, "investors": 0, "businesses": 0, "in_progress_vnd": 0, "in_progress": 0,
    }
    assert {k: v for k, v in rep.items() if k != "monthly"} == {
        "repaid_vnd": 0, "principal_repaid_vnd": 0, "outstanding_principal_vnd": 0,
        "days_due": 0, "days_on_time": 0, "late_days": 0, "late_tickets": 0,
    }
    assert [m["month"] for m in inv["monthly"]] == trend_months(_today()) == [m["month"] for m in rep["monthly"]]
    assert all(m["amount_vnd"] == 0 for m in inv["monthly"] + rep["monthly"])

    # Staff only.
    sme = await make_user(role="SME")
    assert (await client.get("/admin/funding/analytics", headers=sme["headers"])).status_code == 403


async def test_a_received_investment_counts_and_is_all_outstanding(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    data = await _analytics(client, s["admin"])

    inv, rep = data["investments"], data["repayments"]
    assert inv["invested_vnd"] == 30_000_000 and inv["investments"] == 1
    assert inv["investors"] == 1 and inv["businesses"] == 1
    assert inv["in_progress"] == 0 and inv["in_progress_vnd"] == 0
    assert _this_month(inv["monthly"]) == 30_000_000  # received today
    assert rep["outstanding_principal_vnd"] == 30_000_000 and rep["repaid_vnd"] == 0
    assert rep["days_due"] == 0 and rep["late_days"] == 0  # the first day is due today: not late yet


async def test_an_investment_still_being_checked_is_in_progress_not_received(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    ticket = await db_session.get(Tranche, s["ticket"]["id"])
    ticket.status = "UNDER_REVIEW"
    await db_session.commit()

    inv = (await _analytics(client, s["admin"]))["investments"]

    assert inv["invested_vnd"] == 0 and inv["investments"] == 0
    assert inv["in_progress_vnd"] == 30_000_000 and inv["in_progress"] == 1


async def test_a_late_day_counts_once_per_due_date_until_the_business_pays_it(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    # Two rows sharing one due date, like the first instalment and the first-period interest (which only exists
    # when the schedule has stub days, so it is set up here rather than relied on).
    rows = (
        await db_session.execute(
            select(RepaymentDay)
            .where(RepaymentDay.tranche_id == s["ticket"]["id"])
            .order_by(RepaymentDay.due_date, RepaymentDay.day_index)
        )
    ).scalars().all()
    s = s | {"day_ids": [str(day.id) for day in rows[:2]]}
    due = _working_days_before(_today(), 1)
    await _due_on(db_session, s["day_ids"], due)

    rep = (await _analytics(client, s["admin"]))["repayments"]
    assert rep["late_days"] == 1 and rep["late_tickets"] == 1
    assert rep["days_due"] == 1 and rep["days_on_time"] == 0

    await _pay(client, s)
    days = (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(s["day_ids"])))).scalars().all()
    rep = (await _analytics(client, s["admin"]))["repayments"]

    assert rep["late_days"] == 0 and rep["late_tickets"] == 0
    assert rep["repaid_vnd"] == sum(d.gross_vnd for d in days)
    assert rep["principal_repaid_vnd"] == sum(d.principal_vnd for d in days)
    assert rep["outstanding_principal_vnd"] == 30_000_000 - rep["principal_repaid_vnd"]
    # Paid without ever being marked late (the cut-off job did not run here): on time.
    assert rep["days_due"] == 1 and rep["days_on_time"] == 1
    paid_month = next(m for m in rep["monthly"] if m["month"] == due.strftime("%Y-%m"))
    assert paid_month["amount_vnd"] == rep["repaid_vnd"]


async def test_a_day_paid_after_being_marked_late_is_not_on_time(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    due = _working_days_before(_today(), 1)
    await _due_on(db_session, s["day_ids"], due)
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(s["day_ids"])))).scalars():
        day.status, day.late_marked_at = "LATE", dt.datetime.combine(due, dt.time(18), tzinfo=dt.timezone.utc)
    await db_session.commit()
    await _pay(client, s)

    rep = (await _analytics(client, s["admin"]))["repayments"]

    assert rep["late_days"] == 0 and rep["days_due"] == 1 and rep["days_on_time"] == 0
    assert rep["repaid_vnd"] > 0
