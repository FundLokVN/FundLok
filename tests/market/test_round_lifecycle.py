"""A funding round from investment to daily repayment, over the API.

Investors pay the business directly and staff approve each transfer, which means the money is received and that
investment starts repaying at once, as its own loan with its own daily days; the business sees its investors
(masked) and pays each one every working day, one VietQR per investor, and marks the days paid; it closes the round
early once the minimum is confirmed, and staff advance the round to close it.
"""
import datetime as dt
import uuid

from sqlalchemy import select

from app.direct_transfer.calendar_utils import count_business_days, first_working_day_after, ict_date
from app.direct_transfer.evidence import store_evidence_file
from app.direct_transfer.models import FundingWindow, RepaymentDay
from app.direct_transfer.storage import InMemoryStorage
from app.lending.models import AuditLog
from tests.market.test_funding_flow import PNG, _open_project, _reserve, _upload


async def _approved(client, admin, investor, window_id, amount):
    """A ticket the investor paid and staff approved by looking at the receipt: received and repaying (ACTIVE)."""
    ticket = (await _reserve(client, investor, window_id, amount)).json()
    upload = (await _upload(client, investor, ticket["id"], PNG + uuid.uuid4().bytes)).json()
    resp = await client.post(
        f"/admin/funding/transfers/{upload['transfer_id']}/decision",
        json={"outcome": "APPROVED"},
        headers=admin["headers"],
    )
    assert resp.status_code == 200 and resp.json()["status"] == "ACTIVE", resp.text
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

    # The business sees its round and each investor, masked; staff approval counts as received and repaying.
    round_ = await _round(client, sme)
    assert round_["id"] == str(window_id) and round_["status"] == "OPEN"
    assert round_["project_name"].startswith("ABC Co") and round_["confirmed_vnd"] == 30_000_000
    [ticket] = round_["tickets"]
    assert ticket["investor_display"] == "N. A***" and ticket["status"] == "ACTIVE"

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
    proof_id = str((await store_evidence_file(
        db_session, InMemoryStorage(), owner_user_id=uuid.UUID(sme["id"]), purpose="RECEIPT",
        content=f"%PDF-1.4 {uuid.uuid4()}".encode(), content_type="application/pdf",
    )).id)
    if start > ict_date(dt.datetime.now(dt.timezone.utc)):
        not_yet = await client.post(
            "/repayment/days/mark-paid", json={"repayment_day_ids": day_ids[:1], "receipt_file_id": proof_id},
            headers=sme["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
        )
        assert not_yet.status_code == 409 and not_yet.headers["X-Error-Code"] == "NOT_DUE_YET"
        db_session.expire_all()
        for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(day_ids)))).scalars():
            day.due_date = ict_date(dt.datetime.now(dt.timezone.utc))
        await db_session.commit()
    paid = await client.post(
        "/repayment/days/mark-paid", json={"repayment_day_ids": day_ids, "receipt_file_id": proof_id},
        headers=sme["headers"] | {"Idempotency-Key": str(uuid.uuid4())},
    )
    assert paid.status_code == 200, paid.text
    assert {r["status"] for r in paid.json()} == {"PRESUMED_PAID"} and len(paid.json()) == len(day_ids)

    # Each investor sees their own schedule; another investor does not.
    schedule = await client.get(f"/repayment/tranches/{first['id']}/schedule", headers=an["headers"])
    assert schedule.status_code == 200 and len(schedule.json()) > 1
    assert (await client.get(f"/repayment/tranches/{first['id']}/schedule", headers=binh["headers"])).status_code == 404
    # The business sees each investment's own days too, as the investor does: what it paid, what is left.
    owed = await client.get(f"/repayment/tranches/{first['id']}/schedule", headers=sme["headers"])
    assert owed.status_code == 200 and owed.json() == schedule.json()

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


async def test_the_marketplace_card_shows_what_the_round_has_raised(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    viewer = await make_eligible_investor(await make_user(role="INVESTOR"))
    paid = await make_eligible_investor(await make_user(role="INVESTOR"))
    unpaid = await make_eligible_investor(await make_user(role="INVESTOR"))
    await _approved(client, setup["admin"], paid, window_id, 30_000_000)
    await _reserve(client, unpaid, window_id, 10_000_000)  # held, not paid: not raised yet

    projects = (await client.get("/projects/public", headers=viewer["headers"])).json()

    [card] = [p for p in projects if p["id"] == str(setup["project"])]
    listing = card["loan_application"]["listing"]
    assert listing["funded_amount"] == 30_000_000 and listing["investor_count"] == 1
    assert listing["target_amount"] == 80_000_000 and listing["min_ticket"] == 10_000_000
    assert listing["close_at"]


async def test_approval_forms_the_investment_s_own_loan_at_once(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    """The investment repays from its approval: its own loan, its days saved at once, while the round is open."""
    from decimal import Decimal

    from app.repayment_schedule.models import Facility
    from app.market.projection import project_ticket

    setup = await _open_project(client, db_session, make_admin, make_user)  # target 80M, minimum 40M
    window_id, contract_id = setup["window"].id, setup["window"].contract_id
    an = await make_eligible_investor(await make_user(role="INVESTOR"))
    binh = await make_eligible_investor(await make_user(role="INVESTOR"))
    first = await _approved(client, setup["admin"], an, window_id, 30_000_000)

    real = (await client.get(f"/repayment/tranches/{first['id']}/schedule", headers=an["headers"])).json()

    # 84 working days at the full graded rate (16% a year), plus the first-period interest: what the projection gives for today.
    instalments = [r for r in real if r["kind"] == "INSTALMENT"]
    assert len(instalments) == 84 and sum(r["principal_vnd"] for r in instalments) == 30_000_000
    assert sum(r["interest_vnd"] for r in instalments) == 1_600_000  # 16% a year over 4 months of 30M
    today = ict_date(dt.datetime.now(dt.timezone.utc))
    expected = project_ticket(
        amount_vnd=30_000_000, loan_rate_pct=Decimal("16"), term_months=4,
        individual=any(r["tax_withheld_vnd"] for r in real), confirmed_on=today, close_on=today,
    )
    key = lambda d: (d["kind"], str(d["due_date"]), d["principal_vnd"], d["gross_vnd"], d["tax_withheld_vnd"], d["net_vnd"])
    assert sorted(map(key, real)) == sorted(map(key, expected["days"]))
    # Tax is 5% of the interest only, never of the principal coming back: over the loan it is 5% of all the interest
    # (each day rounds down, the last day takes the leftover đồng), and no day's tax ever exceeds its interest.
    total_interest = sum(r["interest_vnd"] for r in real)
    if any(r["tax_withheld_vnd"] for r in real):  # an individual investor
        assert abs(sum(r["tax_withheld_vnd"] for r in real) - total_interest * 5 / 100) <= 1
    assert all(r["tax_withheld_vnd"] <= r["interest_vnd"] for r in real)

    # The round is still open, yet the business already sees what it owes and its real plan.
    plan = (await client.get(f"/funding/me/rounds/{window_id}/repayment-plan", headers=setup["sme"]["headers"])).json()
    assert plan["kind"] == "ACTUAL" and len(plan["days"]) == 84
    due = (
        await client.get("/repayment/today", params={"date": real[-1]["due_date"]}, headers=setup["sme"]["headers"])
    ).json()
    assert {r["tranche_id"] for r in due["rows"]} == {first["id"]}
    # The business transfers each day after deducting the investor's tax, 5% of the interest only (it keeps that
    # back for the tax office): the amount it is asked to pay and its VietQR are that net figure.
    from app.direct_transfer.vietqr import parse_tlv

    for row in due["rows"]:
        assert row["net_vnd"] == row["gross_vnd"] - row["tax_withheld_vnd"]
        assert row["tax_withheld_vnd"] <= row["interest_vnd"]  # never taken from the principal coming back
        assert dict(parse_tlv(row["vietqr_payload"]))["54"] == str(row["net_vnd"])
    assert due["totals"]["net_vnd"] == due["totals"]["gross_vnd"] - due["totals"]["tax_withheld_vnd"]
    projection = await client.get(f"/repayment/tranches/{first['id']}/projection", headers=an["headers"])
    assert projection.status_code == 409  # real days now, nothing to project

    # A second investment is a second loan on the same round.
    await _approved(client, setup["admin"], binh, window_id, 20_000_000)
    db_session.expire_all()
    facilities = (await db_session.execute(select(Facility).where(Facility.contract_id == contract_id))).scalars().all()
    assert sorted(int(f.principal_vnd) for f in facilities) == [20_000_000, 30_000_000]

    # Closing the round forms nothing more and refunds nobody.
    await client.post(f"/funding/windows/{window_id}/close-early", headers=setup["sme"]["headers"])
    advanced = await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=setup["admin"]["headers"])
    assert advanced.json()["status"] == "REPAYING", advanced.text
    db_session.expire_all()
    assert len((await db_session.execute(select(Facility).where(Facility.contract_id == contract_id))).scalars().all()) == 2


async def test_advance_starts_an_investment_approved_before_repaying_from_approval(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    import dataclasses

    from app.direct_transfer import config

    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR"))
    monkeypatch.setattr(config, "DEFAULT_PARAMS", dataclasses.replace(config.DEFAULT_PARAMS, repay_from_approval=False))
    ticket = (await _reserve(client, investor, window_id, 10_000_000)).json()
    upload = (await _upload(client, investor, ticket["id"], PNG + uuid.uuid4().bytes)).json()
    approved = await client.post(
        f"/admin/funding/transfers/{upload['transfer_id']}/decision", json={"outcome": "APPROVED"},
        headers=setup["admin"]["headers"],
    )
    assert approved.json()["status"] == "CONFIRMED"  # approved, but not repaying yet
    # Before its loan forms, the business and the investor both see the investment's projected days; nobody else.
    projected = await client.get(f"/repayment/tranches/{ticket['id']}/projection", headers=setup["sme"]["headers"])
    assert projected.status_code == 200 and projected.json()["business_day_count"] == 84
    mine = await client.get(f"/repayment/tranches/{ticket['id']}/projection", headers=investor["headers"])
    assert mine.json() == projected.json()
    other_sme = await make_user(role="SME")
    assert (
        await client.get(f"/repayment/tranches/{ticket['id']}/projection", headers=other_sme["headers"])
    ).status_code == 404
    monkeypatch.undo()
    # It was approved two weeks before staff advanced the round.
    from app.direct_transfer.models import Tranche

    approved_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)
    db_session.expire_all()
    row = await db_session.get(Tranche, uuid.UUID(ticket["id"]))
    row.confirmed_at = row.attested_at = approved_at
    await db_session.commit()

    await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=setup["admin"]["headers"])

    schedule = (await client.get(f"/repayment/tranches/{ticket['id']}/schedule", headers=investor["headers"])).json()
    # 84 working days, plus a first-period-interest day when the approval (the money's arrival here) fell on a
    # working day: approved on a weekend or holiday, no working day passes before day one.
    approved_on = ict_date(approved_at)
    stub_days = count_business_days(approved_on, first_working_day_after(approved_on))
    assert len(schedule) == 84 + (1 if stub_days else 0)
    # The loan works from the approval: day one is the working day after it, not after staff advanced the round.
    first = min(r["due_date"] for r in schedule)
    assert first == first_working_day_after(ict_date(approved_at)).isoformat()
    mine = (await client.get("/funding/me/tranches", headers=investor["headers"])).json()
    assert mine[0]["status"] == "ACTIVE"


async def test_the_business_pays_an_investor_and_uploads_its_receipt(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    """One investment's page: what is due now as one transfer with one VietQR, then the receipt and the days marked
    paid in one step."""
    from app.direct_transfer.models import EvidenceFile, Transfer
    from app.direct_transfer.repayment import late_interest_vnd
    from app.direct_transfer.vietqr import parse_tlv

    setup = await _open_project(client, db_session, make_admin, make_user)
    sme = setup["sme"]
    an = await make_eligible_investor(await make_user(role="INVESTOR"))
    binh = await make_eligible_investor(await make_user(role="INVESTOR"))
    ticket = await _approved(client, setup["admin"], an, setup["window"].id, 30_000_000)
    other = await _approved(client, setup["admin"], binh, setup["window"].id, 20_000_000)
    schedule = (await client.get(f"/repayment/tranches/{ticket['id']}/schedule", headers=sme["headers"])).json()

    # Make the first day (its instalment and the first-period interest) due today.
    first_date = schedule[0]["due_date"]
    first_ids = [r["repayment_day_id"] for r in schedule if r["due_date"] == first_date]
    today = ict_date(dt.datetime.now(dt.timezone.utc))
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(first_ids)))).scalars():
        day.due_date = today
    await db_session.commit()

    due = await client.get(f"/repayment/tranches/{ticket['id']}/due", headers=sme["headers"])
    assert due.status_code == 200, due.text
    due = due.json()
    assert sorted(r["repayment_day_id"] for r in due["days"]) == sorted(first_ids)
    assert due["net_vnd"] == sum(r["net_vnd"] for r in due["days"]) > 0
    assert due["net_vnd"] == due["gross_vnd"] - due["tax_withheld_vnd"]
    # One transfer for the whole day: the investor's account, the full amount, the investment's memo.
    assert due["payee"]["account_number"] and due["reference_code"] == ticket["reference_code"]
    assert dict(parse_tlv(due["vietqr_payload"]))["54"] == str(due["net_vnd"])
    assert due["late_interest_vnd"] == 0  # due today: nothing late yet

    # Three days overdue, the instruction shows the late interest to pay the investor apart from the QR amount.
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(first_ids)))).scalars():
        day.due_date = today - dt.timedelta(days=3)
    await db_session.commit()
    overdue = (await client.get(f"/repayment/tranches/{ticket['id']}/due", headers=sme["headers"])).json()
    window = await db_session.get(FundingWindow, uuid.UUID(ticket["window_id"]))
    assert overdue["late_interest_vnd"] == sum(
        late_interest_vnd(r["principal_vnd"], r["interest_vnd"], annual_rate_pct=window.annual_rate_pct,
                          due_date=today - dt.timedelta(days=3), received_on=today)
        for r in overdue["days"]
    ) > 0
    assert overdue["net_vnd"] == due["net_vnd"]  # not in the VietQR amount
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(first_ids)))).scalars():
        day.due_date = today
    await db_session.commit()

    # Only the business sees what it owes; it pays only this investment's days.
    assert (await client.get(f"/repayment/tranches/{ticket['id']}/due", headers=an["headers"])).status_code == 403
    other_sme = await make_user(role="SME")
    assert (await client.get(f"/repayment/tranches/{ticket['id']}/due", headers=other_sme["headers"])).status_code == 404
    other_day = (await client.get(f"/repayment/tranches/{other['id']}/schedule", headers=sme["headers"])).json()[0]
    pay = lambda ids, content, key: client.post(
        f"/repayment/tranches/{ticket['id']}/pay",
        data={"repayment_day_ids": ids},
        files={"file": ("receipt.png", content, "image/png")},
        headers=sme["headers"] | ({"Idempotency-Key": key} if key else {}),
    )
    assert (await pay([other_day["repayment_day_id"]], PNG + b"x", "k-0")).status_code == 404
    assert (await pay(first_ids, PNG + b"x", None)).status_code == 400
    unsupported = await pay(first_ids, b"not a receipt", "k-1")
    assert unsupported.status_code == 415 and unsupported.headers["X-Error-Code"] == "UNSUPPORTED_TYPE"

    receipt = PNG + uuid.uuid4().bytes
    paid = await pay(first_ids, receipt, "k-2")
    assert paid.status_code == 200, paid.text
    assert {r["status"] for r in paid.json()} == {"PRESUMED_PAID"} and len(paid.json()) == len(first_ids)
    # The receipt is kept as evidence, against each day it pays.
    db_session.expire_all()
    transfers = (await db_session.execute(select(Transfer).where(Transfer.kind == "REPAYMENT"))).scalars().all()
    assert sorted(str(t.repayment_day_id) for t in transfers) == sorted(
        r["repayment_day_id"] for r in due["days"] if r["net_vnd"] > 0
    )
    assert len({t.evidence_file_id for t in transfers}) == 1
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "REPAYMENT_DAYS_MARKED_PAID"))
    ).scalar_one()
    assert audit.after_state["receipt_file_id"] == str(transfers[0].evidence_file_id)

    # A retry of the same request stores and marks nothing twice.
    again = await pay(first_ids, receipt, "k-2")
    assert again.status_code == 200 and again.json() == paid.json()
    db_session.expire_all()
    business_files = select(EvidenceFile).where(
        EvidenceFile.owner_user_id == uuid.UUID(sme["id"]), EvidenceFile.purpose == "RECEIPT"
    )
    assert len((await db_session.execute(business_files)).scalars().all()) == 1
    assert len((await db_session.execute(select(Transfer).where(Transfer.kind == "REPAYMENT"))).scalars().all()) == len(
        transfers
    )

    # Paid: nothing more is due today. The same receipt cannot pay another day.
    after = (await client.get(f"/repayment/tranches/{ticket['id']}/due", headers=sme["headers"])).json()
    assert after["days"] == [] and after["net_vnd"] == 0 and after["vietqr_payload"] is None
    reused = await pay([schedule[-1]["repayment_day_id"]], receipt, "k-3")
    assert reused.status_code == 409 and reused.headers["X-Error-Code"] == "DUPLICATE_FILE"
