"""Admin > Email: every admin connects their own Gmail account with their own
App Password, then composes and sends templated email from it. A system admin
sets the org-wide allowed recipient domains and sees every admin's sends.
Spec: docs/specs/admin/internal-email.md.

Gmail is never contacted: `FakeGmail` replaces the SMTP connection
(transport._connect), and conftest makes any unpatched connection fail.
"""
import asyncio
import json
import smtplib
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.admin.internal_email import crypto, service
from app.admin.internal_email.models import EmailSettings, InternalEmail
from app.admin.internal_email.schemas import SendEmailRequest
from app.core.database import SessionLocal
from app.lending.models import AuditLog
from app.users.models import User

APP_PASSWORD = "abcd efgh ijkl mnop"  # as Google displays it
PLAIN_PASSWORD = "abcdefghijklmnop"

SETTINGS = "/admin/email/settings"


class FakeGmail:
    """Stands in for smtplib.SMTP_SSL: records logins and messages."""

    def __init__(self):
        self.logins: list[tuple[str, str]] = []
        self.sent = []
        self.reject_login = False
        self.unreachable = False

    def __call__(self):
        if self.unreachable:
            raise TimeoutError("timed out")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if self.reject_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.logins.append((user, password))

    def send_message(self, message):
        self.sent.append(message)
        return {}


@pytest.fixture
def gmail(monkeypatch):
    server = FakeGmail()
    monkeypatch.setattr("app.admin.internal_email.transport._connect", server)
    return server


def _settings(**overrides) -> dict:
    body = {
        "gmail_address": "team@fundlok.com",
        "from_name": "FundLok Team",
        "app_password": APP_PASSWORD,
        "enabled": True,
    }
    body.update(overrides)
    return body


def _message(**overrides) -> dict:
    body = {
        "to": ["colleague@fundlok.com"],
        "template": "general",
        "subject": "Friday review",
        "heading": "The review moves to Friday",
        "body": "Line one\nLine two",
        "button_label": None,
        "button_url": None,
    }
    body.update(overrides)
    return body


async def _connect(client, admin, **overrides) -> dict:
    resp = await client.put(SETTINGS, json=_settings(**overrides), headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _row(db_session, user: dict) -> EmailSettings | None:
    """That admin's own settings row."""
    return (
        await db_session.execute(
            select(EmailSettings)
            .where(EmailSettings.user_id == UUID(user["id"]))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _html_part(message) -> str:
    return message.get_body(preferencelist=("html",)).get_content()


def _text_part(message) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #

POLICY = "/admin/email/policy"

ENDPOINTS = [
    ("get", SETTINGS, None),
    ("put", SETTINGS, _settings()),
    ("delete", SETTINGS, None),
    ("post", f"{SETTINGS}/test", None),
    ("post", "/admin/email/preview", {k: v for k, v in _message().items() if k != "to"}),
    ("post", "/admin/email/send", _message()),
    ("get", "/admin/email/messages", None),
    ("get", POLICY, None),
    ("put", POLICY, {"allowed_domains": []}),
]


async def test_only_admins_can_use_internal_email(client, make_user):
    investor = await make_user(role="INVESTOR")
    sme = await make_user(role="SME")
    # Signing in left cookies on the shared client; the anonymous request
    # below must carry none. The users authenticate by header.
    client.cookies.clear()
    for method, path, body in ENDPOINTS:
        kwargs = {"json": body} if body is not None else {}
        for who in (investor, sme):
            resp = await client.request(method, path, headers=who["headers"], **kwargs)
            assert resp.status_code == 403, (method, path, resp.text)
        anonymous = await client.request(method, path, **kwargs)
        assert anonymous.status_code == 401, (method, path, anonymous.text)


async def test_every_admin_can_connect_and_send_but_only_a_system_admin_sets_policy(
    client, make_admin, gmail
):
    admin = await make_admin()
    connected = await _connect(client, admin, gmail_address=admin["email"])
    assert connected["configured"] is True

    test = await client.post(f"{SETTINGS}/test", headers=admin["headers"])
    assert test.status_code == 200, test.text
    sent = await client.post("/admin/email/send", json=_message(), headers=admin["headers"])
    assert sent.status_code == 200, sent.text
    assert sent.json()["from_address"] == admin["email"]

    assert (await client.get(POLICY, headers=admin["headers"])).status_code == 200
    resp = await client.put(POLICY, json={"allowed_domains": []}, headers=admin["headers"])
    assert resp.status_code == 403


async def test_each_admin_has_their_own_account(client, make_admin, make_system_admin, db_session, gmail):
    alice = await make_admin()
    bob = await make_admin()
    sysadmin = await make_system_admin()
    await _connect(client, alice, gmail_address="alice@fundlok.com", from_name="Alice")

    # Bob sees nothing of Alice's, and cannot send with it.
    bobs = (await client.get(SETTINGS, headers=bob["headers"])).json()
    assert bobs["configured"] is False and bobs["gmail_address"] is None
    resp = await client.post("/admin/email/send", json=_message(), headers=bob["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_CONFIGURED"

    # His own connection is a separate row; Alice's is untouched.
    await _connect(
        client, bob, gmail_address="bob@fundlok.com", from_name="Bob", app_password="qrst uvwx yzab cdef"
    )
    assert (await _row(db_session, alice)).gmail_address == "alice@fundlok.com"
    assert crypto.decrypt_password((await _row(db_session, alice)).app_password_encrypted) == PLAIN_PASSWORD

    for who in (alice, bob):
        ok = await client.post("/admin/email/send", json=_message(), headers=who["headers"])
        assert ok.status_code == 200, ok.text
    # Each send logged in with its own admin's credentials and From.
    assert gmail.logins == [
        ("alice@fundlok.com", PLAIN_PASSWORD),
        ("bob@fundlok.com", "qrstuvwxyzabcdef"),
    ]
    assert "Alice" in gmail.sent[0]["From"] and "alice@fundlok.com" in gmail.sent[0]["From"]
    assert "Bob" in gmail.sent[1]["From"] and "bob@fundlok.com" in gmail.sent[1]["From"]

    # Disconnecting is one's own business too.
    await client.delete(SETTINGS, headers=bob["headers"])
    assert await _row(db_session, bob) is None
    assert await _row(db_session, alice) is not None

    # An admin sees their own sends; a system admin sees everyone's.
    alices = (await client.get("/admin/email/messages", headers=alice["headers"])).json()
    assert [m["from_address"] for m in alices] == ["alice@fundlok.com"]
    every = (await client.get("/admin/email/messages", headers=sysadmin["headers"])).json()
    assert sorted(m["from_address"] for m in every) == ["alice@fundlok.com", "bob@fundlok.com"]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


async def test_settings_start_unconfigured(client, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.get(SETTINGS, headers=sysadmin["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["has_app_password"] is False
    assert body["gmail_address"] is None
    assert body["enabled"] is False


async def test_the_app_password_is_stored_encrypted_and_never_returned(
    client, make_system_admin, db_session
):
    sysadmin = await make_system_admin()
    resp = await client.put(SETTINGS, json=_settings(), headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True and body["has_app_password"] is True
    assert "app_password" not in body
    assert PLAIN_PASSWORD not in resp.text

    row = await _row(db_session, sysadmin)
    assert row.app_password_encrypted.startswith("v1:")
    assert PLAIN_PASSWORD not in row.app_password_encrypted
    assert crypto.decrypt_password(row.app_password_encrypted) == PLAIN_PASSWORD

    again = await client.get(SETTINGS, headers=sysadmin["headers"])
    assert PLAIN_PASSWORD not in again.text and "v1:" not in again.text


async def test_a_blank_password_keeps_the_stored_one(client, make_system_admin, db_session):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    stored = (await _row(db_session, sysadmin)).app_password_encrypted

    for blank in (None, "", "   "):
        body = await _connect(client, sysadmin, app_password=blank, from_name="Ops")
        assert body["from_name"] == "Ops"
        assert body["has_app_password"] is True
    assert (await _row(db_session, sysadmin)).app_password_encrypted == stored


async def test_changing_the_address_requires_a_new_password(client, make_system_admin):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)

    resp = await client.put(
        SETTINGS,
        json=_settings(gmail_address="other@fundlok.com", app_password=None),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "APP_PASSWORD_REQUIRED"

    ok = await _connect(client, sysadmin, gmail_address="other@fundlok.com")
    assert ok["gmail_address"] == "other@fundlok.com"


async def test_enabling_requires_a_password(client, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.put(
        SETTINGS, json=_settings(app_password=None, enabled=True), headers=sysadmin["headers"]
    )
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "APP_PASSWORD_REQUIRED"

    # Saving the address alone, switched off, is fine.
    body = await _connect(client, sysadmin, app_password=None, enabled=False)
    assert body["configured"] is False
    assert body["gmail_address"] == "team@fundlok.com"


@pytest.mark.parametrize(
    "password",
    [
        "short",
        "abcdefghijklmnopq",  # 17
        "abcd-efgh-ijkl-mnop",
        "abcdefghijklmnoé",  # a non-ASCII letter
        "abcdefghijklmno٣",  # a non-ASCII digit
    ],
)
async def test_a_malformed_app_password_is_rejected(client, make_system_admin, password):
    sysadmin = await make_system_admin()
    resp = await client.put(
        SETTINGS, json=_settings(app_password=password), headers=sysadmin["headers"]
    )
    assert resp.status_code == 422, resp.text


async def test_the_domain_policy_is_normalised_checked_and_audited(
    client, make_system_admin, db_session
):
    sysadmin = await make_system_admin()
    resp = await client.put(
        POLICY,
        json={"allowed_domains": ["@FundLok.com", "fundlok.com", " partner.vn "]},
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["allowed_domains"] == ["fundlok.com", "partner.vn"]
    assert (await client.get(POLICY, headers=sysadmin["headers"])).json()["allowed_domains"] == [
        "fundlok.com",
        "partner.vn",
    ]

    for bad in (["not a domain"], ["fundlok"], ["evil.com/x"]):
        resp = await client.put(POLICY, json={"allowed_domains": bad}, headers=sysadmin["headers"])
        assert resp.status_code == 422, bad

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "EMAIL_POLICY_UPDATED"))
    ).scalar_one()
    assert audit.after_state == {"allowed_domains": ["fundlok.com", "partner.vn"]}


async def test_saving_fails_closed_without_an_encryption_key(
    client, make_system_admin, db_session, monkeypatch
):
    sysadmin = await make_system_admin()
    monkeypatch.setattr(crypto.settings, "EMAIL_SETTINGS_ENCRYPTION_KEY", None)

    resp = await client.put(SETTINGS, json=_settings(), headers=sysadmin["headers"])
    assert resp.status_code == 503
    assert resp.headers["X-Error-Code"] == "EMAIL_ENCRYPTION_UNAVAILABLE"
    assert "EMAIL_SETTINGS_ENCRYPTION_KEY" not in resp.text
    assert await _row(db_session, sysadmin) is None


async def test_settings_changes_are_audited_without_the_secret(
    client, make_system_admin, db_session
):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    await _connect(client, sysadmin, app_password=None, from_name="Ops")

    rows = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "EMAIL_SETTINGS_UPDATED")
            .order_by(AuditLog.created_at)
        )
    ).scalars().all()
    assert len(rows) == 2
    for row in rows:
        dumped = json.dumps([row.before_state, row.after_state])
        assert PLAIN_PASSWORD not in dumped and "v1:" not in dumped
        assert str(row.actor_id) == sysadmin["id"]
        # The settings belong to the admin, so the audit row points at them.
        assert str(row.entity_id) == sysadmin["id"]
    assert rows[0].before_state is None
    assert rows[0].after_state["password_changed"] is True
    assert rows[1].after_state["password_changed"] is False
    assert rows[1].after_state["from_name"] == "Ops"


async def test_clearing_removes_the_password_and_disables(client, make_system_admin, db_session):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)

    resp = await client.delete(SETTINGS, headers=sysadmin["headers"])
    assert resp.status_code == 200
    assert resp.json()["configured"] is False
    assert resp.json()["enabled"] is False
    assert await _row(db_session, sysadmin) is None

    cleared = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "EMAIL_SETTINGS_CLEARED"))
    ).scalar_one()
    assert PLAIN_PASSWORD not in json.dumps(cleared.before_state)


# --------------------------------------------------------------------------- #
# Test email
# --------------------------------------------------------------------------- #


async def test_the_test_email_goes_to_the_admin_and_marks_verified(
    client, make_system_admin, db_session, gmail
):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin, enabled=False)  # works while paused

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent_to"] == sysadmin["email"]

    assert gmail.logins == [("team@fundlok.com", PLAIN_PASSWORD)]
    [message] = gmail.sent
    assert message["To"] == sysadmin["email"]
    assert "team@fundlok.com" in _html_part(message)

    settings = (await client.get(SETTINGS, headers=sysadmin["headers"])).json()
    assert settings["last_verified_at"] is not None

    log = (await db_session.execute(select(InternalEmail))).scalar_one()
    assert (log.template, log.status, log.recipients) == ("test", "SENT", [sysadmin["email"]])


async def test_a_rejected_password_is_reported_and_logged(
    client, make_system_admin, db_session, gmail
):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    gmail.reject_login = True

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 502
    assert resp.headers["X-Error-Code"] == "GMAIL_AUTH_FAILED"
    assert PLAIN_PASSWORD not in resp.text and "5.7.8" not in resp.text

    log = (await db_session.execute(select(InternalEmail))).scalar_one()
    assert (log.status, log.error_code) == ("FAILED", "GMAIL_AUTH_FAILED")
    settings = (await client.get(SETTINGS, headers=sysadmin["headers"])).json()
    assert settings["last_verified_at"] is None


async def test_an_unreachable_gmail_is_reported(client, make_system_admin, gmail):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    gmail.unreachable = True

    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 502
    assert resp.headers["X-Error-Code"] == "GMAIL_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


async def test_sending_requires_configured_and_enabled(client, make_system_admin, gmail):
    sysadmin = await make_system_admin()
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_CONFIGURED"

    await _connect(client, sysadmin, enabled=False)
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "EMAIL_DISABLED"
    assert gmail.sent == []


async def test_a_send_reaches_gmail_with_the_rendered_template(
    client, make_system_admin, db_session, gmail
):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)

    resp = await client.post(
        "/admin/email/send",
        json=_message(
            template="announcement",
            to=["colleague@fundlok.com", "Colleague@FundLok.com", "second@fundlok.com"],
            button_label="Open the agenda",
            button_url="https://fundlok.com/agenda?week=41&team=ops",
        ),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "SENT"
    assert out["recipients"] == ["colleague@fundlok.com", "second@fundlok.com"]

    [message] = gmail.sent
    assert message["Subject"] == "Friday review"
    assert message["To"] == "colleague@fundlok.com, second@fundlok.com"
    assert "FundLok Team" in message["From"] and "team@fundlok.com" in message["From"]

    html = _html_part(message)
    assert "Announcement" in html
    assert "The review moves to Friday" in html
    assert "Line one<br/>Line two" in html
    assert 'href="https://fundlok.com/agenda?week=41&amp;team=ops"' in html
    assert 'src="cid:fundlok-logo"' in html
    assert "{{" not in html and "{%" not in html

    logos = [part for part in message.walk() if part.get("Content-ID") == "<fundlok-logo>"]
    assert len(logos) == 1 and logos[0].get_content_disposition() == "inline"

    text = _text_part(message)
    assert "Line one\nLine two" in text
    assert "https://fundlok.com/agenda?week=41&team=ops" in text

    log = (await db_session.execute(select(InternalEmail))).scalar_one()
    assert log.content["body"] == "Line one\nLine two"
    assert str(log.sent_by) == sysadmin["id"]
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "INTERNAL_EMAIL_SENT"))
    ).scalar_one()
    assert audit.entity_id == log.id
    assert audit.after_state["recipient_count"] == 2


async def test_content_is_escaped_not_rendered_as_html(client, make_system_admin, gmail):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)

    resp = await client.post(
        "/admin/email/send",
        json=_message(
            heading='<a href="https://evil.example">Click</a>',
            body="<script>alert(1)</script>\n<img src=x onerror=alert(1)>",
        ),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    html = _html_part(gmail.sent[0])
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert '<a href="https://evil.example">' not in html
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;" in html


@pytest.mark.parametrize(
    "button",
    [
        {"button_label": "Open", "button_url": "javascript:alert(1)"},
        {"button_label": "Open", "button_url": "http://fundlok.com"},
        {"button_label": "Open", "button_url": "https://"},
        {"button_label": "Open", "button_url": "https://fundlok.com/a b"},
        {"button_label": "Open", "button_url": None},
        {"button_label": None, "button_url": "https://fundlok.com"},
    ],
)
async def test_the_button_url_must_be_https(client, make_system_admin, button):
    sysadmin = await make_system_admin()
    body = {k: v for k, v in _message(**button).items() if k != "to"}
    resp = await client.post("/admin/email/preview", json=body, headers=sysadmin["headers"])
    assert resp.status_code == 422, resp.text


async def test_header_injection_is_rejected(client, make_system_admin, gmail):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)

    resp = await client.post(
        "/admin/email/send",
        json=_message(subject="Hello\r\nBcc: everyone@example.com"),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 422

    resp = await client.put(
        SETTINGS, json=_settings(from_name="Team\nBcc: x@example.com"), headers=sysadmin["headers"]
    )
    assert resp.status_code == 422

    # Line breaks the email package recognises beyond CR/LF: a 422 at the
    # schema, never a 500 from the header builder.
    for separator in ("\u2028", "\u2029", "\u0085"):
        resp = await client.post(
            "/admin/email/send",
            json=_message(subject=f"Q3 update{separator}Bcc: x@example.com"),
            headers=sysadmin["headers"],
        )
        assert resp.status_code == 422, separator
        resp = await client.put(
            SETTINGS, json=_settings(from_name=f"Team{separator}Ops"), headers=sysadmin["headers"]
        )
        assert resp.status_code == 422, separator

    # An RFC 2047 encoded word passes email validation but decodes to CR/LF.
    resp = await client.post(
        "/admin/email/send",
        json=_message(to=["=?utf-8?q?x=0d=0aBcc=3a_a=40evil.com?=@fundlok.com"]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 422
    assert gmail.sent == []


async def test_recipients_outside_allowed_domains_are_refused(client, make_system_admin, gmail):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    policy = await client.put(
        POLICY, json={"allowed_domains": ["fundlok.com"]}, headers=sysadmin["headers"]
    )
    assert policy.status_code == 200, policy.text

    resp = await client.post(
        "/admin/email/send",
        json=_message(to=["a@fundlok.com", "outsider@gmail.com", "b@mail.fundlok.com"]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "RECIPIENT_DOMAIN_NOT_ALLOWED"
    assert "outsider@gmail.com" in resp.text
    # Exact match: a subdomain is a different domain.
    assert "b@mail.fundlok.com" in resp.text
    assert gmail.sent == []

    ok = await client.post(
        "/admin/email/send", json=_message(to=["a@FundLok.com"]), headers=sysadmin["headers"]
    )
    assert ok.status_code == 200, ok.text


async def test_too_many_recipients_are_rejected(client, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.post(
        "/admin/email/send",
        json=_message(to=[f"person{i}@fundlok.com" for i in range(21)]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 422


async def test_the_hourly_limit_applies(client, make_system_admin, gmail, monkeypatch):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    monkeypatch.setattr(service, "SENDS_PER_HOUR", 2)

    for _ in range(2):
        ok = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
        assert ok.status_code == 200, ok.text
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 429
    assert resp.headers["X-Error-Code"] == "EMAIL_RATE_LIMITED"
    assert len(gmail.sent) == 2


async def test_the_hourly_limit_is_per_admin(client, make_admin, gmail, monkeypatch):
    # One admin using up the hour must not block another.
    alice = await make_admin()
    bob = await make_admin()
    await _connect(client, alice, gmail_address="alice@fundlok.com")
    await _connect(client, bob, gmail_address="bob@fundlok.com")
    monkeypatch.setattr(service, "SENDS_PER_HOUR", 1)

    ok = await client.post("/admin/email/send", json=_message(), headers=alice["headers"])
    assert ok.status_code == 200, ok.text
    limited = await client.post("/admin/email/send", json=_message(), headers=alice["headers"])
    assert limited.status_code == 429
    bobs = await client.post("/admin/email/send", json=_message(), headers=bob["headers"])
    assert bobs.status_code == 200, bobs.text


async def test_the_hourly_limit_holds_under_concurrent_sends(
    client, make_system_admin, gmail, monkeypatch
):
    # Straight at the service, each send in its own session (the test client
    # shares one session across requests, so it cannot race). Without the
    # per-admin lock, the sends all read the same count before any of them
    # inserts, and all pass.
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    monkeypatch.setattr(service, "SENDS_PER_HOUR", 3)
    body = SendEmailRequest(**_message())

    async def one_send() -> int:
        async with SessionLocal() as session:
            actor = await session.get(User, UUID(sysadmin["id"]))
            try:
                await service.send(session, body, actor=actor, ip_address=None)
            except HTTPException as exc:
                return exc.status_code
            return 200

    codes = await asyncio.gather(*[one_send() for _ in range(8)])
    assert codes.count(200) == 3, codes
    assert codes.count(429) == 5, codes
    assert len(gmail.sent) == 3


async def test_the_test_email_only_verifies_the_credentials_it_tested(
    client, make_system_admin, db_session, gmail, monkeypatch
):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)

    # Someone saves a new password while the test email is with Gmail.
    real_send_logged = service._send_logged

    async def password_changed_mid_send(db, **kwargs):
        log = await real_send_logged(db, **kwargs)
        row = (await db.execute(select(EmailSettings))).scalar_one()
        row.app_password_encrypted = crypto.encrypt_password("qrstuvwxyzabcdef")
        row.last_verified_at = None
        await db.commit()
        return log

    monkeypatch.setattr(service, "_send_logged", password_changed_mid_send)

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    settings = (await client.get(SETTINGS, headers=sysadmin["headers"])).json()
    assert settings["last_verified_at"] is None


async def test_preview_renders_without_sending(client, make_system_admin, db_session, gmail):
    sysadmin = await make_system_admin()
    body = {k: v for k, v in _message(template="action_required").items() if k != "to"}

    resp = await client.post("/admin/email/preview", json=body, headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["subject"] == "Friday review"
    assert "Action required" in preview["html"]
    assert "The review moves to Friday" in preview["html"]
    # A browser cannot resolve cid:, so the preview inlines the logo.
    assert "data:image/png;base64," in preview["html"]
    assert "cid:fundlok-logo" not in preview["html"]

    assert gmail.sent == []
    count = await db_session.scalar(select(func.count()).select_from(InternalEmail))
    assert count == 0


async def test_recent_messages_are_listed(client, make_system_admin, gmail):
    sysadmin = await make_system_admin()
    await _connect(client, sysadmin)
    for subject in ("First", "Second"):
        ok = await client.post(
            "/admin/email/send", json=_message(subject=subject), headers=sysadmin["headers"]
        )
        assert ok.status_code == 200, ok.text

    resp = await client.get("/admin/email/messages", headers=sysadmin["headers"])
    assert resp.status_code == 200
    items = resp.json()
    assert [item["subject"] for item in items] == ["Second", "First"]
    assert items[0]["status"] == "SENT"
    assert items[0]["recipients"] == ["colleague@fundlok.com"]
    assert items[0]["sent_by"] == sysadmin["id"]
