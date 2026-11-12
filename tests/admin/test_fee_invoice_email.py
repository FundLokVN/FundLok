"""The SME is emailed its platform activation-fee invoice (3% of the confirmed
total plus VAT, charged once per round) as soon as the round closes.
Spec: docs/specs/admin/fee-invoice-email.md.

The invoice itself comes from Edward's app/direct_transfer (close + fees);
these tests only drive it to closing. No email leaves: `outbox` captures the
platform relay, and the reminder sender (Gmail) is replaced where it is used.
"""
import dataclasses
import smtplib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.admin.fee_notice import service
from app.admin.fee_notice.models import FeeInvoiceNotice
from app.direct_transfer.models import FeeInvoice
from tests.direct_transfer.helpers import at, close_now, fund_window, make_world
from tests.market.test_funding_flow import _open_project
from tests.market.test_round_lifecycle import _approved

M = 1_000_000
SOON = at(days=1, hours=1)


@pytest.fixture
def outbox(monkeypatch):
    """Emails sent through the platform relay. No reminder sender is chosen
    unless a test sets one."""
    sent: list[dict] = []

    def fake_send_email(to, subject, html, text=None, reply_to=None):
        sent.append({"to": to, "subject": subject, "html": html, "text": text})
        return True

    async def no_sender(db):
        return None

    monkeypatch.setattr(service, "send_email", fake_send_email)
    monkeypatch.setattr(service.email_service, "resolve_reminder_sender", no_sender)
    return sent


async def _closed_round(db, **params):
    """A 100M round, fully confirmed and closed: Edward's close issues the
    SME_ACTIVATION invoice (3,000,000 + 300,000 VAT)."""
    world = await make_world(db)
    if params:
        world.params = dataclasses.replace(world.params, **params)
    await fund_window(world, [10 * M] * 10)
    await close_now(world, now=at(days=1))
    await db.commit()
    invoice = (
        await db.execute(select(FeeInvoice).where(FeeInvoice.kind == "SME_ACTIVATION"))
    ).scalar_one()
    return world, invoice


async def _notice(db, invoice) -> FeeInvoiceNotice | None:
    return await db.get(FeeInvoiceNotice, invoice.id, populate_existing=True)


async def test_the_sme_is_emailed_its_activation_fee_once(db_session, outbox):
    world, invoice = await _closed_round(db_session)

    sent = await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params)
    assert sent == 1
    [email] = outbox
    assert email["to"] == world.sme.email
    assert invoice.invoice_number in email["subject"]
    for body in (email["html"], email["text"]):
        for amount in ("100.000.000 VND", "3.000.000 VND", "300.000 VND", "3.300.000 VND"):
            assert amount in body
        assert invoice.reference_code in body
        assert world.project.legal_name in body
    # The handbook: a separate fee, never taken out of the money received.
    assert "never deducted" in email["text"]

    notice = await _notice(db_session, invoice)
    assert (notice.status, notice.channel, notice.attempts, notice.sent_to) == (
        "SENT", "PLATFORM", 1, world.sme.email,
    )
    assert notice.sent_at is not None

    # Charged once, emailed once.
    assert await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params) == 0
    assert len(outbox) == 1


async def test_the_email_says_where_to_pay(db_session, outbox):
    world, _ = await _closed_round(
        db_session,
        fee_account_bin="970436",
        fee_account_number="0071000123456",
        fee_account_name="CONG TY CO PHAN FUNDLOK",
    )
    await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params)
    [email] = outbox
    assert "0071000123456" in email["html"] and "CONG TY CO PHAN FUNDLOK" in email["html"]
    assert "reply to this email" not in email["text"]


async def test_without_fundlok_s_account_the_sme_is_told_to_reply(db_session, outbox):
    world, _ = await _closed_round(db_session, fee_account_bin=None, fee_account_number=None)
    await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params)
    [email] = outbox
    assert "reply to this email" in email["text"]


async def test_the_reminder_sender_goes_first(db_session, outbox, monkeypatch):
    world, invoice = await _closed_round(db_session)
    calls = []

    async def sender(db):
        return SimpleNamespace(account=SimpleNamespace(gmail_address="ops@fundlok.com"))

    async def send(db, chosen, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(service.email_service, "resolve_reminder_sender", sender)
    monkeypatch.setattr(service.email_service, "send_as_reminder_sender", send)

    assert await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params) == 1
    assert outbox == []
    [call] = calls
    assert call["recipients"] == [world.sme.email]
    assert call["content"]["fee_invoice_id"] == str(invoice.id)
    assert "3.300.000 VND" in call["html"]
    notice = await _notice(db_session, invoice)
    assert (notice.channel, notice.from_address) == ("GMAIL", "ops@fundlok.com")


async def test_the_platform_email_takes_over_when_gmail_cannot_send(db_session, outbox, monkeypatch):
    world, invoice = await _closed_round(db_session)

    async def sender(db):
        return SimpleNamespace(account=SimpleNamespace(gmail_address="ops@fundlok.com"))

    async def refused(db, chosen, **kwargs):
        raise HTTPException(status_code=502, detail="down", headers={"X-Error-Code": "GMAIL_UNAVAILABLE"})

    monkeypatch.setattr(service.email_service, "resolve_reminder_sender", sender)
    monkeypatch.setattr(service.email_service, "send_as_reminder_sender", refused)

    assert await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params) == 1
    assert [e["to"] for e in outbox] == [world.sme.email]
    assert (await _notice(db_session, invoice)).channel == "PLATFORM"


async def test_a_failed_send_is_retried_by_the_next_run(db_session, outbox, monkeypatch):
    world, invoice = await _closed_round(db_session)

    def smtp_down(*args, **kwargs):
        raise smtplib.SMTPServerDisconnected("gone")

    monkeypatch.setattr(service, "send_email", smtp_down)
    assert await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params) == 0
    notice = await _notice(db_session, invoice)
    assert (notice.status, notice.attempts, notice.last_error) == ("FAILED", 1, "SMTPServerDisconnected")

    monkeypatch.setattr(service, "send_email", lambda to, subject, html, text=None: outbox.append(to) or True)
    assert await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params) == 1
    notice = await _notice(db_session, invoice)
    assert (notice.status, notice.attempts, notice.last_error) == ("SENT", 2, None)


async def test_it_gives_up_after_the_last_attempt(db_session, outbox):
    world, invoice = await _closed_round(db_session)
    db_session.add(
        FeeInvoiceNotice(
            invoice_id=invoice.id, status="FAILED", sent_to=world.sme.email,
            attempts=service.MAX_ATTEMPTS, last_error="SMTPServerDisconnected",
        )
    )
    await db_session.commit()
    assert await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params) == 0
    assert outbox == []


async def test_only_a_new_unpaid_invoice_is_emailed(db_session, outbox):
    world, invoice = await _closed_round(db_session)
    # Past its payment period: the notice is for a new invoice.
    late = at(days=1 + world.params.sme_invoice_due_days + 1)
    assert await service.send_pending_fee_invoice_emails(db_session, now=late, params=world.params) == 0

    invoice.status = "PAID"
    await db_session.commit()
    assert await service.send_pending_fee_invoice_emails(db_session, now=SOON, params=world.params) == 0
    assert outbox == []


async def test_advancing_a_round_emails_the_sme_its_fee(
    client, db_session, make_admin, make_user, make_eligible_investor, outbox
):
    setup = await _open_project(client, db_session, make_admin, make_user)  # minimum 40M
    admin, sme = setup["admin"], setup["sme"]
    window_id = setup["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Nguyen Van An"))
    await _approved(client, admin, investor, window_id, 40 * M)
    closing = await client.post(f"/funding/windows/{window_id}/close-early", headers=sme["headers"])
    assert closing.status_code == 200, closing.text
    # The investment repays from approval, but the fee waits for the round to close.
    assert outbox == []

    advanced = await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=admin["headers"])
    assert advanced.status_code == 200 and advanced.json()["status"] == "REPAYING", advanced.text

    # Sent after the response, once: 3% of 40M is 1,200,000 plus 120,000 VAT.
    [email] = outbox
    assert email["to"] == sme["email"]
    assert "1.200.000 VND" in email["html"] and "1.320.000 VND" in email["html"]
    again = await client.post(f"/admin/funding/rounds/{window_id}/advance", headers=admin["headers"])
    assert again.status_code == 200
    assert len(outbox) == 1
