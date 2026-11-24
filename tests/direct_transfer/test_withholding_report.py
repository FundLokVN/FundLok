"""The SME's monthly tax-withholding report (docs/specs/direct_transfer/withholding-report.md section 8).

The `loan` fixture: 10 individual investors x 10M, repayment from Wed 4 Nov 2026. The report counts the instalments
the SME paid in the month (PRESUMED_PAID or MATCHED), per individual lender.
"""
import datetime as dt
import uuid

from conftest import auth_headers
from sqlalchemy import select

from app.core.timezone import ICT
from app.direct_transfer import repayment
from app.direct_transfer.models import RepaymentDay
from app.market.investor_agreement.service import current_party_b

from .helpers import close_now, fund_window, at, pay_days_through, receipt_file


def ict(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ICT)


async def _report(world, y=2026, m=11, sme_user_id=None):
    return await repayment.withholding_report(
        world.db, sme_user_id=sme_user_id or world.sme.id, month=dt.date(y, m, 1)
    )


async def _days(db, **where):
    stmt = select(RepaymentDay)
    for key, value in where.items():
        stmt = stmt.where(getattr(RepaymentDay, key) == value)
    return (await db.execute(stmt.order_by(RepaymentDay.due_date, RepaymentDay.day_index))).scalars().all()


async def _pay(world, due_date, *, paid_at):
    ids = [d.id for d in await _days(world.db, due_date=due_date)]
    return await repayment.mark_days_paid(
        world.db, sme_user_id=world.sme.id, repayment_day_ids=ids, receipt_file_id=(await receipt_file(world)).id,
        idempotency_key=f"pay-{uuid.uuid4().hex}", now=paid_at, params=world.params,
    )


async def test_the_report_counts_only_paid_instalments(loan):
    world, _, _ = loan
    assert (await _report(world))["rows"] == []  # nothing paid yet
    await _pay(world, dt.date(2026, 11, 4), paid_at=ict(2026, 11, 4, 10))
    out = await _report(world)
    paid = [d for d in await _days(world.db, due_date=dt.date(2026, 11, 4)) if d.status == "PRESUMED_PAID"]
    assert len(out["rows"]) == 10 and out["totals"]["paid_days"] == len(paid)
    assert out["totals"]["interest_vnd"] == sum(d.interest_vnd for d in paid)


async def test_a_late_or_missed_day_is_not_in_the_report(loan):
    world, _, _ = loan
    await repayment.mark_late_days(world.db, now=ict(2026, 11, 4, 18, 10), params=world.params)
    assert {d.status for d in await _days(world.db, due_date=dt.date(2026, 11, 4))} == {"LATE"}
    assert (await _report(world))["rows"] == []


async def test_the_month_is_the_month_the_instalment_was_paid(loan):
    world, _, _ = loan
    await repayment.mark_late_days(world.db, now=ict(2026, 11, 5, 0, 10), params=world.params)
    await _pay(world, dt.date(2026, 11, 4), paid_at=ict(2026, 12, 2, 10))  # due in November, paid in December
    assert (await _report(world, 2026, 11))["rows"] == []
    december = await _report(world, 2026, 12)
    assert len(december["rows"]) == 10 and december["totals"]["paid_days"] > 0


async def test_the_report_has_one_row_per_individual_lender_with_full_name_and_id(loan):
    world, _, tr = loan
    await pay_days_through(world, ict(2026, 11, 10))
    rows = (await _report(world))["rows"]
    assert {r["investor_id"] for r in rows} == {t.investor_id for t in tr} and len(rows) == 10
    for row in rows:
        party = await current_party_b(world.db, row["investor_id"])
        assert row["lender_name"] == party.name and row["lender_id_number"] == party.id_number
        assert "*" not in (row["lender_name"] or "")  # the full name, as the contract prints it
    assert [r["lender_name"] for r in rows] == sorted(r["lender_name"] for r in rows)


async def test_a_company_lender_is_not_in_the_report(world):
    await fund_window(world, [10_000_000] * 10, holder_types=["INDIVIDUAL"] * 9 + ["ENTITY"])
    await close_now(world, now=at(days=1))
    await pay_days_through(world, ict(2026, 11, 10))
    assert len((await _report(world))["rows"]) == 9


async def test_the_totals_equal_the_sum_of_the_daily_tax_withheld(loan):
    world, _, _ = loan
    await pay_days_through(world, ict(2026, 12, 3))
    out = await _report(world)
    paid = [d for d in await _days(world.db) if d.status in ("PRESUMED_PAID", "MATCHED")
            and d.marked_paid_at.astimezone(ICT).month == 11]
    assert out["totals"]["tax_withheld_vnd"] == sum(d.tax_withheld_vnd for d in paid) > 0
    assert out["totals"]["tax_withheld_vnd"] == sum(r["tax_withheld_vnd"] for r in out["rows"])
    assert out["totals"]["interest_vnd"] == sum(d.interest_vnd for d in paid)
    assert out["totals"]["paid_days"] == len(paid) == sum(r["paid_days"] for r in out["rows"])


async def test_a_stub_instalment_counts_in_the_month_it_was_paid(loan):
    world, _, _ = loan
    await _pay(world, dt.date(2026, 11, 4), paid_at=ict(2026, 11, 4, 10))
    days = await _days(world.db, due_date=dt.date(2026, 11, 4))
    stubs = [d for d in days if d.kind == "STUB" and d.status == "PRESUMED_PAID"]
    instalments = [d for d in days if d.kind == "INSTALMENT" and d.status == "PRESUMED_PAID"]
    assert len(stubs) == 10 and stubs[0].interest_vnd > 0 and stubs[0].tax_withheld_vnd > 0
    out = await _report(world)
    assert out["totals"]["paid_days"] == len(stubs) + len(instalments)
    assert out["totals"]["tax_withheld_vnd"] == sum(d.tax_withheld_vnd for d in stubs + instalments)
    assert out["totals"]["tax_withheld_vnd"] > sum(d.tax_withheld_vnd for d in instalments)  # the stubs are in it


async def test_only_the_signed_in_smes_own_rounds_are_included(loan):
    world, _, _ = loan
    await pay_days_through(world, ict(2026, 11, 10))
    assert (await _report(world))["rows"]
    assert (await _report(world, sme_user_id=uuid.uuid4()))["rows"] == []


async def test_an_empty_month_returns_no_rows_and_zero_totals(loan):
    world, _, _ = loan
    out = await _report(world, 2027, 6)
    assert out == {"month": "2027-06", "rows": [], "totals": {"paid_days": 0, "interest_vnd": 0, "tax_withheld_vnd": 0}}


async def test_the_route_is_sme_only_and_rejects_a_bad_month(client, make_user):
    url = "/repayment/withholding-report"
    client.cookies.clear()
    assert (await client.get(url)).status_code == 401
    investor = await make_user(role="INVESTOR")
    assert (await client.get(url, headers=auth_headers(investor))).status_code == 403
    sme = await make_user(role="SME")
    for bad in ("2026-13", "2026-1", "nov-2026", "2026-11-01"):
        assert (await client.get(url, params={"month": bad}, headers=auth_headers(sme))).status_code == 422, bad
    ok = await client.get(url, params={"month": "2026-12"}, headers=auth_headers(sme))
    assert ok.status_code == 200 and ok.json() == {
        "month": "2026-12", "rows": [], "totals": {"paid_days": 0, "interest_vnd": 0, "tax_withheld_vnd": 0},
    }
