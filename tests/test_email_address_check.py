"""send_email refuses to send to a malformed address.

Every email leaves through app.utils.email.send_email, so the address check
lives there and no caller can skip it. Senders run as background tasks after
the response has gone, so a bad address is logged and skipped (send_email
returns False) rather than raised. SMTP is faked: the tests see what would
have been handed to the mail server, and whether a connection was opened.
"""
import logging

import pytest

from app.utils import email as email_module

# conftest's autouse backstop replaces send_email with a no-op in every test so
# nothing can reach a mail server. These tests need the real function, so it is
# captured at import (before any fixture runs) and restored per test. SMTP
# itself is replaced by FakeSMTP, so the network is still never touched.
REAL_SEND_EMAIL = email_module.send_email


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port):
        self.sent = []
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        pass

    def sendmail(self, from_addr, to_addr, message):
        self.sent.append((to_addr, message))

    def quit(self):
        pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(email_module, "send_email", REAL_SEND_EMAIL)
    monkeypatch.setattr(email_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_module.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.instances


def _send(to, **kw):
    return email_module.send_email(to_email=to, subject="Test subject", html_content="<p>hi</p>", **kw)


@pytest.mark.parametrize(
    "address",
    ["phat.huynh@fundlok.com", "user+tag@gmail.com", "nguyễn@ví-dụ.vn", "x@example.com"],
)
def test_a_well_formed_address_is_sent_to(smtp, address):
    assert _send(address) is True
    [server] = smtp
    [(to, _)] = server.sent
    assert to == email_module.valid_email_address(address)


def test_the_domain_is_normalised(smtp):
    _send("Victim@Example.COM")
    [(to, message)] = smtp[0].sent
    assert to == "Victim@example.com"
    assert "To: Victim@example.com" in message


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "not-an-email",
        "a@",
        "@b.com",
        "a@b",
        "a b@c.com",
        "a@@b.com",
        "a@localhost",
        None,
        123,
    ],
)
def test_a_malformed_address_is_not_sent_to(smtp, address):
    assert _send(address) is False
    assert smtp == [], "no SMTP connection may even be opened"


def test_a_newline_in_the_address_cannot_add_headers(smtp):
    # The address goes into the To header; a newline would add a Bcc of its own.
    assert _send("a@b.com\r\nBcc: everyone@example.com") is False
    assert _send("a@b.com\nBcc: everyone@example.com") is False
    assert smtp == []


def test_a_skipped_email_is_logged_without_the_address(smtp, caplog):
    with caplog.at_level(logging.WARNING, logger="app.utils.email"):
        _send("not-an-email")

    assert "Test subject" in caplog.text
    assert "not-an-email" not in caplog.text


def test_a_malformed_reply_to_is_dropped_but_the_email_still_goes(smtp):
    assert _send("team@fundlok.com", reply_to="not-an-email") is True
    [(_, message)] = smtp[0].sent
    assert "Reply-To" not in message


def test_a_valid_reply_to_is_kept(smtp):
    _send("team@fundlok.com", reply_to="sender@example.com")
    [(_, message)] = smtp[0].sent
    assert "Reply-To: sender@example.com" in message


def test_every_sender_goes_through_the_check(smtp):
    # The named helpers all build on send_email, so they inherit the check.
    email_module.send_contact_autoreply(to_email="not-an-email")
    email_module.send_verification_email(to_email="not-an-email", token="t")
    email_module.send_password_reset_email(to_email="not-an-email", token="t")
    email_module.send_new_device_signin_alert(
        "not-an-email", device="Mac", browser="Safari 18", ip_address=None
    )
    assert smtp == []
