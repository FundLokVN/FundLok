"""Late repayments in the admin Transactions tab: a day the business has not marked paid after its due date.

The business pays each investor directly and uploads a receipt, which marks the days paid (DUE -> PRESUMED_PAID).
A day still DUE once its due date has passed is late. Display only (no arrears state, MVP scope): staff see which
tickets are late, by how many days and working days, and for how much.
"""
import datetime as dt
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.admin.repayments import is_overdue, overdue_summary
from app.direct_transfer.calendar_utils import count_business_days, default_calendar, ict_date
from app.direct_transfer.models import FundingWindow, RepaymentDay, Tranche
from tests.admin.test_repayment_receipts import _pay, _receipts, _repaying

MON = dt.date(2026, 11, 9)
TUE = dt.date(2026, 11, 10)
WED = dt.date(2026, 11, 11)
FRI = dt.date(2026, 11, 13)
NEXT_MON = dt.date(2026, 11, 16)


def _day(due_date, status="DUE", net_vnd=100_000, settled_at=None):
    return SimpleNamespace(due_date=due_date, status=status, net_vnd=net_vnd, settled_at=settled_at)


# --------------------------------------------------------------------------- #
# what counts as late
# --------------------------------------------------------------------------- #
def test_a_day_is_not_late_on_its_own_due_date():
    # No cut-off hour: the business may pay at any time on the day.
    assert not is_overdue(_day(MON), MON)
    assert overdue_summary([_day(MON)], MON).days == 0


def test_a_day_still_due_the_next_day_is_one_working_day_late():
    late = overdue_summary([_day(MON, net_vnd=125_080)], TUE)

    assert late.days == 1 and late.working_days_late == 1
    assert late.oldest_due_date == MON and late.net_vnd == 125_080


def test_the_weekend_does_not_count_as_working_days_late():
    assert overdue_summary([_day(FRI)], NEXT_MON).working_days_late == 1
    assert overdue_summary([_day(FRI)], NEXT_MON + dt.timedelta(days=1)).working_days_late == 2


def test_working_days_late_run_from_the_oldest_late_day_and_amounts_add_up():
    late = overdue_summary([_day(MON, net_vnd=100), _day(TUE, net_vnd=200), _day(WED, net_vnd=400)], FRI)

    assert late.days == 3 and late.oldest_due_date == MON
    assert late.working_days_late == 4  # Mon, Tue, Wed, Thu
    assert late.net_vnd == 700


def test_first_period_interest_due_with_day_one_is_the_same_late_day():
    late = overdue_summary([_day(MON, net_vnd=100), _day(MON, net_vnd=50)], TUE)

    assert late.days == 1 and late.net_vnd == 150


def test_days_marked_paid_reported_or_confirmed_are_not_late():
    days = [
        _day(MON, status="PRESUMED_PAID"),
        _day(MON, status="REPORTED_MISSED"),
        _day(MON, status="MATCHED", settled_at=dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)),
    ]

    assert overdue_summary(days, FRI).days == 0
    assert overdue_summary(days, FRI).oldest_due_date is None


# --------------------------------------------------------------------------- #
# the admin endpoint
# --------------------------------------------------------------------------- #
def _working_days_before(today: dt.date, n: int) -> dt.date:
    """The n-th working day before `today`."""
    cal, d, seen = default_calendar(), today, 0
    while seen < n:
        d -= dt.timedelta(days=1)
        if cal.is_business_day(d):
            seen += 1
    return d


async def _overdue(client, admin):
    resp = await client.get("/admin/funding/repayments/overdue", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _due_on(db_session, ids, due_date):
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(ids)))).scalars():
        day.due_date = due_date
    await db_session.commit()


async def test_staff_see_a_ticket_the_business_has_not_repaid_on_time(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    assert (await _overdue(client, s["admin"]))["items"] == []  # due today: not late yet

    today = ict_date(dt.datetime.now(dt.timezone.utc))
    due = _working_days_before(today, 2)
    await _due_on(db_session, s["day_ids"], due)

    page = await _overdue(client, s["admin"])

    [item] = page["items"]
    days = (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(s["day_ids"])))).scalars().all()
    assert item["tranche_id"] == s["ticket"]["id"]
    assert item["reference_code"] == s["ticket"]["reference_code"]
    assert item["business"]["id"] == s["sme"]["id"] and item["investor"]["full_name"] == "Nguyen Van An"
    assert item["overdue_days"] == 1  # the instalment and first-period interest share a due date
    assert item["oldest_due_date"] == due.isoformat()
    assert item["working_days_late"] == count_business_days(due, today) >= 2
    assert item["overdue_net_vnd"] == sum(d.net_vnd for d in days)
    assert page["as_of"] == today.isoformat()
    assert page["tickets"] == 1 and page["businesses"] == 1
    assert page["overdue_days"] == 1 and page["overdue_net_vnd"] == item["overdue_net_vnd"]

    # Staff only.
    assert (
        await client.get("/admin/funding/repayments/overdue", headers=s["sme"]["headers"])
    ).status_code == 403


async def test_a_late_day_the_business_then_pays_is_no_longer_late(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    today = ict_date(dt.datetime.now(dt.timezone.utc))
    await _due_on(db_session, s["day_ids"], _working_days_before(today, 1))
    assert len((await _overdue(client, s["admin"]))["items"]) == 1

    await _pay(client, s)

    assert (await _overdue(client, s["admin"]))["items"] == []
    [receipt] = (await _receipts(client, s["admin"]))["items"]
    assert receipt["tranche_overdue_days"] == 0 and receipt["tranche_overdue_net_vnd"] == 0
    assert receipt["tranche_oldest_overdue_date"] is None


async def test_the_receipts_list_flags_a_ticket_with_a_later_day_still_unpaid(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    today = ict_date(dt.datetime.now(dt.timezone.utc))
    await _due_on(db_session, s["day_ids"], _working_days_before(today, 3))
    await _pay(client, s)  # day 1 paid, late
    # Day 2 has come and gone with no receipt.
    db_session.expire_all()
    day_two = (
        await db_session.execute(
            select(RepaymentDay).where(
                RepaymentDay.tranche_id == uuid.UUID(s["ticket"]["id"]),
                RepaymentDay.kind == "INSTALMENT",
                RepaymentDay.day_index == 2,
            )
        )
    ).scalar_one()
    late_since = _working_days_before(today, 2)
    day_two.due_date = late_since
    await db_session.commit()

    [receipt] = (await _receipts(client, s["admin"]))["items"]

    assert receipt["tranche_overdue_days"] == 1
    assert receipt["tranche_overdue_net_vnd"] == day_two.net_vnd
    assert receipt["tranche_oldest_overdue_date"] == late_since.isoformat()
    assert receipt["tranche_working_days_late"] == count_business_days(late_since, today)
    [item] = (await _overdue(client, s["admin"]))["items"]
    assert item["overdue_days"] == 1 and item["overdue_net_vnd"] == day_two.net_vnd


async def test_a_round_no_longer_repaying_is_not_listed(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    today = ict_date(dt.datetime.now(dt.timezone.utc))
    await _due_on(db_session, s["day_ids"], _working_days_before(today, 1))
    window = (
        await db_session.execute(
            select(FundingWindow)
            .join(Tranche, Tranche.window_id == FundingWindow.id)
            .where(Tranche.id == uuid.UUID(s["ticket"]["id"]))
        )
    ).scalar_one()
    window.status = "CANCELLED"
    await db_session.commit()

    assert (await _overdue(client, s["admin"]))["items"] == []
