"""The React Email templates (emails/src/templates/), as exported to
app/email_templates/ and filled in by app/utils/email_templates.py.

These guard the seam between the two: every exported file renders, nothing
is left unfilled, values are escaped the way the old f-string templates
escaped them, and the platform emails still say what they said.
"""
from pathlib import Path

import pytest
from jinja2 import UndefinedError, meta

from app.utils import email as email_module
from app.utils import email_templates
from app.utils.email_templates import render_email, template_names

SOURCES = Path(__file__).resolve().parents[1] / "emails" / "src" / "templates"


def _variables(name: str) -> set[str]:
    env = email_templates._environment()
    source = env.loader.get_source(env, f"{name}.html")[0]
    return meta.find_undeclared_variables(env.parse(source))


def test_every_source_template_has_been_exported():
    # Catches a template added in emails/ without `npm run export`. (It cannot
    # catch an edit that was not re-exported: that needs Node.)
    sources = sorted(path.stem for path in SOURCES.glob("*.tsx"))
    assert sources == template_names()


@pytest.mark.parametrize("name", template_names())
def test_every_exported_template_renders(name):
    # logo_src and year have defaults; everything else is supplied.
    context = {var: "value" for var in _variables(name) - {"logo_src", "year"}}
    html = render_email(name, **context)
    assert "{{" not in html and "{%" not in html
    assert 'src="cid:fundlok-logo"' in html
    assert html.startswith("<!DOCTYPE html")


def test_a_missing_variable_is_an_error_not_a_blank():
    with pytest.raises(UndefinedError):
        render_email("verification")


def test_values_are_escaped_like_html_escape():
    html = render_email(
        "contact-notification",
        purpose="General",
        name='<a href="https://evil.example">x</a>',
        email="a@example.com",
        topic="'quoted' & more",
        message="<b>one</b>\r\ntwo",
    )
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;x&lt;/a&gt;" in html
    assert "&#x27;quoted&#x27; &amp; more" in html
    assert "&lt;b&gt;one&lt;/b&gt;<br/>two" in html


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(email_module, "send_email", lambda **kw: sent.append(kw))
    return sent


@pytest.mark.parametrize(
    ("send", "phrases"),
    [
        (
            lambda: email_module.send_verification_email("a@example.com", "tok"),
            ["Verify Your Email Address", "This verification link will expire in 24 hours.",
             "/verify-email?token=tok", "All rights reserved."],
        ),
        (
            lambda: email_module.send_password_reset_email("a@example.com", "tok"),
            ["Reset Your Password", "This password reset link will expire in 1 hour.",
             "If you did not request a password reset, please ignore this email.",
             "/reset-password?token=tok"],
        ),
        (
            lambda: email_module.send_admin_invite_email(
                to_email="a@example.com", token="tok", invited_by="Phat", expires_hours=24
            ),
            ["Set up your admin account", "<strong>Phat</strong> has created a",
             "This link works once and expires in 24 hours.",
             "/reset-password?token=tok&amp;invite=1"],
        ),
        (
            lambda: email_module.send_password_changed_notice("a@example.com"),
            ["Your password was changed", "was just changed.", "Reset your password"],
        ),
        (
            lambda: email_module.send_password_changed_notice("a@example.com", first_time=True),
            ["Your password was set", "You can now sign in with your email address and password."],
        ),
        (
            lambda: email_module.send_existing_account_notice("a@example.com"),
            ["Your account already exists", "Reset it here", "no changes were made to your account."],
        ),
        (
            lambda: email_module.send_new_device_signin_alert(
                "a@example.com", device="macOS", browser="Chrome", ip_address=None
            ),
            ["New sign-in to your FundLok account", "<strong>Chrome on macOS</strong>",
             "(an unknown address)", "Review signed-in devices"],
        ),
        (
            lambda: email_module.send_application_updated_email("a@example.com"),
            ["Hồ sơ vay của bạn đã được cập nhật", "Your loan application has been updated",
             "Xem trên FundLok / View on FundLok"],
        ),
        (
            lambda: email_module.send_contact_autoreply("a@example.com"),
            ["Thank you for contacting FundLok!", "Hello,", "The FundLok Team"],
        ),
    ],
)
def test_platform_emails_keep_their_wording(outbox, send, phrases):
    send()
    [sent] = outbox
    for phrase in phrases:
        assert phrase in sent["html_content"], phrase
    assert "FundLok · This is an automated message, please do not reply." in sent["html_content"]
