"""Day-14 jobs (docs/specs/direct_transfer/day14-transfer-rule.md rules 9-10, section 8 reminder criteria)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.timezone import ICT
from app.direct_transfer import tranches
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.day14_times import set_day14_cutoffs
from app.direct_transfer.models import TransferReminder
from app.notifications.models import Notification
from app.phone_verification import zalo
from app.scheduled_jobs import day14 as jobs
from tests.direct_transfer.helpers import T0, accept, good_fields, make_investor, make_world, open_window, reserve, upload_receipt
from tests.scheduled_jobs.test_cloud_scheduler_jobs import outbox, scheduler  # noqa: F401 - fixtures

DAY = T0.date()  # Monday 2 Nov 2026: the round's disbursement day in these tests
TEMPLATE = "777001"
PHONE = "+84912345678"


def ict(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=ICT)


@pytest.fixture
def zalo_outbox(monkeypatch):
    sent: list[dict] = []

    async def fake_send(to, template_id, data, *, tracking_id):
        sent.append({"to": to, "template_id": template_id, "data": data, "tracking_id": tracking_id})

    monkeypatch.setattr(zalo, "send_template", fake_send)
    monkeypatch.setattr(settings, "ZALO_ZBS_DAY14_TEMPLATE_ID", TEMPLATE)
    return sent


async def _day14_round(db, *, day14=True):
    """A round paying out today with one lender who has not paid and one who has."""
    # On a commit-then-accept round the hold runs to the pay deadline; a long hold stands in for that here.
    world = await make_world(db, params=DirectTransferParams(repay_from_approval=False, hold_hours=8))
    await open_window(world, now=T0 - dt.timedelta(days=13))
    if day14:
        set_day14_cutoffs(world.window, DAY, world.params)
    unpaid = await make_investor(world)
    unpaid.user.phone = PHONE
    unpaid.user.phone_verified_at = dt.datetime.now(dt.timezone.utc)
    ticket = await reserve(world, unpaid, 10_000_000, now=T0 - dt.timedelta(hours=2))
    await accept(world, unpaid, ticket, now=T0 - dt.timedelta(hours=2))
    paid = await make_investor(world)
    paid_ticket = await reserve(world, paid, 10_000_000, now=T0 - dt.timedelta(hours=2))
    await accept(world, paid, paid_ticket, now=T0 - dt.timedelta(hours=2))
    await upload_receipt(world, paid, paid_ticket, now=T0 - dt.timedelta(hours=1, minutes=50))
    await db.commit()
    return world, unpaid, ticket, paid


async def _notifications(db, user_id, event):
    return (
        await db.execute(select(Notification).where(Notification.user_id == user_id, Notification.event == event))
    ).scalars().all()


def test_the_zalo_template_gets_every_parameter_as_text():
    due = jobs.DueTransfer(
        tranche_id=__import__("uuid").uuid4(), investor_id=__import__("uuid").uuid4(), listing_title="Cong Ty ABC",
        reference_code="FLAB12CD34", amount_vnd=20_000_000, pay_deadline_at=ict(14, 45), project_id=None,
    )
    # No reference code: on Day 14 the investor must pay with a QR's own code, from FundLok.
    assert jobs.zalo_template_data(due) == {
        "company_name": "Cong Ty ABC", "amount": "20000000", "deadline": "14:45 02/11/2026",
    }
    content = jobs.compose_email(due)
    assert "14:45" in content.subject and "20.000.000 ₫" in content.body


async def test_day14_reminder_targets_unpaid_lenders_on_disbursement_day(db_session, outbox, zalo_outbox):
    world, unpaid, ticket, paid = await _day14_round(db_session)
    counts = await jobs.send_day14_reminders(db_session, kind="DAY14_MORNING", now=ict(8, 0))
    assert counts["in_app"]["sent"] == 1 and counts["email"]["sent"] == 1 and counts["zalo"]["sent"] == 1

    [note] = await _notifications(db_session, unpaid.user.id, "TRANSFER_DUE_TODAY")
    assert note.data["tranche_id"] == str(ticket.id) and note.data["amount_vnd"] == 10_000_000
    assert dt.datetime.fromisoformat(note.data["pay_deadline_at"]) == ict(14, 45)
    assert [m["to"] for m in outbox] == [unpaid.user.email]
    assert "14:45" in outbox[0]["subject"]
    assert zalo_outbox[0]["template_id"] == TEMPLATE and "reference_code" not in zalo_outbox[0]["data"]
    assert note.data["project_id"] == str(world.project.id)
    assert len(zalo_outbox[0]["tracking_id"]) == 32
    assert await _notifications(db_session, paid.user.id, "TRANSFER_DUE_TODAY") == []


async def test_day14_reminder_sends_once_per_ticket_kind_and_channel(db_session, outbox, zalo_outbox):
    await _day14_round(db_session)
    await jobs.send_day14_reminders(db_session, kind="DAY14_MORNING", now=ict(8, 0))
    again = await jobs.send_day14_reminders(db_session, kind="DAY14_MORNING", now=ict(8, 5))
    assert all(again[channel]["sent"] == 0 for channel in ("in_app", "email", "zalo"))
    afternoon = await jobs.send_day14_reminders(db_session, kind="DAY14_AFTERNOON", now=ict(14, 0))
    assert afternoon["in_app"]["sent"] == 1 and afternoon["email"]["sent"] == 1
    rows = (await db_session.execute(select(TransferReminder))).scalars().all()
    assert sorted((r.kind, r.channel, r.status) for r in rows) == sorted(
        (kind, channel, "SENT") for kind in ("DAY14_MORNING", "DAY14_AFTERNOON") for channel in ("IN_APP", "EMAIL", "ZALO")
    )


async def test_a_failed_email_is_retried_by_the_next_run(db_session, outbox, monkeypatch):
    from app.admin import system_email

    await _day14_round(db_session)
    monkeypatch.setattr(system_email, "send_email", lambda *a, **k: False)
    first = await jobs.send_day14_reminders(db_session, kind="DAY14_MORNING", now=ict(8, 0))
    assert first["email"]["failed"] == 1
    monkeypatch.setattr(system_email, "send_email", lambda to, subject, html, text=None, reply_to=None: outbox.append(to) or True)
    second = await jobs.send_day14_reminders(db_session, kind="DAY14_MORNING", now=ict(8, 30))
    assert second["email"]["sent"] == 1 and second["in_app"]["sent"] == 0


async def test_day14_reminder_skips_zalo_without_a_template(db_session, outbox, monkeypatch):
    monkeypatch.setattr(settings, "ZALO_ZBS_DAY14_TEMPLATE_ID", None)
    await _day14_round(db_session)
    counts = await jobs.send_day14_reminders(db_session, kind="DAY14_MORNING", now=ict(8, 0))
    assert counts["zalo"] == {"sent": 0, "failed": 0, "skipped": "NOT_CONFIGURED"}
    assert counts["in_app"]["sent"] == 1 and counts["email"]["sent"] == 1


async def test_rounds_without_day14_cut_offs_get_no_reminder(db_session, outbox, zalo_outbox):
    await _day14_round(db_session, day14=False)
    counts = await jobs.send_day14_reminders(db_session, kind="DAY14_MORNING", now=ict(8, 0))
    assert counts["in_app"]["sent"] == counts["email"]["sent"] == counts["zalo"]["sent"] == 0


async def test_no_reminder_after_1445(db_session, outbox, zalo_outbox):
    await _day14_round(db_session)
    counts = await jobs.send_day14_reminders(db_session, kind="DAY14_AFTERNOON", now=ict(14, 45))
    assert counts["in_app"]["sent"] == 0


async def test_the_cutoff_job_locks_no_shows_and_late_money_and_tells_them(db_session):
    world = await make_world(db_session)
    await open_window(world, now=T0)
    set_day14_cutoffs(world.window, DAY, world.params)
    no_show = await make_investor(world)
    ticket = await reserve(world, no_show, 10_000_000, now=ict(13, 0))
    await accept(world, no_show, ticket, now=ict(13, 0))
    late = await make_investor(world)
    late_ticket = await reserve(world, late, 10_000_000, now=ict(10, 0))
    await accept(world, late, late_ticket, now=ict(10, 0))
    pay = await tranches.payment_instructions(
        db_session, tranche_id=late_ticket.id, investor_id=late.user.id, now=ict(10, 1), params=world.params
    )
    fields = good_fields(world, late_ticket, late, when=ict(10, 40), memo=pay.qr_code)
    await upload_receipt(world, late, late_ticket, now=ict(10, 41), fields=fields)
    await db_session.commit()

    result = await jobs.run_day14_cutoff(db_session, now=ict(15, 0))
    assert result == {"locked": 2, "pledges": 0, "released": 0, "failed": 0}
    [locked] = await _notifications(db_session, no_show.user.id, "LENDER_LOCKED")
    assert locked.data["reason"] == "NO_SHOW" and locked.data["listing_title"] == world.project.legal_name
    assert [n.data["reason"] for n in await _notifications(db_session, late.user.id, "LENDER_LOCKED")] == ["LATE_MONEY"]

    # After the 15:30 receipt cut-off: the late money becomes the lender's top-up; nobody is told twice.
    assert await jobs.run_day14_cutoff(db_session, now=ict(15, 35)) == {
        "locked": 0, "pledges": 1, "released": 0, "failed": 0,
    }
    [pledged] = await _notifications(db_session, late.user.id, "TOP_UP_PLEDGE_RECORDED")
    assert pledged.data["amount_vnd"] == 10_000_000
    assert len(await _notifications(db_session, no_show.user.id, "LENDER_LOCKED")) == 1

    assert await jobs.run_day14_cutoff(db_session, now=ict(15, 40)) == {
        "locked": 0, "pledges": 0, "released": 0, "failed": 0,
    }


async def test_the_day14_endpoints_answer_only_the_scheduler(client, scheduler, outbox):
    for path, body in (
        ("/internal/jobs/day14-cutoff", None),
        ("/internal/jobs/day14-transfer-reminders", {"kind": "DAY14_MORNING"}),
    ):
        denied = await client.post(path, json=body)
        assert denied.status_code in (401, 403)
        resp = await client.post(path, json=body, headers={"Authorization": "Bearer valid"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["job"] == path.rsplit("/", 1)[1]
    bad = await client.post(
        "/internal/jobs/day14-transfer-reminders", json={"kind": "NOON"}, headers={"Authorization": "Bearer valid"}
    )
    assert bad.status_code == 422
