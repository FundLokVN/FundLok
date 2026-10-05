"""What the contact form can make FundLok send, and to whom.

The form is public and anonymous, and the auto-reply goes to whatever address
the submitter typed. It used to greet "Hi {name}" with the name unescaped, so
a submitter could send a FundLok-branded email containing their own HTML
(links, fake buttons) to any inbox: a phishing kit with our logo and domain.

The auto-reply now carries no submitted text at all; the team notification
still carries everything, escaped.
"""
import inspect

import pytest

from app.utils import email as email_module

PHISHING_NAME = (
    '<a href="https://evil.example/login">Your FundLok account is locked - verify here</a>'
    "<img src=x onerror=alert(1)>"
)
BAIT_TEXT = "Your FundLok account is locked"


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(email_module, "send_email", lambda **kw: sent.append(kw))
    return sent


def _by_recipient(outbox, to):
    return [m for m in outbox if m["to_email"] == to]


async def _submit(client, **overrides):
    body = {
        "name": PHISHING_NAME,
        "email": "victim@example.com",
        "purpose": "support",
        "subject": "Hello",
        "message": "<script>alert(1)</script>\nsecond line",
    }
    body.update(overrides)
    return await client.post("/contact", json=body)


async def test_the_autoreply_carries_nothing_the_submitter_wrote(client, outbox):
    resp = await _submit(client)
    assert resp.status_code == 200, resp.text

    [reply] = _by_recipient(outbox, "victim@example.com")
    for part in (reply["html_content"], reply["text_content"], reply["subject"]):
        assert "evil.example" not in part
        assert BAIT_TEXT not in part
        assert "<a href=\"https://evil" not in part
        assert "onerror" not in part


async def test_the_autoreply_greets_generically(client, outbox):
    await _submit(client, name="Nguyễn Văn A")

    [reply] = _by_recipient(outbox, "victim@example.com")
    assert "Nguyễn Văn A" not in reply["html_content"]
    assert "Hello," in reply["html_content"]
    assert reply["text_content"].startswith("Hello,")


def test_the_autoreply_cannot_be_given_a_name():
    # Structural guard: nobody can reintroduce submitted text by passing it in.
    assert list(inspect.signature(email_module.send_contact_autoreply).parameters) == ["to_email"]


async def test_the_team_still_gets_the_details_escaped(client, outbox, monkeypatch):
    monkeypatch.setattr(email_module.settings, "CONTACT_INBOX_EMAIL", "team@fundlok.test")
    await _submit(client)

    [note] = _by_recipient(outbox, "team@fundlok.test")
    html = note["html_content"]
    assert "&lt;a href=&quot;https://evil.example/login&quot;&gt;" in html
    assert '<a href="https://evil.example' not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    # The sender stays reachable, as Reply-To rather than From.
    assert note["reply_to"] == "victim@example.com"


async def test_header_injection_in_the_name_is_flattened(client, outbox, monkeypatch):
    monkeypatch.setattr(email_module.settings, "CONTACT_INBOX_EMAIL", "team@fundlok.test")
    await _submit(client, name="Mallory\r\nBcc: everyone@example.com")

    [note] = _by_recipient(outbox, "team@fundlok.test")
    assert "\n" not in note["subject"] and "\r" not in note["subject"]


def test_the_signin_alert_escapes_what_it_derives_from_the_user_agent(outbox):
    email_module.send_new_device_signin_alert(
        "owner@example.com",
        device="<b>Windows</b>",
        browser='<a href="https://evil.example">Chrome</a>',
        ip_address="<script>1</script>",
    )

    [alert] = outbox
    html = alert["html_content"]
    assert '<a href="https://evil.example">' not in html
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;" in html
    assert "<b>Windows</b>" not in html and "&lt;b&gt;Windows&lt;/b&gt;" in html
    assert "<script>1</script>" not in html
