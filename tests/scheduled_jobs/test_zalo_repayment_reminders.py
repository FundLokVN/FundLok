"""The daily Zalo repayment reminder: with the 15:00 email, the SME gets one ZBS
message per investor ticket due today that it has not marked paid (no receipt
uploaded). Spec: docs/specs/scheduled_jobs/cloud-scheduler-jobs.md.

Zalo is never contacted: `zalo.send_template` is replaced by `zalo_outbox`.
"""
import dataclasses
import datetime as dt
import uuid

import pytest
from sqlalchemy import distinct, func, select, update

from app.core.config import settings
from app.direct_transfer.models import RepaymentDay, Tranche
from app.phone_verification import zalo
from app.scheduled_jobs import zalo_reminders
from app.scheduled_jobs.models import RepaymentZaloReminder
from app.scheduled_jobs.zalo_reminders import DueTicket, template_data
from tests.scheduled_jobs.test_cloud_scheduler_jobs import (  # noqa: F401 - fixtures
    REMINDERS,
    _morning,
    _repaying_round,
    outbox,
    scheduler,
)

TEMPLATE = "666161"
PHONE = "+84912345678"


@pytest.fixture
def zalo_outbox(monkeypatch):
    sent: list[dict] = []

    async def fake_send(to, template_id, data, *, tracking_id):
        sent.append({"to": to, "template_id": template_id, "data": data, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_template", fake_send)
    monkeypatch.setattr(settings, "ZALO_ZBS_REMINDER_TEMPLATE_ID", TEMPLATE)
    return sent


async def _with_phone(db, world, *, verified=True):
    sme = world.sme
    sme.phone = PHONE
    sme.phone_verified_at = dt.datetime.now(dt.timezone.utc) if verified else None
    await db.commit()


async def _due(db, day):
    """{tranche_id: net due that day} for days still DUE."""
    rows = (
        await db.execute(
            select(RepaymentDay.tranche_id, RepaymentDay.net_vnd).where(
                RepaymentDay.due_date == day, RepaymentDay.status == "DUE"
            )
        )
    ).all()
    out: dict = {}
    for tranche_id, net in rows:
        out[tranche_id] = out.get(tranche_id, 0) + net
    return out


# --------------------------------------------------------------------------- #
# what the message says
# --------------------------------------------------------------------------- #
def test_the_template_gets_every_parameter_as_text():
    ticket = DueTicket(
        tranche_id=uuid.uuid4(), sme_user_id=uuid.uuid4(), company_name="CÔNG TY TNHH HẠT MỘC",
        reference_code="FL0EMIRQAE", amount_vnd=166_270, days_left=59,
    )

    assert template_data(ticket, dt.date(2026, 11, 13)) == {
        "company_name": "CÔNG TY TNHH HẠT MỘC",
        "reference_code": "FL0EMIRQAE",
        "due_date": "13/11/2026",
        "amount_due": "166270",
        "days_left": "59",
    }
    long = dataclasses.replace(ticket, company_name="A" * 250)
    assert len(template_data(long, dt.date(2026, 11, 13))["company_name"]) == 200


# --------------------------------------------------------------------------- #
# who gets one
# --------------------------------------------------------------------------- #
async def test_each_ticket_due_today_and_not_paid_gets_one_message(db_session, zalo_outbox):
    world, _, second = await _repaying_round(db_session)
    await _with_phone(db_session, world)
    due = await _due(db_session, second)
    assert len(due) == 10

    result = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))

    assert result == {"sent": 10, "failed": 0, "skipped": None}
    assert {m["to"] for m in zalo_outbox} == {"84912345678"}
    assert {m["template_id"] for m in zalo_outbox} == {TEMPLATE}
    tranches = {t.reference_code: t for t in (await db_session.execute(select(Tranche))).scalars()}
    for message in zalo_outbox:
        data = message["data"]
        tranche = tranches[data["reference_code"]]
        days_left = (
            await db_session.execute(
                select(func.count(distinct(RepaymentDay.due_date))).where(
                    RepaymentDay.tranche_id == tranche.id, RepaymentDay.due_date > second
                )
            )
        ).scalar_one()
        assert data["company_name"] == world.project.legal_name
        assert data["due_date"] == second.strftime("%d/%m/%Y")
        assert data["amount_due"] == str(due[tranche.id])
        assert data["days_left"] == str(days_left) and days_left > 0
        assert len(message["tracking_id"]) == 32
    assert len({m["tracking_id"] for m in zalo_outbox}) == 10

    rows = (await db_session.execute(select(RepaymentZaloReminder))).scalars().all()
    assert {(r.status, r.sent_to, r.reminder_date) for r in rows} == {("SENT", "84912345678", second)}

    # A scheduler retry the same day: nothing more is sent (or paid for).
    again = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))
    assert again["sent"] == 0 and len(zalo_outbox) == 10


async def test_a_ticket_the_sme_uploaded_a_receipt_for_gets_nothing(db_session, zalo_outbox):
    world, _, second = await _repaying_round(db_session)
    await _with_phone(db_session, world)
    paid = next(iter(await _due(db_session, second)))
    await db_session.execute(
        update(RepaymentDay)
        .where(RepaymentDay.tranche_id == paid, RepaymentDay.due_date == second)
        .values(status="PRESUMED_PAID")
    )
    await db_session.commit()

    result = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))

    assert result["sent"] == 9
    paid_code = (await db_session.get(Tranche, paid)).reference_code
    assert paid_code not in {m["data"]["reference_code"] for m in zalo_outbox}


async def test_only_earlier_unpaid_days_send_nothing(db_session, zalo_outbox):
    # The template says "due today": earlier unpaid days are for staff (Admin > Repayments, in red).
    world, first, second = await _repaying_round(db_session)
    await _with_phone(db_session, world)
    await db_session.execute(
        update(RepaymentDay).where(RepaymentDay.due_date == second).values(status="PRESUMED_PAID")
    )
    await db_session.commit()
    assert await _due(db_session, first)

    result = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))

    assert result["sent"] == 0 and zalo_outbox == []


async def test_an_unproven_phone_gets_nothing(db_session, zalo_outbox):
    world, _, second = await _repaying_round(db_session)
    await _with_phone(db_session, world, verified=False)

    result = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))

    assert result["sent"] == 0 and zalo_outbox == []


async def test_nothing_without_the_template_or_on_a_day_off(db_session, zalo_outbox, monkeypatch):
    world, first, second = await _repaying_round(db_session)
    await _with_phone(db_session, world)

    saturday = first + dt.timedelta(days=(5 - first.weekday()) % 7)
    assert (await zalo_reminders.send_zalo_reminders(db_session, now=_morning(saturday)))["skipped"] == (
        "NOT_A_WORKING_DAY"
    )
    monkeypatch.setattr(settings, "ZALO_ZBS_REMINDER_TEMPLATE_ID", None)
    assert (await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second)))["skipped"] == (
        "NOT_CONFIGURED"
    )
    assert zalo_outbox == []


async def test_a_message_zalo_refused_is_retried_by_the_next_run(db_session, zalo_outbox, monkeypatch):
    world, _, second = await _repaying_round(db_session)
    await _with_phone(db_session, world)
    working = zalo.send_template

    async def refused(to, template_id, data, *, tracking_id):
        raise zalo.ZaloError("no zalo account", code=-118)

    monkeypatch.setattr(zalo, "send_template", refused)
    result = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))
    assert result == {"sent": 0, "failed": 10, "skipped": None}
    rows = (await db_session.execute(select(RepaymentZaloReminder))).scalars().all()
    assert {(r.status, r.attempts, r.last_error) for r in rows} == {("FAILED", 1, "ZALO_-118")}

    monkeypatch.setattr(zalo, "send_template", working)
    result = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))
    assert result["sent"] == 10 and len(zalo_outbox) == 10
    db_session.expire_all()
    rows = (await db_session.execute(select(RepaymentZaloReminder))).scalars().all()
    assert {(r.status, r.attempts) for r in rows} == {("SENT", 2)}


async def test_a_missing_zalo_setting_stops_the_run_after_one_try(db_session, zalo_outbox, monkeypatch):
    world, _, second = await _repaying_round(db_session)
    await _with_phone(db_session, world)

    async def unconfigured(to, template_id, data, *, tracking_id):
        raise zalo.ZaloNotConfigured("ZALO_APP_SECRET not configured")

    monkeypatch.setattr(zalo, "send_template", unconfigured)
    result = await zalo_reminders.send_zalo_reminders(db_session, now=_morning(second))

    assert result == {"sent": 0, "failed": 1, "skipped": None}
    [row] = (await db_session.execute(select(RepaymentZaloReminder))).scalars().all()
    assert row.last_error == "ZALO_NOT_CONFIGURED"


# --------------------------------------------------------------------------- #
# the scheduler's call
# --------------------------------------------------------------------------- #
async def test_the_scheduler_call_reports_the_zalo_messages_too(client, scheduler, outbox, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "ZALO_ZBS_REMINDER_TEMPLATE_ID", None)
    client.cookies.clear()

    resp = await client.post(REMINDERS, headers={"Authorization": "Bearer valid"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job"] == "repayment-reminders"
    # On a day off nothing runs at all; otherwise an unset template is named.
    assert body["zalo"]["sent"] == 0 and body["zalo"]["skipped"] in ("NOT_CONFIGURED", "NOT_A_WORKING_DAY")
