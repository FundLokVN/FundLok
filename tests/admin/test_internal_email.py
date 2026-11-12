"""Admin > Email: every admin connects their own Google account with Google
OAuth 2.0 (scope gmail.send), then composes and sends templated email from it
through the Gmail API. A system admin sets the org-wide allowed recipient
domains and sees every admin's sends. Spec: docs/specs/admin/internal-email.md.

Google is never contacted: `FakeGoogle` answers the OAuth token, revocation
and Gmail send endpoints through httpx.MockTransport (google._client), and
conftest makes any unpatched client fail.
"""
import asyncio
import base64
import email
import email.policy
import hashlib
import json
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.admin.internal_email import crypto, google, service, transport
from app.admin.internal_email.models import EmailSettings, InternalEmail
from app.admin.internal_email.schemas import SendEmailRequest
from app.core.config import settings
from app.core.database import SessionLocal
from app.lending.models import AuditLog
from app.main import app
from app.users.models import User

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"

SETTINGS = "/admin/email/settings"
AUTHORIZE = "/admin/email/oauth/authorize"
CALLBACK = "/admin/email/oauth/callback"
DISCONNECT = "/admin/email/disconnect"
POLICY = "/admin/email/policy"
REMINDER_SENDER = "/admin/email/reminder-sender"


def _gmail_error(status: int, reason: str, status_name: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "code": status,
                "message": "refused",
                "errors": [{"reason": reason, "message": "refused"}],
                "status": status_name,
            }
        },
    )


class FakeGoogle:
    """Google's token, revocation and Gmail send endpoints.

    issue_code() stands in for the consent screen: it returns the code
    Google would put on the redirect. The token endpoint checks the PKCE
    verifier, client secret and redirect URI like Google does. Each refresh
    token belongs to one Google account; a send is recorded as (account,
    parsed message)."""

    def __init__(self):
        self._codes: dict[str, dict] = {}
        self.accounts: dict[str, str] = {}  # refresh token -> Google account
        self.exchanges: list[dict] = []
        self.refreshes: list[str] = []
        self.revoked: list[str] = []
        self.sent: list[tuple[str, email.message.EmailMessage]] = []
        self.auth_headers: list[str] = []
        # Behaviour switches
        self.invalid_grant: set[str] = set()  # refresh tokens Google refuses
        self.token_endpoint_down = False
        self.exchange_fails = False
        self.revoke_down = False
        self.gmail_down = False
        self.gmail_unauthorized = False
        self.gmail_api_disabled = False
        self.refuse: set[str] = set()
        self.drop_after: int | None = None
        self._serial = 0

    # The consent screen ------------------------------------------------ #

    def issue_code(
        self,
        *,
        challenge: str,
        account: str,
        name: str | None = "Google Name",
        scopes: list[str] | None = None,
        email_verified: bool = True,
        refresh_token: str | None = "",
    ) -> str:
        self._serial += 1
        code = f"code-{self._serial}"
        if refresh_token == "":
            refresh_token = f"1//refresh-{self._serial}-{account}"
        self._codes[code] = {
            "challenge": challenge,
            "account": account,
            "name": name,
            "scopes": scopes if scopes is not None else list(google.SCOPES),
            "email_verified": email_verified,
            "refresh_token": refresh_token,
        }
        return code

    # HTTP --------------------------------------------------------------- #

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == google.TOKEN_URL:
            return self._token(dict(httpx.QueryParams(request.content.decode())))
        if url == google.REVOKE_URL:
            if self.revoke_down:
                raise httpx.ConnectError("down", request=request)
            self.revoked.append(dict(httpx.QueryParams(request.content.decode()))["token"])
            return httpx.Response(200)
        if url == google.GMAIL_SEND_URL:
            return self._send(request)
        raise AssertionError(f"unexpected Google call: {url}")

    def _token(self, form: dict) -> httpx.Response:
        if self.token_endpoint_down:
            raise httpx.ConnectError("down")
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            self.exchanges.append(form)
            grant = self._codes.pop(form["code"], None)
            if self.exchange_fails or grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier_hash = (
                base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if verifier_hash != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if form["redirect_uri"] != settings.gmail_oauth_redirect_uri:
                return httpx.Response(400, json={"error": "redirect_uri_mismatch"})
            id_token = jwt.encode(
                {
                    "iss": "https://accounts.google.com",
                    "aud": CLIENT_ID,
                    "sub": f"sub-{grant['account']}",
                    "email": grant["account"],
                    "email_verified": grant["email_verified"],
                    "name": grant["name"],
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 3600,
                },
                "fake-google-signing-key-not-checked-0123456789",
                algorithm="HS256",
            )
            body = {
                "access_token": f"ya29.first-{grant['account']}",
                "expires_in": 3599,
                "scope": " ".join(grant["scopes"]),
                "token_type": "Bearer",
                "id_token": id_token,
            }
            if grant["refresh_token"]:
                body["refresh_token"] = grant["refresh_token"]
                self.accounts[grant["refresh_token"]] = grant["account"]
            return httpx.Response(200, json=body)
        if form["grant_type"] == "refresh_token":
            token = form["refresh_token"]
            self.refreshes.append(token)
            if token in self.invalid_grant or token in self.revoked or token not in self.accounts:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
                )
            return httpx.Response(200, json={"access_token": f"ya29.{token}", "expires_in": 3599})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _send(self, request: httpx.Request) -> httpx.Response:
        if self.gmail_down:
            raise httpx.ConnectError("down", request=request)
        auth = request.headers["Authorization"]
        self.auth_headers.append(auth)
        refresh_token = auth.removeprefix("Bearer ya29.")
        if self.gmail_unauthorized or refresh_token not in self.accounts:
            return _gmail_error(401, "authError", "UNAUTHENTICATED")
        if self.gmail_api_disabled:
            # What Google answers when the Gmail API is off for the project.
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Gmail API has not been used in project 628295811257 before or it is disabled.",
                        "errors": [{"reason": "accessNotConfigured", "domain": "usageLimits"}],
                        "status": "PERMISSION_DENIED",
                        "details": [{"reason": "SERVICE_DISABLED", "domain": "googleapis.com"}],
                    }
                },
            )
        raw = base64.urlsafe_b64decode(json.loads(request.content)["raw"])
        message = email.message_from_bytes(raw, policy=email.policy.default)
        if self.drop_after is not None and len(self.sent) >= self.drop_after:
            return _gmail_error(503, "backendError", "UNAVAILABLE")
        if message["To"] in self.refuse:
            return _gmail_error(400, "invalidArgument", "INVALID_ARGUMENT")
        self.sent.append((self.accounts[refresh_token], message))
        return httpx.Response(200, json={"id": f"msg-{len(self.sent)}", "labelIds": ["SENT"]})

    @property
    def messages(self) -> list[email.message.EmailMessage]:
        return [message for _, message in self.sent]


@pytest.fixture
def fake_google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", None)
    monkeypatch.setattr(settings, "GMAIL_CONNECT_ALLOWED_DOMAINS", "")
    monkeypatch.setattr(google, "_client", fake.client)
    return fake


def _profile(**overrides) -> dict:
    body = {
        "from_name": "FundLok Team",
        "enabled": True,
        "signature_name": None,
        "signature_title": None,
        "signature_phone": None,
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


async def _authorize(client, admin) -> dict:
    """Start the flow as the admin; the consent URL's query, flattened."""
    resp = await client.get(AUTHORIZE, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    query = parse_qs(urlsplit(resp.json()["authorization_url"]).query)
    return {key: values[0] for key, values in query.items()}


async def _callback(client, **params) -> dict:
    """Google's redirect back; the console URL's query it redirects to."""
    resp = await client.get(CALLBACK, params=params)
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith(settings.FRONTEND_URL.rstrip("/") + "/admin/email?")
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


async def _oauth(client, fake_google, admin, account: str | None = None, **grant) -> dict:
    """The whole connect, as the browser and Google would do it."""
    consent = await _authorize(client, admin)
    code = fake_google.issue_code(
        challenge=consent["code_challenge"], account=account or admin["email"], **grant
    )
    return await _callback(client, code=code, state=consent["state"])


async def _connect(client, fake_google, admin, account: str | None = None, **profile) -> dict:
    """Connect a Google account and save the profile (sending on by default)."""
    outcome = await _oauth(client, fake_google, admin, account)
    assert outcome == {"connected": "true"}, outcome
    resp = await client.put(SETTINGS, json=_profile(**profile), headers=admin["headers"])
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

ENDPOINTS = [
    ("get", SETTINGS, None),
    ("put", SETTINGS, _profile()),
    ("get", AUTHORIZE, None),
    ("post", DISCONNECT, None),
    ("post", f"{SETTINGS}/test", None),
    ("post", "/admin/email/preview", {k: v for k, v in _message().items() if k != "to"}),
    ("post", "/admin/email/send", _message()),
    ("get", "/admin/email/messages", None),
    ("get", POLICY, None),
    ("put", POLICY, {"allowed_domains": []}),
    ("get", REMINDER_SENDER, None),
    ("put", REMINDER_SENDER, {"user_id": None}),
]


async def test_only_admins_can_use_internal_email(client, make_user, fake_google):
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
    client, make_admin, fake_google
):
    admin = await make_admin()
    connected = await _connect(client, fake_google, admin)
    assert connected["configured"] is True
    assert connected["gmail_address"] == admin["email"]

    test = await client.post(f"{SETTINGS}/test", headers=admin["headers"])
    assert test.status_code == 200, test.text
    sent = await client.post("/admin/email/send", json=_message(), headers=admin["headers"])
    assert sent.status_code == 200, sent.text
    assert sent.json()["from_address"] == admin["email"]

    assert (await client.get(POLICY, headers=admin["headers"])).status_code == 200
    resp = await client.put(POLICY, json={"allowed_domains": []}, headers=admin["headers"])
    assert resp.status_code == 403


async def test_each_admin_has_their_own_account(
    client, make_admin, make_system_admin, db_session, fake_google
):
    alice = await make_admin(email="alice@fundlok.com")
    bob = await make_admin(email="bob@fundlok.com")
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, alice, "alice@fundlok.com", from_name="Alice")

    # Bob sees nothing of Alice's, and cannot send with it.
    bobs = (await client.get(SETTINGS, headers=bob["headers"])).json()
    assert bobs["configured"] is False and bobs["gmail_address"] is None
    resp = await client.post("/admin/email/send", json=_message(), headers=bob["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_CONFIGURED"

    # His own connection is a separate row; Alice's is untouched.
    await _connect(client, fake_google, bob, "bob@fundlok.com", from_name="Bob")
    alices_row = await _row(db_session, alice)
    assert alices_row.gmail_address == "alice@fundlok.com"
    assert fake_google.accounts[crypto.decrypt_secret(alices_row.refresh_token_encrypted)] == (
        "alice@fundlok.com"
    )

    for who in (alice, bob):
        ok = await client.post("/admin/email/send", json=_message(), headers=who["headers"])
        assert ok.status_code == 200, ok.text
    # Each send went out as its own admin's Google account, with their From.
    assert [account for account, _ in fake_google.sent] == ["alice@fundlok.com", "bob@fundlok.com"]
    first, second = fake_google.messages
    assert "Alice" in first["From"] and "alice@fundlok.com" in first["From"]
    assert "Bob" in second["From"] and "bob@fundlok.com" in second["From"]

    # Disconnecting is one's own business too.
    await client.post(DISCONNECT, headers=bob["headers"])
    assert (await _row(db_session, bob)).refresh_token_encrypted is None
    assert (await _row(db_session, alice)).refresh_token_encrypted is not None

    # An admin sees their own sends; a system admin sees everyone's.
    alices = (await client.get("/admin/email/messages", headers=alice["headers"])).json()
    assert [m["from_address"] for m in alices] == ["alice@fundlok.com"]
    every = (await client.get("/admin/email/messages", headers=sysadmin["headers"])).json()
    assert sorted(m["from_address"] for m in every) == ["alice@fundlok.com", "bob@fundlok.com"]


# --------------------------------------------------------------------------- #
# Connecting with Google
# --------------------------------------------------------------------------- #


async def test_authorize_sends_the_admin_to_google_consent(client, make_admin, fake_google):
    admin = await make_admin()
    resp = await client.get(AUTHORIZE, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    url = urlsplit(resp.json()["authorization_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == google.AUTHORIZE_URL
    query = {key: values[0] for key, values in parse_qs(url.query).items()}
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == (
        settings.FRONTEND_URL.rstrip("/") + "/api/admin/email/oauth/callback"
    )
    assert query["response_type"] == "code"
    assert query["scope"].split() == [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/gmail.send",
    ]
    # offline + consent: a refresh token on every connect, reconnects too.
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["include_granted_scopes"] == "true"
    assert query["code_challenge_method"] == "S256"
    assert google.read_state(query["state"]) == (UUID(admin["id"]), query["code_challenge"])

    # The PKCE verifier is in an httpOnly, Lax cookie scoped to the callback's
    # directory (no Path=/), never in the URL.
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{google.BINDING_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=" not in cookie
    verifier = resp.cookies[google.BINDING_COOKIE]
    assert verifier not in resp.text
    assert google.code_challenge(verifier) == query["code_challenge"]


async def test_the_redirect_uri_can_be_set_explicitly(client, make_admin, fake_google, monkeypatch):
    monkeypatch.setattr(
        settings, "GOOGLE_OAUTH_REDIRECT_URI", "https://api.fundlok.com/admin/email/oauth/callback"
    )
    admin = await make_admin()
    consent = await _authorize(client, admin)
    assert consent["redirect_uri"] == "https://api.fundlok.com/admin/email/oauth/callback"
    # The code exchange must send the same one.
    code = fake_google.issue_code(challenge=consent["code_challenge"], account=admin["email"])
    assert await _callback(client, code=code, state=consent["state"]) == {"connected": "true"}
    assert fake_google.exchanges[-1]["redirect_uri"] == consent["redirect_uri"]


async def test_authorize_is_refused_until_oauth_is_configured(client, make_admin, monkeypatch):
    admin = await make_admin()
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", None)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", None)
    resp = await client.get(AUTHORIZE, headers=admin["headers"])
    assert resp.status_code == 503
    assert resp.headers["X-Error-Code"] == "GMAIL_OAUTH_NOT_CONFIGURED"
    assert "GOOGLE_OAUTH" not in resp.text

    # A client id alone (the sign-in one) is not enough: the code exchange
    # needs the secret.
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", CLIENT_ID)
    resp = await client.get(AUTHORIZE, headers=admin["headers"])
    assert resp.status_code == 503


async def test_the_sign_in_client_is_reused_when_no_gmail_client_is_set(
    client, make_admin, fake_google, monkeypatch
):
    # Only GOOGLE_CLIENT_ID (Google sign-in) and the secret are set.
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", None)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", CLIENT_ID)
    admin = await make_admin()
    consent = await _authorize(client, admin)
    assert consent["client_id"] == CLIENT_ID
    code = fake_google.issue_code(challenge=consent["code_challenge"], account=admin["email"])
    assert await _callback(client, code=code, state=consent["state"]) == {"connected": "true"}
    # The exchange used the same client, and the ID token's audience matched it.
    assert fake_google.exchanges[-1]["client_id"] == CLIENT_ID

    # A dedicated Gmail client, when set, wins over the sign-in one.
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "sign-in-only.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", CLIENT_ID)
    assert (await _authorize(client, admin))["client_id"] == CLIENT_ID


async def test_connecting_stores_the_refresh_token_encrypted_and_never_returns_it(
    client, make_admin, db_session, fake_google
):
    admin = await make_admin(email="ops.lead@fundlok.com")
    assert await _oauth(client, fake_google, admin, "ops.lead@fundlok.com") == {"connected": "true"}

    row = await _row(db_session, admin)
    token = crypto.decrypt_secret(row.refresh_token_encrypted)
    assert token.startswith("1//refresh-")
    assert row.refresh_token_encrypted.startswith("v1:") and token not in row.refresh_token_encrypted
    # The address is Google's verified one; nobody typed it.
    assert row.gmail_address == "ops.lead@fundlok.com"
    assert google.GMAIL_SEND_SCOPE in row.oauth_scopes.split()
    assert row.token_updated_at is not None
    # A first connect starts paused, named after the admin.
    assert row.enabled is False
    assert row.from_name == "Test User"

    body = (await client.get(SETTINGS, headers=admin["headers"])).json()
    assert body["configured"] is True and body["needs_reconnect"] is False
    assert body["gmail_address"] == "ops.lead@fundlok.com"
    assert body["connected_at"] is not None
    assert token not in json.dumps(body) and "v1:" not in json.dumps(body)
    # Only the refresh token is kept; the one-hour access token is dropped.
    assert "ya29" not in json.dumps([row.refresh_token_encrypted, row.oauth_scopes])


async def test_the_callback_needs_the_browser_that_started_it(
    client, make_admin, db_session, fake_google, monkeypatch
):
    admin = await make_admin()

    # Another browser (no binding cookie) cannot finish this admin's flow.
    consent = await _authorize(client, admin)
    code = fake_google.issue_code(challenge=consent["code_challenge"], account="attacker@gmail.com")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as other:
        assert await _callback(other, code=code, state=consent["state"]) == {
            "error": "invalid_state"
        }

    # A state from an earlier start does not match this browser's newer cookie.
    stale = await _authorize(client, admin)
    await _authorize(client, admin)
    code = fake_google.issue_code(challenge=stale["code_challenge"], account=admin["email"])
    assert await _callback(client, code=code, state=stale["state"]) == {"error": "invalid_state"}

    # Tampered, foreign and expired states.
    fresh = await _authorize(client, admin)
    code = fake_google.issue_code(challenge=fresh["code_challenge"], account=admin["email"])
    forged = jwt.encode(
        jwt.decode(fresh["state"], options={"verify_signature": False}),
        "not-the-server-secret-0123456789abcdef",
        algorithm="HS256",
    )
    monkeypatch.setattr(google, "STATE_TTL", timedelta(seconds=-5))
    expired = google.create_state(UUID(admin["id"]), fresh["code_challenge"])
    for state in (forged, expired, "not-a-jwt", ""):
        assert await _callback(client, code=code, state=state) == {"error": "invalid_state"}
    assert await _callback(client, code=code) == {"error": "invalid_state"}

    # None of it reached Google's token endpoint or stored anything.
    assert fake_google.exchanges == []
    assert await _row(db_session, admin) is None


async def test_a_state_is_not_a_session(client, make_admin, fake_google):
    admin = await make_admin()
    consent = await _authorize(client, admin)
    client.cookies.clear()
    resp = await client.get(SETTINGS, headers={"Authorization": f"Bearer {consent['state']}"})
    assert resp.status_code == 401


async def test_cancelling_on_google_returns_to_the_console(client, make_admin, fake_google):
    admin = await make_admin()
    consent = await _authorize(client, admin)
    outcome = await _callback(client, error="access_denied", state=consent["state"])
    assert outcome == {"error": "cancelled"}
    # Google's own error text never reaches the console URL.
    outcome = await _callback(client, error="<script>x</script>", state=consent["state"])
    assert outcome == {"error": "failed"}


async def test_a_connect_without_the_send_permission_is_refused_and_revoked(
    client, make_admin, db_session, fake_google
):
    # Google's consent screen lets the admin untick "Send email on your behalf".
    admin = await make_admin()
    outcome = await _oauth(
        client, fake_google, admin, scopes=["openid", "email", "profile"], refresh_token="1//no-send"
    )
    assert outcome == {"error": "scope_missing"}
    assert fake_google.revoked == ["1//no-send"]
    assert await _row(db_session, admin) is None


async def test_an_unverified_google_address_is_refused(client, make_admin, db_session, fake_google):
    admin = await make_admin()
    outcome = await _oauth(client, fake_google, admin, email_verified=False, refresh_token="1//x")
    assert outcome == {"error": "email_unverified"}
    assert fake_google.revoked == ["1//x"]
    assert await _row(db_session, admin) is None


async def test_the_connect_domain_allowlist_applies(
    client, make_admin, db_session, fake_google, monkeypatch
):
    monkeypatch.setattr(settings, "GMAIL_CONNECT_ALLOWED_DOMAINS", "fundlok.com, @FundLok.vn")
    outsider = await make_admin(email="someone@gmail.com")
    outcome = await _oauth(client, fake_google, outsider, "someone@gmail.com", refresh_token="1//g")
    assert outcome == {"error": "domain_not_allowed"}
    assert fake_google.revoked == ["1//g"]
    assert await _row(db_session, outsider) is None

    for account in ("ops@fundlok.com", "ops@FundLok.vn"):
        admin = await make_admin(email=account)
        assert await _oauth(client, fake_google, admin, account) == {"connected": "true"}


async def test_connecting_a_different_email_from_the_admin_account_is_refused(
    client, make_admin, db_session, fake_google
):
    admin = await make_admin(email="phat.huynh@fundlok.com")
    outcome = await _oauth(
        client,
        fake_google,
        admin,
        "other.account@fundlok.com",
        refresh_token="1//mismatched",
    )
    assert outcome == {"error": "email_mismatch"}
    assert fake_google.revoked == ["1//mismatched"]
    assert await _row(db_session, admin) is None


async def test_a_grant_without_a_refresh_token_is_refused(client, make_admin, db_session, fake_google):
    admin = await make_admin()
    outcome = await _oauth(client, fake_google, admin, refresh_token=None)
    assert outcome == {"error": "no_refresh_token"}
    assert await _row(db_session, admin) is None


async def test_a_failed_code_exchange_is_reported(client, make_admin, db_session, fake_google):
    admin = await make_admin()
    fake_google.exchange_fails = True
    assert await _oauth(client, fake_google, admin) == {"error": "failed"}
    fake_google.exchange_fails = False
    fake_google.token_endpoint_down = True
    assert await _oauth(client, fake_google, admin) == {"error": "failed"}
    assert await _row(db_session, admin) is None


async def test_connecting_fails_closed_without_an_encryption_key(
    client, make_admin, db_session, fake_google, monkeypatch
):
    admin = await make_admin()
    monkeypatch.setattr(crypto.settings, "EMAIL_SETTINGS_ENCRYPTION_KEY", None)
    outcome = await _oauth(client, fake_google, admin, refresh_token="1//plain")
    assert outcome == {"error": "encryption_unavailable"}
    # Never stored in plaintext; never left live at Google either.
    assert await _row(db_session, admin) is None
    assert fake_google.revoked == ["1//plain"]


async def test_the_callback_refuses_someone_no_longer_an_admin(
    client, make_admin, db_session, fake_google
):
    admin = await make_admin()
    consent = await _authorize(client, admin)
    user = await db_session.get(User, UUID(admin["id"]))
    user.role = "INVESTOR"
    await db_session.commit()

    code = fake_google.issue_code(challenge=consent["code_challenge"], account=admin["email"])
    assert await _callback(client, code=code, state=consent["state"]) == {"error": "not_admin"}
    assert fake_google.exchanges == []


async def test_reconnecting_keeps_the_profile_and_resets_verification(
    client, make_admin, db_session, fake_google
):
    admin = await make_admin(email="ops@fundlok.com")
    await _connect(client, fake_google, admin, "ops@fundlok.com", signature_name="Lan Nguyen")
    assert (await client.post(f"{SETTINGS}/test", headers=admin["headers"])).status_code == 200
    first = crypto.decrypt_secret((await _row(db_session, admin)).refresh_token_encrypted)

    # The same account again: a new token, and the old one is NOT revoked
    # (revoking it would revoke the whole grant, the new token included).
    assert await _oauth(client, fake_google, admin, "Ops@FundLok.com") == {"connected": "true"}
    row = await _row(db_session, admin)
    assert crypto.decrypt_secret(row.refresh_token_encrypted) != first
    assert fake_google.revoked == []
    assert row.last_verified_at is None
    assert (row.enabled, row.from_name, row.signature_name) == (True, "FundLok Team", "Lan Nguyen")

    # Trying to replace it with a different Google account is refused and
    # revoked; the existing connection stays intact.
    second = crypto.decrypt_secret(row.refresh_token_encrypted)
    assert await _oauth(
        client, fake_google, admin, "other@fundlok.com", refresh_token="1//other"
    ) == {"error": "email_mismatch"}
    row = await _row(db_session, admin)
    assert row.gmail_address == "Ops@FundLok.com"
    assert crypto.decrypt_secret(row.refresh_token_encrypted) == second
    assert fake_google.revoked == ["1//other"]


async def test_connecting_is_audited_without_the_token(client, make_admin, db_session, fake_google):
    admin = await make_admin()
    await _oauth(client, fake_google, admin, refresh_token="1//secret-refresh")
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "EMAIL_GMAIL_CONNECTED"))
    ).scalar_one()
    dumped = json.dumps([audit.before_state, audit.after_state])
    assert "1//secret-refresh" not in dumped and "v1:" not in dumped
    assert audit.after_state["connected"] is True
    assert audit.after_state["gmail_address"] == admin["email"]
    assert str(audit.actor_id) == admin["id"] and str(audit.entity_id) == admin["id"]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


async def test_settings_start_unconfigured(client, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.get(SETTINGS, headers=sysadmin["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["needs_reconnect"] is False
    assert body["gmail_address"] is None
    assert body["enabled"] is False
    assert "has_app_password" not in body


async def test_the_profile_can_be_saved_before_connecting(client, make_system_admin, db_session):
    sysadmin = await make_system_admin()
    resp = await client.put(
        SETTINGS,
        json=_profile(enabled=False, signature_name="Lan Nguyen", signature_phone="+84 90 000 0000"),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is False and body["gmail_address"] is None
    assert (body["from_name"], body["signature_name"]) == ("FundLok Team", "Lan Nguyen")
    assert (await _row(db_session, sysadmin)).gmail_address is None


async def test_enabling_requires_a_connected_account(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    resp = await client.put(SETTINGS, json=_profile(enabled=True), headers=sysadmin["headers"])
    assert resp.status_code == 400
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_CONFIGURED"

    body = await _connect(client, fake_google, sysadmin, enabled=True)
    assert body["enabled"] is True and body["configured"] is True


async def test_the_settings_no_longer_take_an_address_or_password(client, make_system_admin):
    sysadmin = await make_system_admin()
    for extra in ({"gmail_address": "team@fundlok.com"}, {"app_password": "abcdefghijklmnop"}):
        resp = await client.put(
            SETTINGS, json=_profile(enabled=False, **extra), headers=sysadmin["headers"]
        )
        assert resp.status_code == 422, extra


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


async def test_settings_changes_are_audited_without_the_secret(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    await client.put(SETTINGS, json=_profile(from_name="Ops"), headers=sysadmin["headers"])
    token = crypto.decrypt_secret((await _row(db_session, sysadmin)).refresh_token_encrypted)

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
        assert token not in dumped and "v1:" not in dumped
        assert str(row.actor_id) == sysadmin["id"]
        # The settings belong to the admin, so the audit row points at them.
        assert str(row.entity_id) == sysadmin["id"]
    assert rows[1].after_state["from_name"] == "Ops"
    assert rows[1].after_state["connected"] is True


async def test_disconnecting_revokes_the_token_and_keeps_the_profile(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin, signature_name="Lan Nguyen")
    token = crypto.decrypt_secret((await _row(db_session, sysadmin)).refresh_token_encrypted)

    resp = await client.post(DISCONNECT, headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is False and body["needs_reconnect"] is False
    assert body["enabled"] is False and body["gmail_address"] is None
    assert (body["from_name"], body["signature_name"]) == ("FundLok Team", "Lan Nguyen")
    assert fake_google.revoked == [token]

    row = await _row(db_session, sysadmin)
    assert (row.refresh_token_encrypted, row.oauth_scopes, row.last_verified_at) == (None, None, None)
    cleared = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "EMAIL_GMAIL_DISCONNECTED")
        )
    ).scalar_one()
    assert token not in json.dumps([cleared.before_state, cleared.after_state])

    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_CONFIGURED"
    # Disconnecting again, or with nothing connected, is harmless.
    assert (await client.post(DISCONNECT, headers=sysadmin["headers"])).status_code == 200


async def test_disconnecting_works_when_google_cannot_be_reached(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    fake_google.revoke_down = True
    resp = await client.post(DISCONNECT, headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    assert (await _row(db_session, sysadmin)).refresh_token_encrypted is None


# --------------------------------------------------------------------------- #
# Test email and Google refusing the token
# --------------------------------------------------------------------------- #


async def test_the_test_email_goes_to_the_admin_and_marks_verified(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin(email="team@fundlok.com")
    await _connect(client, fake_google, sysadmin, "team@fundlok.com", enabled=False)  # works while paused

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent_to"] == sysadmin["email"]

    [(account, message)] = fake_google.sent
    assert account == "team@fundlok.com"
    assert message["To"] == sysadmin["email"]
    assert "team@fundlok.com" in _html_part(message)
    # A fresh access token from the stored refresh token, sent as a bearer.
    token = crypto.decrypt_secret((await _row(db_session, sysadmin)).refresh_token_encrypted)
    assert fake_google.refreshes == [token]
    assert fake_google.auth_headers == [f"Bearer ya29.{token}"]

    settings_out = (await client.get(SETTINGS, headers=sysadmin["headers"])).json()
    assert settings_out["last_verified_at"] is not None

    log = (await db_session.execute(select(InternalEmail))).scalar_one()
    assert (log.template, log.status, log.recipients) == ("test", "SENT", [sysadmin["email"]])


async def test_a_revoked_grant_asks_to_reconnect(client, make_system_admin, db_session, fake_google):
    # The admin removed FundLok under "Third-party access" in their Google
    # account: Google answers invalid_grant to the refresh.
    sysadmin = await make_system_admin(email="team@fundlok.com")
    await _connect(client, fake_google, sysadmin, "team@fundlok.com", signature_name="Lan")
    token = crypto.decrypt_secret((await _row(db_session, sysadmin)).refresh_token_encrypted)
    fake_google.invalid_grant.add(token)

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "GMAIL_RECONNECT_REQUIRED"
    assert "expired or revoked" not in resp.text and token not in resp.text

    log = (await db_session.execute(select(InternalEmail))).scalar_one()
    assert (log.status, log.error_code) == ("FAILED", "GMAIL_RECONNECT_REQUIRED")
    # The dead token is dropped; the address stays to say what to reconnect.
    body = (await client.get(SETTINGS, headers=sysadmin["headers"])).json()
    assert body["configured"] is False and body["needs_reconnect"] is True
    assert body["gmail_address"] == "team@fundlok.com"
    assert body["signature_name"] == "Lan"
    assert (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "EMAIL_GMAIL_ACCESS_REVOKED")
        )
    ).scalar_one()

    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_CONFIGURED"

    # The switch stayed on, so the profile still saves while disconnected.
    saved = await client.put(
        SETTINGS, json=_profile(enabled=True, signature_name="Lan N."), headers=sysadmin["headers"]
    )
    assert saved.status_code == 200, saved.text

    # Reconnecting brings it back, sending switch and all.
    assert await _oauth(client, fake_google, sysadmin, "team@fundlok.com") == {"connected": "true"}
    ok = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert ok.status_code == 200, ok.text


async def test_a_gmail_401_asks_to_reconnect(client, make_system_admin, db_session, fake_google):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    fake_google.gmail_unauthorized = True

    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "GMAIL_RECONNECT_REQUIRED"
    assert (await _row(db_session, sysadmin)).refresh_token_encrypted is None


async def test_a_reconnect_during_a_failed_send_is_not_undone(
    client, make_system_admin, db_session, fake_google, monkeypatch
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    old = crypto.decrypt_secret((await _row(db_session, sysadmin)).refresh_token_encrypted)
    fake_google.invalid_grant.add(old)

    real_deliver = transport.deliver

    async def reconnected_mid_send(refresh_token, messages):
        row = (await db_session.execute(select(EmailSettings))).scalar_one()
        row.refresh_token_encrypted = crypto.encrypt_secret("1//newer")
        await db_session.commit()
        return await real_deliver(refresh_token, messages)

    monkeypatch.setattr(transport, "deliver", reconnected_mid_send)
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.headers["X-Error-Code"] == "GMAIL_RECONNECT_REQUIRED"
    row = await _row(db_session, sysadmin)
    assert crypto.decrypt_secret(row.refresh_token_encrypted) == "1//newer"


async def test_a_disabled_gmail_api_is_reported_as_a_setup_problem(
    client, make_system_admin, db_session, fake_google
):
    # The OAuth client's Google Cloud project does not have the Gmail API on.
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin, enabled=False)
    fake_google.gmail_api_disabled = True

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 502
    assert resp.headers["X-Error-Code"] == "GMAIL_API_DISABLED"
    assert "628295811257" not in resp.text  # Google's own text stays in the log
    log = (await db_session.execute(select(InternalEmail))).scalar_one()
    assert (log.status, log.error_code) == ("FAILED", "GMAIL_API_DISABLED")
    # A setup problem, not a revoked grant: the connection stays.
    assert (await _row(db_session, sysadmin)).refresh_token_encrypted is not None


async def test_an_unreadable_token_asks_to_reconnect(
    client, make_system_admin, db_session, fake_google
):
    # Stored under an encryption key that has since been rotated.
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    row = await _row(db_session, sysadmin)
    row.refresh_token_encrypted = "v1:" + "A" * 100
    await db_session.commit()

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "GMAIL_RECONNECT_REQUIRED"
    assert fake_google.refreshes == []


async def test_an_unreachable_google_is_reported(client, make_system_admin, db_session, fake_google):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)

    fake_google.token_endpoint_down = True
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 502
    assert resp.headers["X-Error-Code"] == "GMAIL_UNAVAILABLE"

    fake_google.token_endpoint_down = False
    fake_google.gmail_down = True
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 502
    assert resp.headers["X-Error-Code"] == "GMAIL_UNAVAILABLE"
    # An outage is not a revocation: the connection stays.
    assert (await _row(db_session, sysadmin)).refresh_token_encrypted is not None


async def test_the_test_email_only_verifies_the_token_it_tested(
    client, make_system_admin, db_session, fake_google, monkeypatch
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)

    # The admin reconnects while the test email is with Gmail.
    real_send_logged = service._send_logged

    async def reconnected_mid_send(db, **kwargs):
        log = await real_send_logged(db, **kwargs)
        row = (await db.execute(select(EmailSettings))).scalar_one()
        row.refresh_token_encrypted = crypto.encrypt_secret("1//newer")
        row.last_verified_at = None
        await db.commit()
        return log

    monkeypatch.setattr(service, "_send_logged", reconnected_mid_send)

    resp = await client.post(f"{SETTINGS}/test", headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    body = (await client.get(SETTINGS, headers=sysadmin["headers"])).json()
    assert body["last_verified_at"] is None


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


async def test_sending_requires_configured_and_enabled(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "EMAIL_NOT_CONFIGURED"

    await _connect(client, fake_google, sysadmin, enabled=False)
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "EMAIL_DISABLED"
    assert fake_google.sent == []


async def test_a_send_reaches_gmail_with_the_rendered_template(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin(email="team@fundlok.com")
    await _connect(client, fake_google, sysadmin, "team@fundlok.com")

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

    # One copy each, addressed to that person alone, one token refresh.
    assert [m["To"] for m in fake_google.messages] == ["colleague@fundlok.com", "second@fundlok.com"]
    assert len(fake_google.refreshes) == 1
    message = fake_google.messages[0]
    assert message["Subject"] == "Friday review"
    assert "FundLok Team" in message["From"] and "team@fundlok.com" in message["From"]
    assert message["Cc"] is None and message["Bcc"] is None

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


async def test_the_signature_is_appended_when_ticked(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    await _connect(
        client,
        fake_google,
        sysadmin,
        signature_name="Lan Nguyen",
        signature_title="Operations",
        signature_phone="+84 90 000 0000",
    )
    resp = await client.post(
        "/admin/email/send", json=_message(include_signature=True), headers=sysadmin["headers"]
    )
    assert resp.status_code == 200, resp.text
    text = _text_part(fake_google.messages[0])
    assert "Lan Nguyen\nOperations\nMobile: +84 90 000 0000" in text
    assert "Lan Nguyen" in _html_part(fake_google.messages[0])


async def test_content_is_escaped_not_rendered_as_html(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)

    resp = await client.post(
        "/admin/email/send",
        json=_message(
            heading='<a href="https://evil.example">Click</a>',
            body="<script>alert(1)</script>\n<img src=x onerror=alert(1)>",
        ),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    html = _html_part(fake_google.messages[0])
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


async def test_header_injection_is_rejected(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)

    resp = await client.post(
        "/admin/email/send",
        json=_message(subject="Hello\r\nBcc: everyone@example.com"),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 422

    resp = await client.put(
        SETTINGS, json=_profile(from_name="Team\nBcc: x@example.com"), headers=sysadmin["headers"]
    )
    assert resp.status_code == 422

    # Line breaks the email package recognises beyond CR/LF: a 422 at the
    # schema, never a 500 from the header builder.
    for separator in (" ", " ", "\u0085"):
        resp = await client.post(
            "/admin/email/send",
            json=_message(subject=f"Q3 update{separator}Bcc: x@example.com"),
            headers=sysadmin["headers"],
        )
        assert resp.status_code == 422, separator
        resp = await client.put(
            SETTINGS, json=_profile(from_name=f"Team{separator}Ops"), headers=sysadmin["headers"]
        )
        assert resp.status_code == 422, separator

    # An RFC 2047 encoded word passes email validation but decodes to CR/LF.
    resp = await client.post(
        "/admin/email/send",
        json=_message(to=["=?utf-8?q?x=0d=0aBcc=3a_a=40evil.com?=@fundlok.com"]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 422
    assert fake_google.sent == []


async def test_recipients_outside_allowed_domains_are_refused(
    client, make_system_admin, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
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
    assert fake_google.sent == []

    ok = await client.post(
        "/admin/email/send", json=_message(to=["a@FundLok.com"]), headers=sysadmin["headers"]
    )
    assert ok.status_code == 200, ok.text


async def test_too_many_recipients_are_rejected(client, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.post(
        "/admin/email/send",
        json=_message(to=[f"person{i}@fundlok.com" for i in range(51)]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 422


async def test_the_hourly_limit_applies(client, make_system_admin, fake_google, monkeypatch):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    monkeypatch.setattr(service, "RECIPIENTS_PER_HOUR", 2)

    for _ in range(2):
        ok = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
        assert ok.status_code == 200, ok.text
    resp = await client.post("/admin/email/send", json=_message(), headers=sysadmin["headers"])
    assert resp.status_code == 429
    assert resp.headers["X-Error-Code"] == "EMAIL_RATE_LIMITED"
    assert len(fake_google.sent) == 2


async def test_the_hourly_limit_is_per_admin(client, make_admin, fake_google, monkeypatch):
    # One admin using up the hour must not block another.
    alice = await make_admin(email="alice@fundlok.com")
    bob = await make_admin(email="bob@fundlok.com")
    await _connect(client, fake_google, alice, "alice@fundlok.com")
    await _connect(client, fake_google, bob, "bob@fundlok.com")
    monkeypatch.setattr(service, "RECIPIENTS_PER_HOUR", 1)

    ok = await client.post("/admin/email/send", json=_message(), headers=alice["headers"])
    assert ok.status_code == 200, ok.text
    limited = await client.post("/admin/email/send", json=_message(), headers=alice["headers"])
    assert limited.status_code == 429
    bobs = await client.post("/admin/email/send", json=_message(), headers=bob["headers"])
    assert bobs.status_code == 200, bobs.text


async def test_the_hourly_limit_holds_under_concurrent_sends(
    client, make_system_admin, fake_google, monkeypatch
):
    # Straight at the service, each send in its own session (the test client
    # shares one session across requests, so it cannot race). Without the
    # per-admin lock, the sends all read the same count before any of them
    # inserts, and all pass.
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    monkeypatch.setattr(service, "RECIPIENTS_PER_HOUR", 3)
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
    assert len(fake_google.sent) == 3


async def test_the_hourly_limit_counts_recipients(client, make_system_admin, fake_google, monkeypatch):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    monkeypatch.setattr(service, "RECIPIENTS_PER_HOUR", 3)
    two = _message(to=["a@fundlok.com", "b@fundlok.com"])

    ok = await client.post("/admin/email/send", json=two, headers=sysadmin["headers"])
    assert ok.status_code == 200, ok.text
    # 2 used, 1 left: another 2 would go over, a single one fits.
    over = await client.post("/admin/email/send", json=two, headers=sysadmin["headers"])
    assert over.status_code == 429
    assert over.headers["X-Error-Code"] == "EMAIL_RATE_LIMITED"
    fits = await client.post(
        "/admin/email/send", json=_message(to=["c@fundlok.com"]), headers=sysadmin["headers"]
    )
    assert fits.status_code == 200, fits.text
    assert len(fake_google.sent) == 3


async def test_no_recipient_sees_the_others(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    people = ["sme.owner@example.com", "investor@example.com", "staff@fundlok.com"]

    resp = await client.post("/admin/email/send", json=_message(to=people), headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    assert len(fake_google.sent) == 3
    for message, person in zip(fake_google.messages, people):
        raw = message.as_string()
        assert message["To"] == person
        for other in people:
            if other != person:
                assert other not in raw


async def test_refused_recipients_are_recorded_and_the_rest_still_go(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    fake_google.refuse = {"gone@fundlok.com"}

    resp = await client.post(
        "/admin/email/send",
        json=_message(to=["a@fundlok.com", "gone@fundlok.com", "b@fundlok.com"]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "SENT"
    assert out["error_code"] == "SOME_RECIPIENTS_REFUSED"
    assert out["undelivered"] == ["gone@fundlok.com"]
    assert [m["To"] for m in fake_google.messages] == ["a@fundlok.com", "b@fundlok.com"]


async def test_every_recipient_refused_is_a_failure(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    fake_google.refuse = {"gone@fundlok.com"}

    resp = await client.post(
        "/admin/email/send", json=_message(to=["gone@fundlok.com"]), headers=sysadmin["headers"]
    )
    assert resp.status_code == 502
    assert resp.headers["X-Error-Code"] == "GMAIL_RECIPIENT_REFUSED"
    log = (await db_session.execute(select(InternalEmail))).scalar_one()
    assert (log.status, log.undelivered) == ("FAILED", ["gone@fundlok.com"])


async def test_gmail_failing_part_way_keeps_what_was_sent(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    fake_google.drop_after = 1

    resp = await client.post(
        "/admin/email/send",
        json=_message(to=["a@fundlok.com", "b@fundlok.com", "c@fundlok.com"]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "SENT"
    assert out["undelivered"] == ["b@fundlok.com", "c@fundlok.com"]
    assert [m["To"] for m in fake_google.messages] == ["a@fundlok.com"]


async def test_a_send_past_its_time_budget_returns_the_rest_undelivered(
    client, make_system_admin, fake_google, monkeypatch
):
    # A long list must not run past the proxy timeout while still sending:
    # the loop stops starting new copies and reports the rest.
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    monkeypatch.setattr(transport, "SEND_BUDGET_SECONDS", -1)

    resp = await client.post(
        "/admin/email/send",
        json=_message(to=["a@fundlok.com", "b@fundlok.com", "c@fundlok.com"]),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["undelivered"] == ["b@fundlok.com", "c@fundlok.com"]
    assert [m["To"] for m in fake_google.messages] == ["a@fundlok.com"]


async def test_failed_and_undelivered_copies_do_not_use_up_the_hour(
    client, make_system_admin, fake_google, monkeypatch
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    monkeypatch.setattr(service, "RECIPIENTS_PER_HOUR", 4)

    # A failed attempt to two people costs one.
    fake_google.token_endpoint_down = True
    failed = await client.post(
        "/admin/email/send", json=_message(to=["a@fundlok.com", "b@fundlok.com"]), headers=sysadmin["headers"]
    )
    assert failed.status_code == 502
    fake_google.token_endpoint_down = False

    # A send to three where one is refused costs the two that went: 1 + 2 = 3.
    fake_google.refuse = {"gone@fundlok.com"}
    partial = await client.post(
        "/admin/email/send",
        json=_message(to=["c@fundlok.com", "gone@fundlok.com", "d@fundlok.com"]),
        headers=sysadmin["headers"],
    )
    assert partial.status_code == 200, partial.text

    one_left = await client.post(
        "/admin/email/send", json=_message(to=["e@fundlok.com"]), headers=sysadmin["headers"]
    )
    assert one_left.status_code == 200, one_left.text
    none_left = await client.post(
        "/admin/email/send", json=_message(to=["f@fundlok.com"]), headers=sysadmin["headers"]
    )
    assert none_left.status_code == 429


# --------------------------------------------------------------------------- #
# Recipient directory
# --------------------------------------------------------------------------- #


async def _user(client, make_user, db_session, *, email, full_name, role=None, verified=True, status="ACTIVE"):
    created = await make_user(role=role, email=email, full_name=full_name)
    row = (await db_session.execute(select(User).where(User.id == UUID(created["id"])))).scalar_one()
    row.email_verified = verified
    row.status = status
    await db_session.commit()
    return created


async def test_the_recipient_directory_lists_active_verified_users(
    client, make_admin, make_user, db_session
):
    admin = await make_admin(email="lan.admin@fundlok.com")
    await _user(client, make_user, db_session, email="minh@fundlok.vn", full_name="Minh Tran")
    await _user(client, make_user, db_session, email="owner@shop.vn", full_name="Shop Owner", role="SME")
    await _user(client, make_user, db_session, email="new@shop.vn", full_name="Unverified", verified=False)
    await _user(client, make_user, db_session, email="gone@fundlok.com", full_name="Suspended", status="SUSPENDED")
    # Back in onboarding is still a working account (only SUSPENDED is a deny).
    await _user(client, make_user, db_session, email="kyc@shop.vn", full_name="Re-submitting", status="PENDING_KYC")

    resp = await client.get("/admin/email/recipients", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    emails = {r["email"] for r in resp.json()}
    assert {"minh@fundlok.vn", "owner@shop.vn", "kyc@shop.vn"} <= emails
    assert "new@shop.vn" not in emails and "gone@fundlok.com" not in emails
    owner = next(r for r in resp.json() if r["email"] == "owner@shop.vn")
    assert owner == {
        "id": owner["id"],
        "full_name": "Shop Owner",
        "email": "owner@shop.vn",
        "role": "SME",
        "is_fundlok": False,
    }

    fundlok = (
        await client.get("/admin/email/recipients?fundlok_only=true", headers=admin["headers"])
    ).json()
    assert fundlok and all(r["is_fundlok"] for r in fundlok)
    assert "minh@fundlok.vn" in {r["email"] for r in fundlok}
    assert "owner@shop.vn" not in {r["email"] for r in fundlok}

    found = (await client.get("/admin/email/recipients?q=minh", headers=admin["headers"])).json()
    assert [r["email"] for r in found] == ["minh@fundlok.vn"]
    # LIKE wildcards in the search are literal.
    assert (await client.get("/admin/email/recipients?q=%25", headers=admin["headers"])).json() == []


async def test_only_admins_can_list_recipients(client, make_user):
    investor = await make_user(role="INVESTOR")
    resp = await client.get("/admin/email/recipients", headers=investor["headers"])
    assert resp.status_code == 403


async def test_preview_renders_without_sending(client, make_system_admin, db_session, fake_google):
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

    assert fake_google.sent == []
    count = await db_session.scalar(select(func.count()).select_from(InternalEmail))
    assert count == 0


async def test_recent_messages_are_listed(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
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


async def test_rich_text_formatting_in_email(client, make_system_admin, fake_google):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)

    resp = await client.post(
        "/admin/email/send",
        json=_message(
            subject="Rich text announcement",
            body="<b>Important:</b> Please review our <i>guidelines</i> at <a href=\"https://fundlok.com/docs\">our docs</a>.",
        ),
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    html = _html_part(fake_google.messages[0])
    assert "<b>Important:</b>" in html
    assert "<i>guidelines</i>" in html
    assert '<a href="https://fundlok.com/docs"' in html
    assert 'color:#16a34a' in html


async def test_markdown_formatting_in_email_preview(client, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.post(
        "/admin/email/preview",
        json={
            "template": "general",
            "subject": "Markdown test",
            "heading": "Markdown heading",
            "body": "Here is **bold text** and *italic text* with [FundLok Link](https://fundlok.com).",
        },
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert "<b>bold text</b>" in preview["html"]
    assert "<i>italic text</i>" in preview["html"]
    assert '<a href="https://fundlok.com"' in preview["html"]
    assert "color:#16a34a" in preview["html"]


async def test_html_formatting_in_email_preview(client, make_system_admin):
    sysadmin = await make_system_admin()
    resp = await client.post(
        "/admin/email/preview",
        json={
            "template": "general",
            "subject": "HTML rich text test",
            "heading": "HTML heading",
            "body": "<div>Hello <b>world</b></div><div>Line 2 with <a href=\"https://fundlok.com\">link</a></div><ul><li>Item 1</li><li>Item 2</li></ul>",
        },
        headers=sysadmin["headers"],
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert "&lt;div&gt;" not in preview["html"]
    assert "<b>world</b>" in preview["html"]
    assert "Line 2" in preview["html"]
    assert '<a href="https://fundlok.com"' in preview["html"]
    assert "color:#16a34a" in preview["html"]
    assert "<ul><li>Item 1</li><li>Item 2</li></ul>" in preview["html"]


# --------------------------------------------------------------------------- #
# Repayment reminder sender
# --------------------------------------------------------------------------- #


async def test_only_a_system_admin_chooses_the_reminder_sender(client, make_admin, fake_google):
    admin = await make_admin()
    await _connect(client, fake_google, admin)
    assert (await client.get(REMINDER_SENDER, headers=admin["headers"])).status_code == 403
    resp = await client.put(
        REMINDER_SENDER, json={"user_id": admin["id"]}, headers=admin["headers"]
    )
    assert resp.status_code == 403


async def test_the_reminder_sender_is_chosen_from_connected_admins_and_audited(
    client, make_admin, make_system_admin, db_session, fake_google
):
    alice = await make_admin(email="alice@fundlok.com")
    bob = await make_admin(email="bob@fundlok.com")
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, alice, "alice@fundlok.com", from_name="Alice")

    # Nothing chosen yet; only Alice has a connected account to choose.
    start = (await client.get(REMINDER_SENDER, headers=sysadmin["headers"])).json()
    assert start["sender"] is None and start["updated_at"] is None
    assert [(c["gmail_address"], c["status"]) for c in start["candidates"]] == [
        ("alice@fundlok.com", "READY")
    ]

    resp = await client.put(
        REMINDER_SENDER, json={"user_id": alice["id"]}, headers=sysadmin["headers"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sender"] == {
        "user_id": alice["id"],
        "full_name": body["sender"]["full_name"],
        "gmail_address": "alice@fundlok.com",
        "from_name": "Alice",
        "status": "READY",
    }
    assert body["updated_by"] == sysadmin["id"] and body["updated_at"] is not None
    assert "refresh_token" not in json.dumps(body)

    # Alice's own screen says her account sends the reminders; Bob's does not.
    alices = (await client.get(SETTINGS, headers=alice["headers"])).json()
    assert alices["is_reminder_sender"] is True
    assert (await client.get(SETTINGS, headers=bob["headers"])).json()["is_reminder_sender"] is False
    saved = await client.put(SETTINGS, json=_profile(from_name="Alice"), headers=alice["headers"])
    assert saved.json()["is_reminder_sender"] is True

    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "REMINDER_SENDER_CHANGED")
        )
    ).scalar_one()
    assert str(audit.actor_id) == sysadmin["id"]
    assert audit.before_state == {"user_id": None, "gmail_address": None}
    assert audit.after_state == {"user_id": alice["id"], "gmail_address": "alice@fundlok.com"}


async def test_only_an_admin_with_a_connected_account_can_send_reminders(
    client, make_admin, make_system_admin, make_user, fake_google
):
    bob = await make_admin(email="bob@fundlok.com")
    investor = await make_user(role="INVESTOR")
    sysadmin = await make_system_admin()
    for user_id in (bob["id"], investor["id"], "00000000-0000-0000-0000-000000000000"):
        resp = await client.put(
            REMINDER_SENDER, json={"user_id": user_id}, headers=sysadmin["headers"]
        )
        assert resp.status_code == 400, (user_id, resp.text)
        assert resp.headers["X-Error-Code"] == "REMINDER_SENDER_UNAVAILABLE"
    resp = await client.put(REMINDER_SENDER, json={"user_id": "not-a-uuid"}, headers=sysadmin["headers"])
    assert resp.status_code == 422
    resp = await client.put(
        REMINDER_SENDER, json={"user_id": None, "extra": 1}, headers=sysadmin["headers"]
    )
    assert resp.status_code == 422


async def test_the_reminder_sender_follows_its_owners_switches(
    client, make_admin, make_system_admin, db_session, fake_google
):
    alice = await make_admin(email="alice@fundlok.com")
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, alice, "alice@fundlok.com")
    await client.put(REMINDER_SENDER, json={"user_id": alice["id"]}, headers=sysadmin["headers"])

    async def sender_status() -> str:
        body = (await client.get(REMINDER_SENDER, headers=sysadmin["headers"])).json()
        # The chosen account stays in the picker whatever its state.
        assert alice["id"] in [c["user_id"] for c in body["candidates"]]
        return body["sender"]["status"]

    ready = await service.resolve_reminder_sender(db_session)
    assert ready is not None and str(ready.user.id) == alice["id"]
    assert ready.account.gmail_address == "alice@fundlok.com"
    assert fake_google.accounts[ready.refresh_token] == "alice@fundlok.com"
    assert ready.refresh_token not in repr(ready)

    # Alice pauses sending: the reminders pause with it.
    await client.put(SETTINGS, json=_profile(enabled=False), headers=alice["headers"])
    assert await sender_status() == "PAUSED"
    assert await service.resolve_reminder_sender(db_session) is None
    # A paused account can still be chosen; the card says it is paused.
    resp = await client.put(
        REMINDER_SENDER, json={"user_id": alice["id"]}, headers=sysadmin["headers"]
    )
    assert resp.status_code == 200, resp.text

    await client.put(SETTINGS, json=_profile(enabled=True), headers=alice["headers"])
    assert await sender_status() == "READY"

    # Disconnected: still chosen, shown as such, but nothing can go out and
    # it cannot be chosen again until reconnected.
    await client.post(DISCONNECT, headers=alice["headers"])
    assert await sender_status() == "NOT_CONNECTED"
    assert await service.resolve_reminder_sender(db_session) is None
    resp = await client.put(
        REMINDER_SENDER, json={"user_id": alice["id"]}, headers=sysadmin["headers"]
    )
    assert resp.headers["X-Error-Code"] == "REMINDER_SENDER_UNAVAILABLE"

    await _connect(client, fake_google, alice, "alice@fundlok.com")
    assert await sender_status() == "READY"

    # No longer an admin: off, whatever the account's state.
    row = await db_session.get(User, UUID(alice["id"]))
    row.role = "INVESTOR"
    await db_session.commit()
    assert await sender_status() == "NOT_ADMIN"
    assert await service.resolve_reminder_sender(db_session) is None


async def test_clearing_the_reminder_sender_stops_reminders(
    client, make_system_admin, db_session, fake_google
):
    sysadmin = await make_system_admin()
    await _connect(client, fake_google, sysadmin)
    await client.put(REMINDER_SENDER, json={"user_id": sysadmin["id"]}, headers=sysadmin["headers"])
    assert await service.resolve_reminder_sender(db_session) is not None

    resp = await client.put(REMINDER_SENDER, json={"user_id": None}, headers=sysadmin["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["sender"] is None
    assert len(resp.json()["candidates"]) == 1
    assert await service.resolve_reminder_sender(db_session) is None
    assert (await client.get(SETTINGS, headers=sysadmin["headers"])).json()["is_reminder_sender"] is False

