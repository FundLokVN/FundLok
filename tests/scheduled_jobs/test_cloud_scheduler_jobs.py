"""Jobs Google Cloud Scheduler runs on prod-api: only its service account may
call them, and the daily repayment reminder emails each SME once per working
day about the days still DUE. Spec: docs/specs/scheduled_jobs/cloud-scheduler-jobs.md.

Google is never contacted: the token check (`auth._verify`) is replaced, and
`outbox` captures the platform relay with no reminder sender chosen.
"""
import datetime as dt
import smtplib

import pytest
from sqlalchemy import select, update

from app.admin import system_email
from app.core.timezone import ICT
from app.direct_transfer.models import RepaymentDay
from app.scheduled_jobs import acceptance, auth, reminders
from app.scheduled_jobs.models import RepaymentReminder
from tests.direct_transfer.helpers import at, close_now, fund_window, make_world

M = 1_000_000
ACCOUNT = "cloud-scheduler@fundlok.iam.gserviceaccount.com"
AUDIENCE = "https://prod-api.example.run.app"
REMINDERS = "/internal/jobs/repayment-reminders"
FEES = "/internal/jobs/fee-invoice-emails"
REPAYMENT_DAY = "/internal/jobs/repayment-day"
INVOICES = "/internal/jobs/invoices"
CIC_DAILY_BALANCE = "/internal/jobs/cic-daily-balance"
FUNDING_ADVANCE = "/internal/jobs/funding-advance"


@pytest.fixture
def scheduler(monkeypatch):
    """Configured, with Google's check replaced: the token names the account
    it was issued for ("valid" is the scheduler's own)."""
    monkeypatch.setenv("SCHEDULER_SERVICE_ACCOUNT", ACCOUNT)
    monkeypatch.setenv("SCHEDULER_AUDIENCE", AUDIENCE)

    def verify(token, audience):
        assert audience == AUDIENCE
        if token == "valid":
            return {"email": ACCOUNT, "email_verified": True}
        if token == "someone-else":
            return {"email": "intruder@example.iam.gserviceaccount.com", "email_verified": True}
        if token == "unverified":
            return {"email": ACCOUNT, "email_verified": False}
        raise ValueError("bad signature")

    monkeypatch.setattr(auth, "_verify", verify)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    def fake_send_email(to, subject, html, text=None, reply_to=None):
        sent.append({"to": to, "subject": subject, "html": html, "text": text})
        return True

    async def no_sender(db):
        return None

    monkeypatch.setattr(system_email, "send_email", fake_send_email)
    monkeypatch.setattr(system_email.email_service, "resolve_reminder_sender", no_sender)
    return sent


async def _repaying_round(db):
    """A 100M round of ten 10M investments, closed: Edward's close forms the
    loan and the daily repayment days. Returns the world and the first two
    due dates."""
    world = await make_world(db)
    await fund_window(world, [10 * M] * 10)
    await close_now(world, now=at(days=1))
    await db.commit()
    dates = sorted(
        {d for (d,) in (await db.execute(select(RepaymentDay.due_date))).all()}
    )
    return world, dates[0], dates[1]


def _morning(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(8, 0), tzinfo=ICT)


async def _due(db, day: dt.date) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(RepaymentDay.net_vnd).where(
                RepaymentDay.due_date == day, RepaymentDay.status == "DUE"
            )
        )
    ).scalars().all()
    return len(rows), sum(rows)


def _vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + " VND"


# --------------------------------------------------------------------------- #
# Only Cloud Scheduler
# --------------------------------------------------------------------------- #


async def test_funding_advance_answers_500_when_a_round_failed_so_the_scheduler_retries(client, scheduler, monkeypatch):
    runs = iter([{"advanced": 2, "contracts": 1, "failed": 1}, {"advanced": 1, "contracts": 1, "failed": 0}])

    async def run(db):
        return next(runs)

    monkeypatch.setattr(acceptance, "run_funding_advance", run)
    client.cookies.clear()
    headers = {"Authorization": "Bearer valid"}
    failed = await client.post(FUNDING_ADVANCE, headers=headers)
    assert failed.status_code == 500, failed.text
    assert failed.json() == {"job": "funding-advance", "advanced": 2, "contracts": 1, "failed": 1}
    retried = await client.post(FUNDING_ADVANCE, headers=headers)
    assert retried.status_code == 200 and retried.json()["failed"] == 0


@pytest.mark.parametrize("path", [REMINDERS, FEES])
async def test_jobs_do_nothing_until_configured(client, monkeypatch, path):
    monkeypatch.delenv("SCHEDULER_SERVICE_ACCOUNT", raising=False)
    monkeypatch.delenv("SCHEDULER_AUDIENCE", raising=False)
    client.cookies.clear()
    resp = await client.post(path, headers={"Authorization": "Bearer valid"})
    assert resp.status_code == 503


@pytest.mark.parametrize("path", [REMINDERS, FEES])
async def test_only_the_scheduler_s_own_token_runs_a_job(client, scheduler, outbox, path):
    client.cookies.clear()
    assert (await client.post(path)).status_code == 401
    for token, expected in (("forged", 401), ("someone-else", 403), ("unverified", 403)):
        resp = await client.post(path, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == expected, (token, resp.text)

    resp = await client.post(path, headers={"Authorization": "Bearer valid"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"] == path.rsplit("/", 1)[-1]


async def test_an_admin_session_is_not_a_scheduler(client, scheduler, make_admin, path=REMINDERS):
    admin = await make_admin()
    resp = await client.post(path, headers=admin["headers"])
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# The repayment reminder
# --------------------------------------------------------------------------- #


async def test_each_sme_is_reminded_once_a_working_day(db_session, outbox):
    world, first, second = await _repaying_round(db_session)
    today_count, today_vnd = await _due(db_session, second)
    earlier_count, earlier_vnd = await _due(db_session, first)
    # The first day also carries each investment's short stub period.
    assert today_count == 10 and earlier_count >= 10

    result = await reminders.send_repayment_reminders(db_session, now=_morning(second))
    assert result == {"sent": 1, "failed": 0, "skipped": None}
    [email] = outbox
    assert email["to"] == world.sme.email
    assert second.strftime("%d/%m/%Y") in email["subject"]
    for body in (email["html"], email["text"]):
        assert "10 khoản trả" in body and _vnd(today_vnd) in body
        assert f"{earlier_count} khoản" in body and _vnd(earlier_vnd) in body
    assert "You pay each investor directly" in email["text"]
    # Lateness is out of MVP scope: "not marked paid", never late or overdue.
    assert "overdue" not in email["text"].lower() and "late" not in email["text"].lower()

    row = await db_session.get(RepaymentReminder, (world.sme.id, second), populate_existing=True)
    assert (row.status, row.channel, row.due_count, row.due_vnd, row.earlier_count) == (
        "SENT", "PLATFORM", 10, today_vnd, earlier_count,
    )

    # Called again (a scheduler retry): nothing more.
    again = await reminders.send_repayment_reminders(db_session, now=_morning(second))
    assert again["sent"] == 0 and len(outbox) == 1


async def test_nothing_is_sent_on_a_day_off(db_session, outbox):
    _, first, _ = await _repaying_round(db_session)
    saturday = first + dt.timedelta(days=(5 - first.weekday()) % 7)
    result = await reminders.send_repayment_reminders(db_session, now=_morning(saturday))
    assert result["skipped"] == "NOT_A_WORKING_DAY" and outbox == []


async def test_days_marked_paid_are_not_reminded(db_session, outbox):
    _, _, second = await _repaying_round(db_session)
    await db_session.execute(
        update(RepaymentDay).where(RepaymentDay.due_date <= second).values(status="PRESUMED_PAID")
    )
    await db_session.commit()
    result = await reminders.send_repayment_reminders(db_session, now=_morning(second))
    assert result["sent"] == 0 and outbox == []


async def test_only_earlier_days_still_get_a_reminder(db_session, outbox):
    _, first, second = await _repaying_round(db_session)
    await db_session.execute(
        update(RepaymentDay).where(RepaymentDay.due_date == second).values(status="PRESUMED_PAID")
    )
    await db_session.commit()
    await reminders.send_repayment_reminders(db_session, now=_morning(second))
    [email] = outbox
    assert "không có khoản trả mới đến hạn" in email["text"]
    assert "not marked paid yet" in email["text"]


async def test_a_failed_reminder_is_retried_by_the_next_run(db_session, outbox, monkeypatch):
    world, _, second = await _repaying_round(db_session)

    def smtp_down(*args, **kwargs):
        raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr(system_email, "send_email", smtp_down)
    result = await reminders.send_repayment_reminders(db_session, now=_morning(second))
    assert result == {"sent": 0, "failed": 1, "skipped": None}
    row = await db_session.get(RepaymentReminder, (world.sme.id, second), populate_existing=True)
    assert (row.status, row.attempts, row.last_error) == ("FAILED", 1, "SMTPServerDisconnected")

    monkeypatch.setattr(system_email, "send_email", lambda to, subject, html, text=None: outbox.append(to) or True)
    result = await reminders.send_repayment_reminders(db_session, now=_morning(second))
    assert result["sent"] == 1
    row = await db_session.get(RepaymentReminder, (world.sme.id, second), populate_existing=True)
    assert (row.status, row.attempts) == ("SENT", 2)


@pytest.mark.parametrize("path", [REPAYMENT_DAY, INVOICES, CIC_DAILY_BALANCE])
async def test_the_money_timer_routes_need_the_scheduler_token(client, monkeypatch, scheduler, path):
    client.cookies.clear()
    assert (await client.post(path)).status_code == 401
    for token, expected in (("forged", 401), ("someone-else", 403), ("unverified", 403)):
        resp = await client.post(path, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == expected, (token, resp.text)
    resp = await client.post(path, headers={"Authorization": "Bearer valid"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"] == path.rsplit("/", 1)[-1]
    monkeypatch.delenv("SCHEDULER_SERVICE_ACCOUNT")
    assert (await client.post(path, headers={"Authorization": "Bearer valid"})).status_code == 503
