"""Admin > Email: each admin's Gmail connection, the org-wide policy, preview,
and sending.

Every admin connects their own Google account with Google OAuth 2.0 and sends
from it through the Gmail API. Settings are always looked up by the signed-in
admin's id (or, on the OAuth callback, the id in the signed state that admin's
browser started), never by an id from the request body, so one admin can
neither read nor use another's credentials.

Spec: docs/specs/admin/internal-email.md. Failures the console must translate
carry an X-Error-Code header; `detail` stays a plain English string.
"""

import base64
import html
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urlencode
from uuid import UUID

from markupsafe import Markup

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, status
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.internal_email import crypto, google, transport
from app.admin.internal_email.models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    EmailSettings,
    InternalEmail,
)
from app.admin.internal_email.schemas import (
    EmailContent,
    EmailOAuthStart,
    EmailPolicyOut,
    EmailPolicyUpdate,
    EmailPreview,
    EmailSettingsOut,
    EmailSettingsUpdate,
    EmailTestResult,
    InternalEmailOut,
    RecipientOut,
    SendEmailRequest,
)
from app.core.config import settings
from app.system.models import SystemSetting
from app.users.models import USER_STATUS_SUSPENDED, Role, User
from app.utils.audit import append_audit
from app.utils.email import logo_bytes
from app.utils.email_templates import render_email

logger = logging.getLogger(__name__)

# Composer template -> exported React Email template (app/email_templates/).
TEMPLATE_FILES = {
    "general": "internal-general",
    "announcement": "internal-announcement",
    "action_required": "internal-action-required",
}

# system_settings key for the org-wide policy (EmailPolicyOut).
POLICY_KEY = "internal_email_policy"

# Recipients per admin per rolling hour, test emails included (one each).
# Counted in recipients, not sends, since each recipient is its own message.
# Gmail caps a Workspace mailbox at about 2,000 recipients a day; this keeps
# one admin well below that and bounds what a hijacked session could send.
RECIPIENTS_PER_HOUR = 200

# Addresses on these domains are FundLok's own: the composer's "FundLok only"
# filter and "add all FundLok emails" button use them.
FUNDLOK_EMAIL_DOMAINS = ("fundlok.com", "fundlok.vn")

_ENTITY = "EMAIL_SETTINGS"


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


def _encryption_unavailable() -> HTTPException:
    # The cause (EMAIL_SETTINGS_ENCRYPTION_KEY) is in the server log; the
    # response does not describe the deployment's configuration.
    return _error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "EMAIL_ENCRYPTION_UNAVAILABLE",
        "A Google account cannot be connected until the server's encryption key is configured.",
    )


def _reconnect_required() -> HTTPException:
    return _error(
        status.HTTP_409_CONFLICT,
        "GMAIL_RECONNECT_REQUIRED",
        "Google no longer accepts this connection. Reconnect your Google account under Email settings.",
    )


async def _row(
    db: AsyncSession, user_id: UUID, *, for_update: bool = False
) -> EmailSettings | None:
    """The given admin's own settings row. Every caller passes the signed-in
    admin's id."""
    query = (
        select(EmailSettings)
        .where(EmailSettings.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


def _out(row: EmailSettings | None) -> EmailSettingsOut:
    if row is None:
        return EmailSettingsOut(configured=False, enabled=False, needs_reconnect=False)
    connected = bool(row.refresh_token_encrypted)
    return EmailSettingsOut(
        configured=connected,
        enabled=row.enabled,
        gmail_address=row.gmail_address,
        from_name=row.from_name,
        # An account was connected (or an App Password saved, before OAuth)
        # and its access is gone without the admin disconnecting.
        needs_reconnect=not connected and bool(row.gmail_address),
        connected_at=row.token_updated_at if connected else None,
        last_verified_at=row.last_verified_at,
        updated_at=row.updated_at,
        signature_name=row.signature_name,
        signature_title=row.signature_title,
        signature_phone=row.signature_phone,
    )


def _audit_state(row: EmailSettings | None) -> dict | None:
    """What the audit log may hold about the settings: never the token, not
    even encrypted, only whether there is one."""
    if row is None:
        return None
    return {
        "gmail_address": row.gmail_address,
        "from_name": row.from_name,
        "enabled": row.enabled,
        "connected": bool(row.refresh_token_encrypted),
        "signature_name": row.signature_name,
        "signature_title": row.signature_title,
        "signature_phone": row.signature_phone,
    }


async def get_settings(db: AsyncSession, *, actor_id: UUID) -> EmailSettingsOut:
    return _out(await _row(db, actor_id))


async def update_settings(
    db: AsyncSession,
    body: EmailSettingsUpdate,
    *,
    actor_id: UUID,
    actor_email: str | None = None,
    ip_address: str | None,
) -> EmailSettingsOut:
    """Sender name, signature and the sending switch. The Google account
    itself is connected through the OAuth flow, never typed in here."""
    row = await _row(db, actor_id, for_update=True)
    before = _audit_state(row)
    # Turning sending ON needs a connected account. Leaving it on does not:
    # after Google revokes access (or the move from App Passwords) the switch
    # keeps its value, so editing a signature must not fail, and sending
    # resumes on reconnect. Sending itself still needs the token.
    turning_on = body.enabled and (row is None or not row.enabled)
    if turning_on and (row is None or not row.refresh_token_encrypted):
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "EMAIL_NOT_CONFIGURED",
            "Connect your Google account before turning sending on.",
        )
    if (
        body.enabled
        and actor_email
        and row is not None
        and row.gmail_address
        and row.gmail_address.strip().lower() != actor_email.strip().lower()
    ):
        raise _error(
            status.HTTP_409_CONFLICT,
            "GMAIL_EMAIL_MISMATCH",
            "The connected Google account does not match your signed-in account email. Connect your own Google account before turning sending on.",
        )

    if row is None:
        row = EmailSettings(user_id=actor_id, from_name=body.from_name)
        db.add(row)
    row.from_name = body.from_name
    row.enabled = body.enabled
    row.signature_name = body.signature_name
    row.signature_title = body.signature_title
    row.signature_phone = body.signature_phone

    append_audit(
        db,
        entity_type=_ENTITY,
        entity_id=actor_id,
        action="EMAIL_SETTINGS_UPDATED",
        actor_id=actor_id,
        before_state=before,
        after_state=_audit_state(row),
        ip_address=ip_address,
    )
    try:
        await db.commit()
    except IntegrityError:
        # Two first-time saves at once (two tabs): this admin's row exists now.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your email settings were changed at the same time. Reload and try again.",
        ) from None
    await db.refresh(row)
    return _out(row)


async def disconnect(
    db: AsyncSession, *, actor_id: UUID, ip_address: str | None
) -> EmailSettingsOut:
    """Forget the connected Google account: the token, the address and its
    verification, and switch sending off. Sender name and signature stay.
    The token is revoked at Google afterwards, best effort."""
    row = await _row(db, actor_id, for_update=True)
    if row is None:
        return _out(None)
    token = _decrypt_quietly(row.refresh_token_encrypted)
    if row.refresh_token_encrypted or row.gmail_address:
        before = _audit_state(row)
        row.refresh_token_encrypted = None
        row.oauth_scopes = None
        row.token_updated_at = None
        row.gmail_address = None
        row.last_verified_at = None
        row.enabled = False
        append_audit(
            db,
            entity_type=_ENTITY,
            entity_id=actor_id,
            action="EMAIL_GMAIL_DISCONNECTED",
            actor_id=actor_id,
            before_state=before,
            after_state=_audit_state(row),
            ip_address=ip_address,
        )
        await db.commit()
        await db.refresh(row)
    if token:
        # After the commit: the row no longer holds it whatever Google says.
        await google.revoke(token)
    return _out(row)


def _decrypt_quietly(stored: str | None) -> str | None:
    """The token for a best-effort revocation, or None if it cannot be read
    (no key, rotated key): disconnecting must work regardless."""
    try:
        return crypto.decrypt_secret(stored)
    except crypto.EncryptionUnavailable:
        return None


# --------------------------------------------------------------------------- #
# Google OAuth: connect
# --------------------------------------------------------------------------- #

# The ?error= values the console can show after the redirect back. Never
# Google's own text, which can carry anything.
OAUTH_CANCELLED = "cancelled"
OAUTH_INVALID_STATE = "invalid_state"
OAUTH_NOT_ADMIN = "not_admin"
OAUTH_SCOPE_MISSING = "scope_missing"
OAUTH_EMAIL_UNVERIFIED = "email_unverified"
OAUTH_EMAIL_MISMATCH = "email_mismatch"
OAUTH_DOMAIN_NOT_ALLOWED = "domain_not_allowed"
OAUTH_NO_REFRESH_TOKEN = "no_refresh_token"
OAUTH_NOT_CONFIGURED = "not_configured"
OAUTH_ENCRYPTION_UNAVAILABLE = "encryption_unavailable"
OAUTH_FAILED = "failed"


class OAuthCallbackError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def console_url(**params: str) -> str:
    """Admin > Email on the frontend, with the outcome of a connect."""
    base = settings.FRONTEND_URL.strip().rstrip("/") + "/admin/email"
    return f"{base}?{urlencode(params)}" if params else base


def start_oauth(*, actor_id: UUID) -> tuple[EmailOAuthStart, str]:
    """Google's consent URL for the signed-in admin, and the PKCE verifier
    the router puts in the browser-binding cookie."""
    if not google.is_configured():
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "GMAIL_OAUTH_NOT_CONFIGURED",
            "Connecting Google accounts is not set up on this server yet.",
        )
    verifier = google.new_code_verifier()
    challenge = google.code_challenge(verifier)
    state = google.create_state(actor_id, challenge)
    return EmailOAuthStart(authorization_url=google.authorization_url(state, challenge)), verifier


async def complete_oauth(
    db: AsyncSession,
    *,
    code: str | None,
    state: str | None,
    error: str | None,
    verifier: str | None,
    ip_address: str | None,
) -> None:
    """The callback's work: check the state against this browser, trade the
    code for tokens, and store the refresh token, encrypted, on the admin's
    own row. Raises OAuthCallbackError with the code the console shows."""
    if error:
        # access_denied when the admin pressed Cancel; anything else is a
        # misconfiguration on Google's side, logged by name only.
        if error != "access_denied":
            logger.warning("Google OAuth returned error=%s", error[:64])
        raise OAuthCallbackError(OAUTH_CANCELLED if error == "access_denied" else OAUTH_FAILED)
    read = google.read_state(state) if state else None
    if read is None or not code or not verifier:
        raise OAuthCallbackError(OAUTH_INVALID_STATE)
    actor_id, challenge = read
    # The browser that finished is the one that started: its cookie hashes
    # to the challenge signed into the state.
    if not secrets.compare_digest(google.code_challenge(verifier), challenge):
        raise OAuthCallbackError(OAUTH_INVALID_STATE)

    actor = await db.get(User, actor_id)
    if (
        actor is None
        or actor.role not in (Role.ADMIN.value, Role.SYSTEM_ADMIN.value)
        or actor.status == USER_STATUS_SUSPENDED
    ):
        raise OAuthCallbackError(OAUTH_NOT_ADMIN)

    try:
        grant = await google.exchange_code(code, verifier)
    except google.OAuthNotConfigured:
        raise OAuthCallbackError(OAUTH_NOT_CONFIGURED) from None
    except google.GoogleUnavailable:
        raise OAuthCallbackError(OAUTH_FAILED) from None

    rejection = _grant_rejection(grant, expected_email=actor.email)
    if rejection is not None:
        # Nothing of it is kept; do not leave the grant live at Google.
        await google.revoke(grant.refresh_token or grant.access_token)
        raise OAuthCallbackError(rejection)

    try:
        encrypted = crypto.encrypt_secret(grant.refresh_token)
    except crypto.EncryptionUnavailable:
        await google.revoke(grant.refresh_token)
        raise OAuthCallbackError(OAUTH_ENCRYPTION_UNAVAILABLE) from None

    row = await _row(db, actor_id, for_update=True)
    before = _audit_state(row)
    previous_token = None
    if row is None:
        row = EmailSettings(
            user_id=actor_id,
            from_name=(actor.full_name or grant.name or grant.email)[:80],
        )
        db.add(row)
    elif row.refresh_token_encrypted and (row.gmail_address or "").lower() != grant.email.lower():
        # A different Google account replaces this one: give the old grant
        # up. Never for the same account, where revoking the old token would
        # revoke the whole grant, the new token included.
        previous_token = _decrypt_quietly(row.refresh_token_encrypted)
    row.gmail_address = grant.email
    row.refresh_token_encrypted = encrypted
    row.oauth_scopes = " ".join(grant.scopes)
    row.token_updated_at = datetime.now(timezone.utc)
    # A test email proved the old token, not this one.
    row.last_verified_at = None
    append_audit(
        db,
        entity_type=_ENTITY,
        entity_id=actor_id,
        action="EMAIL_GMAIL_CONNECTED",
        actor_id=actor_id,
        before_state=before,
        after_state=_audit_state(row),
        ip_address=ip_address,
    )
    try:
        await db.commit()
    except IntegrityError:
        # A first-time connect raced a first-time save; the row exists now.
        await db.rollback()
        await google.revoke(grant.refresh_token)
        raise OAuthCallbackError(OAUTH_FAILED) from None
    if previous_token:
        await google.revoke(previous_token)


def _grant_rejection(
    grant: google.TokenGrant, *, expected_email: str | None = None
) -> str | None:
    if google.GMAIL_SEND_SCOPE not in grant.scopes:
        # Google's consent screen lets the admin untick "Send email on your
        # behalf"; without it the connection could never send.
        return OAUTH_SCOPE_MISSING
    if not grant.email or not grant.email_verified:
        return OAUTH_EMAIL_UNVERIFIED
    allowed = settings.gmail_connect_allowed_domains
    if allowed and grant.email.rsplit("@", 1)[-1].lower() not in allowed:
        return OAUTH_DOMAIN_NOT_ALLOWED
    if expected_email and grant.email.strip().lower() != expected_email.strip().lower():
        return OAUTH_EMAIL_MISMATCH
    if not grant.refresh_token:
        return OAUTH_NO_REFRESH_TOKEN
    return None


# --------------------------------------------------------------------------- #
# Org-wide policy
# --------------------------------------------------------------------------- #


async def get_policy(db: AsyncSession) -> EmailPolicyOut:
    row = await db.get(SystemSetting, POLICY_KEY, populate_existing=True)
    domains = (row.value or {}).get("allowed_domains") if row is not None else None
    return EmailPolicyOut(
        allowed_domains=[d for d in domains or [] if isinstance(d, str)],
        updated_at=row.updated_at if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
    )


async def update_policy(
    db: AsyncSession, body: EmailPolicyUpdate, *, actor_id: UUID, ip_address: str | None
) -> EmailPolicyOut:
    """SYSTEM_ADMIN only (router). Applies to every admin's next send."""
    before = (await get_policy(db)).allowed_domains
    row = await db.get(SystemSetting, POLICY_KEY)
    value = {"allowed_domains": body.allowed_domains}
    if row is None:
        db.add(
            SystemSetting(
                key=POLICY_KEY,
                value=value,
                description="Admin > Email: recipient domains every admin may send to",
                updated_by=actor_id,
            )
        )
    else:
        row.value = value
        row.updated_by = actor_id
    append_audit(
        db,
        entity_type="SYSTEM_SETTING",
        entity_id=None,
        action="EMAIL_POLICY_UPDATED",
        actor_id=actor_id,
        before_state={"allowed_domains": before},
        after_state=value,
        ip_address=ip_address,
    )
    await db.commit()
    return await get_policy(db)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _preview_logo_src() -> str | None:
    """The logo as a data: URI for the console's preview. A browser has no
    message to resolve cid: against. (Gmail strips data: URIs, which is why
    real sends attach the logo instead.)"""
    logo = logo_bytes()
    if logo is None:
        return None
    return "data:image/png;base64," + base64.b64encode(logo).decode()


_ALLOWED_EMAIL_TAGS = {"b", "strong", "i", "em", "u", "a", "p", "br", "ul", "ol", "li", "span", "div"}
_EMAIL_LINK_STYLE = "color:#16a34a;text-decoration:underline;font-weight:600;"


class _SafeEmailHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.output: list[str] = []
        self.plain: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag_lower = tag.lower()
        if tag_lower in _ALLOWED_EMAIL_TAGS:
            if tag_lower == "a":
                href = ""
                for k, v in attrs:
                    if k.lower() == "href" and v and (
                        v.startswith("http://") or v.startswith("https://") or v.startswith("mailto:")
                    ):
                        href = v
                if href:
                    safe_href = html.escape(href, quote=True)
                    self.output.append(
                        f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer" style="{_EMAIL_LINK_STYLE}">'
                    )
                else:
                    self.output.append("<span>")
            elif tag_lower == "br":
                self.output.append("<br/>")
                self.plain.append("\n")
            elif tag_lower == "div":
                current_out = "".join(self.output)
                if current_out and not current_out.endswith(("<br/>", "<br>", "</ul>", "</ol>")):
                    self.output.append("<br/>")
                    self.plain.append("\n")
            elif tag_lower == "p":
                current_out = "".join(self.output)
                if current_out:
                    if current_out.endswith("<br/>"):
                        self.output.append("<br/>")
                        self.plain.append("\n")
                    elif not current_out.endswith(("<br/><br/>", "</ul>", "</ol>")):
                        self.output.append("<br/><br/>")
                        self.plain.append("\n\n")
            elif tag_lower == "li":
                self.output.append("<li>")
                self.plain.append("• ")
            elif tag_lower in ("ul", "ol"):
                self.output.append(f"<{tag_lower}>")
                if self.plain and not "".join(self.plain).endswith("\n"):
                    self.plain.append("\n")
            else:
                self.output.append(f"<{tag_lower}>")
        else:
            # Escape disallowed start tag so raw tags (like <script>) stay visible as text
            raw = self.get_starttag_text() or f"<{tag}>"
            self.output.append(html.escape(raw))
            self.plain.append(raw)

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower in _ALLOWED_EMAIL_TAGS:
            if tag_lower == "a":
                self.output.append("</a>")
            elif tag_lower in ("p", "br", "div"):
                pass
            elif tag_lower in ("ul", "ol"):
                self.output.append(f"</{tag_lower}>")
                if self.plain and not "".join(self.plain).endswith("\n"):
                    self.plain.append("\n")
            elif tag_lower in ("b", "strong", "i", "em", "u", "li", "span"):
                self.output.append(f"</{tag_lower}>")
                if tag_lower == "li":
                    self.plain.append("\n")
        else:
            raw = f"</{tag}>"
            self.output.append(html.escape(raw))
            self.plain.append(raw)

    def handle_data(self, data: str):
        self.output.append(html.escape(data, quote=False))
        self.plain.append(data)


def sanitize_internal_email_body(body: str) -> tuple[Markup, str]:
    """Sanitize rich text / HTML or markdown from the admin email composer.
    Preserves safe formatting (bold, italic, links, lists) and styles links.
    Disallowed tags are HTML-escaped.
    Returns (safe_html_markup, plain_text_fallback).
    """
    if not body:
        return Markup(""), ""

    # Support markdown syntax if user typed markdown instead of HTML
    processed = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s\)]+|mailto:[^\s\)]+)\)",
        r'<a href="\g<2>">\g<1></a>',
        body,
    )
    processed = re.sub(r"\*\*([^*]+)\*\*", r"<b>\g<1></b>", processed)
    processed = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\g<1></i>", processed)

    parser = _SafeEmailHTMLParser()
    parser.feed(processed)
    return Markup("".join(parser.output)), "".join(parser.plain)


def _render(
    content: EmailContent,
    *,
    signature_name: str | None = None,
    signature_title: str | None = None,
    signature_phone: str | None = None,
    for_preview: bool = False,
) -> tuple[str, str]:
    """(html, text) for an admin's message.

    The signature is appended only when the admin ticked "Include my email
    signature" AND has set a signature name. A name with no title/phone
    still shows; a ticked box with no name shows nothing (there is nothing to
    sign with).
    """
    include_signature = content.include_signature and bool(signature_name)
    safe_body_html, plain_body = sanitize_internal_email_body(content.body)
    context = {
        "heading": content.heading or "",
        "body": safe_body_html,
        "button_label": content.button_label or "",
        "button_url": content.button_url or "",
        "include_signature": include_signature,
        "signature_name": signature_name or "",
        "signature_title": signature_title or "",
        "signature_phone": signature_phone or "",
    }
    if for_preview:
        logo_src = _preview_logo_src()
        if logo_src:
            context["logo_src"] = logo_src
    html = render_email(TEMPLATE_FILES[content.template], **context)

    lines = [content.heading, plain_body] if content.heading else [plain_body]
    if content.button_url:
        lines.append(f"{content.button_label}: {content.button_url}")
    if include_signature:
        sig = [signature_name]
        if signature_title:
            sig.append(signature_title)
        if signature_phone:
            sig.append(f"Mobile: {signature_phone}")
        text = "\n\n".join(lines) + "\n\n" + "\n".join(sig)
    else:
        text = "\n\n".join(lines) + "\n\n--\nFundLok"
    return html, text


async def preview(db: AsyncSession, content: EmailContent, *, actor_id: UUID) -> EmailPreview:
    """The rendered email, exactly as it would be sent. Sends nothing.

    The signature comes from the admin's own settings (set under Email
    settings), so the preview shows the same signature a send would.
    """
    row = await _row(db, actor_id)
    signature_name = row.signature_name if row is not None else None
    signature_title = row.signature_title if row is not None else None
    signature_phone = row.signature_phone if row is not None else None
    html, _ = _render(
        content,
        signature_name=signature_name,
        signature_title=signature_title,
        signature_phone=signature_phone,
        for_preview=True,
    )
    return EmailPreview(subject=content.subject, html=html)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


async def _connected_account(
    db: AsyncSession, actor_id: UUID, *, actor_email: str | None = None
) -> tuple[EmailSettings, str]:
    """The admin's own settings row and decrypted refresh token, or the
    409/503 that says why there is nothing to send with."""
    row = await _row(db, actor_id)
    if row is None or not row.refresh_token_encrypted or not row.gmail_address:
        raise _error(
            status.HTTP_409_CONFLICT,
            "EMAIL_NOT_CONFIGURED",
            "Connect your Google account under Email settings first.",
        )
    if (
        actor_email
        and row.gmail_address.strip().lower() != actor_email.strip().lower()
    ):
        raise _error(
            status.HTTP_409_CONFLICT,
            "GMAIL_EMAIL_MISMATCH",
            "The connected Google account does not match your signed-in account email. Connect your own Google account under Email settings.",
        )
    try:
        token = crypto.decrypt_secret(row.refresh_token_encrypted)
    except crypto.EncryptionUnavailable:
        raise _encryption_unavailable() from None
    if token is None:
        # Written under an encryption key that has since been rotated.
        raise _reconnect_required()
    return row, token


async def _mark_access_revoked(
    db: AsyncSession, *, actor_id: UUID, ciphertext: str, ip_address: str | None
) -> None:
    """Google refused the stored token for good. Drop it, so the console
    shows "Reconnect with Google", but only if the row still holds that very
    token: a reconnect that finished meanwhile must not be undone. The
    address stays, to say which account to reconnect."""
    row = await _row(db, actor_id, for_update=True)
    if row is None or row.refresh_token_encrypted != ciphertext:
        return
    before = _audit_state(row)
    row.refresh_token_encrypted = None
    row.oauth_scopes = None
    row.token_updated_at = None
    row.last_verified_at = None
    append_audit(
        db,
        entity_type=_ENTITY,
        entity_id=actor_id,
        action="EMAIL_GMAIL_ACCESS_REVOKED",
        actor_id=actor_id,
        before_state=before,
        after_state=_audit_state(row),
        ip_address=ip_address,
    )
    await db.commit()


async def _check_rate(db: AsyncSession, actor_id: UUID, recipient_count: int) -> None:
    """Raise 429 when sending to `recipient_count` more would take the admin
    past RECIPIENTS_PER_HOUR.

    Takes a transaction-scoped advisory lock on the admin first and holds it
    until the commit that writes the PENDING row (_send_logged). Without it,
    concurrent requests all read the same count before any of them inserts,
    and all pass. The lock covers only the count and the insert, never the
    Gmail call."""
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(actor_id), 0)))
    )
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    # What each logged send costs: a pending one its whole list (it may still
    # go out), a sent one the copies that actually went, a failed one a single
    # attempt. Charging failures in full would let a Gmail outage, retried a
    # few times, lock the admin out for the hour without anything sent.
    cost = case(
        (InternalEmail.status == STATUS_FAILED, 1),
        (
            InternalEmail.status == STATUS_SENT,
            func.cardinality(InternalEmail.recipients)
            - func.cardinality(InternalEmail.undelivered),
        ),
        else_=func.cardinality(InternalEmail.recipients),
    )
    used = await db.scalar(
        select(func.coalesce(func.sum(cost), 0)).where(
            InternalEmail.sent_by == actor_id, InternalEmail.created_at > since
        )
    )
    remaining = max(RECIPIENTS_PER_HOUR - int(used or 0), 0)
    if recipient_count > remaining:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "EMAIL_RATE_LIMITED",
            f"You can email {RECIPIENTS_PER_HOUR} recipients an hour; {remaining} left now. "
            "Try again later or send to fewer people.",
        )


def _domain(address: str) -> str:
    return validate_email(address, check_deliverability=False).ascii_domain.lower()


def _recipients(addresses: list[str], allowed_domains: list[str]) -> list[str]:
    """De-duplicated (case-insensitively, first spelling kept), and each inside
    the allowed domains when there are any."""
    unique: list[str] = []
    seen: set[str] = set()
    for address in addresses:
        if address.lower() not in seen:
            seen.add(address.lower())
            unique.append(address)
    if allowed_domains:
        allowed = set(allowed_domains)
        outside = []
        for address in unique:
            try:
                if _domain(address) not in allowed:
                    outside.append(address)
            except EmailNotValidError:
                outside.append(address)
        if outside:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "RECIPIENT_DOMAIN_NOT_ALLOWED",
                "These recipients are outside the allowed domains: " + ", ".join(outside),
            )
    return unique


async def _send_logged(
    db: AsyncSession,
    *,
    account: EmailSettings,
    refresh_token: str,
    actor: User,
    recipients: list[str],
    template: str,
    subject: str,
    content: dict,
    html: str,
    text: str,
    ip_address: str | None,
) -> InternalEmail:
    """Check the hourly limit, write the log row and commit it, then contact
    Gmail and record the outcome. Committing first means the attempt is on
    record even if the process dies mid-send, and the hourly limit already
    counts it.

    One message per recipient, each addressed to that person alone: the list
    can hold customers picked from the user directory, and none of them may
    see the others' addresses."""
    from_address = account.gmail_address
    ciphertext = account.refresh_token_encrypted
    try:
        messages = [
            (
                recipient,
                transport.build_message(
                    from_name=account.from_name,
                    from_address=from_address,
                    recipients=[recipient],
                    subject=subject,
                    html=html,
                    text=text,
                ),
            )
            for recipient in recipients
        ]
    except ValueError:
        # The email package refuses a header value it would split across
        # lines. The schemas reject those first; this is the backstop, so a
        # value they missed is a 422 and never a 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The subject, sender name or a recipient contains a line break.",
        ) from None

    await _check_rate(db, actor.id, len(recipients))
    log = InternalEmail(
        sent_by=actor.id,
        from_address=from_address,
        recipients=recipients,
        template=template,
        subject=subject,
        content=content,
        status=STATUS_PENDING,
    )
    db.add(log)
    await db.commit()

    failure: transport.DeliveryError | None = None
    undelivered: list[str] = []
    try:
        undelivered = await transport.deliver(refresh_token, messages)
    except transport.DeliveryError as exc:
        failure = exc

    if failure is None:
        log.status = STATUS_SENT
        log.sent_at = datetime.now(timezone.utc)
        log.undelivered = undelivered
        if undelivered:
            log.error_code = "SOME_RECIPIENTS_REFUSED"
    else:
        log.status = STATUS_FAILED
        log.error_code = failure.code
        log.undelivered = list(recipients)
    append_audit(
        db,
        entity_type="INTERNAL_EMAIL",
        entity_id=log.id,
        action="INTERNAL_EMAIL_SENT",
        actor_id=actor.id,
        after_state={
            "status": log.status,
            "template": template,
            "recipient_count": len(recipients),
            "undelivered_count": len(log.undelivered),
            "error_code": log.error_code,
        },
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(log)

    if failure is not None:
        if failure.reconnect:
            await _mark_access_revoked(
                db, actor_id=actor.id, ciphertext=ciphertext, ip_address=ip_address
            )
            raise _error(status.HTTP_409_CONFLICT, failure.code, failure.message)
        raise _error(status.HTTP_502_BAD_GATEWAY, failure.code, failure.message)
    return log


async def send_test(
    db: AsyncSession, *, actor: User, ip_address: str | None
) -> EmailTestResult:
    """Send the test template to the signed-in admin's own address. Allowed
    while sending is paused, so the account can be checked before it is
    switched on."""
    account, refresh_token = await _connected_account(db, actor.id, actor_email=actor.email)
    # What is being tested, so only this token is marked verified.
    tested_address = account.gmail_address
    tested_ciphertext = account.refresh_token_encrypted
    html = render_email("internal-test", from_address=account.gmail_address)
    text = (
        f"This test email was sent from {account.gmail_address} through the FundLok "
        "admin console. If you can read it, sending from this account works."
    )
    await _send_logged(
        db,
        account=account,
        refresh_token=refresh_token,
        actor=actor,
        recipients=[actor.email],
        template="test",
        subject="FundLok email test",
        content={},
        html=html,
        text=text,
        ip_address=ip_address,
    )
    # The settings may have changed (or been cleared) during the Gmail call.
    # Stamp the row only if it still holds the token just tested.
    verified_at = datetime.now(timezone.utc)
    await db.execute(
        update(EmailSettings)
        .where(
            EmailSettings.user_id == actor.id,
            EmailSettings.gmail_address == tested_address,
            EmailSettings.refresh_token_encrypted == tested_ciphertext,
        )
        .values(last_verified_at=verified_at)
    )
    await db.commit()
    return EmailTestResult(sent_to=actor.email, last_verified_at=verified_at)


async def send(
    db: AsyncSession, body: SendEmailRequest, *, actor: User, ip_address: str | None
) -> InternalEmailOut:
    account, refresh_token = await _connected_account(db, actor.id, actor_email=actor.email)
    if not account.enabled:
        raise _error(
            status.HTTP_409_CONFLICT,
            "EMAIL_DISABLED",
            "Sending is paused. Turn it on under Email settings.",
        )
    policy = await get_policy(db)
    recipients = _recipients(list(body.to), policy.allowed_domains)
    html, text = _render(
        body,
        signature_name=account.signature_name,
        signature_title=account.signature_title,
        signature_phone=account.signature_phone,
    )
    log = await _send_logged(
        db,
        account=account,
        refresh_token=refresh_token,
        actor=actor,
        recipients=recipients,
        template=body.template,
        subject=body.subject,
        content={
            "heading": body.heading,
            "body": body.body,
            "button_label": body.button_label,
            "button_url": body.button_url,
            "include_signature": body.include_signature,
        },
        html=html,
        text=text,
        ip_address=ip_address,
    )
    return InternalEmailOut.model_validate(log)


async def list_messages(
    db: AsyncSession, *, actor: User, limit: int, offset: int = 0
) -> list[InternalEmailOut]:
    """An admin sees their own sends; a system admin sees everyone's, which is
    the oversight of what goes out under FundLok's name."""
    query = (
        select(InternalEmail)
        .order_by(InternalEmail.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if actor.role != Role.SYSTEM_ADMIN.value:
        query = query.where(InternalEmail.sent_by == actor.id)
    rows = (await db.execute(query)).scalars()
    return [InternalEmailOut.model_validate(row) for row in rows]


# --------------------------------------------------------------------------- #
# Recipient directory
# --------------------------------------------------------------------------- #


def _is_fundlok(address: str) -> bool:
    return address.rsplit("@", 1)[-1].lower() in FUNDLOK_EMAIL_DOMAINS


async def list_recipients(
    db: AsyncSession, *, query: str | None, fundlok_only: bool, limit: int
) -> list[RecipientOut]:
    """Users an admin can add to the To list: every account that is not
    suspended (the codebase's one deny status; PENDING_KYC is normal
    onboarding) and has a verified email (an unverified address may be a typo
    or someone else's). Admins already see every user in the console, so this
    exposes nothing new."""
    domain = func.lower(func.split_part(User.email, "@", 2))
    stmt = select(User).where(
        User.status != USER_STATUS_SUSPENDED, User.email_verified.is_(True)
    )
    if fundlok_only:
        stmt = stmt.where(domain.in_(FUNDLOK_EMAIL_DOMAINS))
    if query and query.strip():
        # Escape LIKE wildcards so a "%" or "_" in the search is literal.
        term = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        stmt = stmt.where(
            User.email.ilike(pattern, escape="\\") | User.full_name.ilike(pattern, escape="\\")
        )
    stmt = stmt.order_by(func.lower(func.coalesce(User.full_name, User.email)), User.email).limit(limit)
    users = (await db.execute(stmt)).scalars().all()
    return [
        RecipientOut(
            id=user.id,
            full_name=user.full_name,
            email=user.email,
            role=user.role,
            is_fundlok=_is_fundlok(user.email),
        )
        for user in users
    ]
