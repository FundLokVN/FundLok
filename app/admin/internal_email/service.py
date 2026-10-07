"""Admin > Email: each admin's Gmail connection, the org-wide policy, preview,
and sending.

Every admin connects their own Gmail account and sends from it. Settings are
always looked up by the signed-in admin's id, never by an id from the request,
so one admin can neither read nor use another's credentials.

Spec: docs/specs/admin/internal-email.md. Failures the console must translate
carry an X-Error-Code header; `detail` stays a plain English string.
"""

import base64
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, status
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.admin.internal_email import crypto, transport
from app.admin.internal_email.models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    EmailSettings,
    InternalEmail,
)
from app.admin.internal_email.schemas import (
    EmailContent,
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
from app.system.models import SystemSetting
from app.users.models import USER_STATUS_SUSPENDED, Role, User
from app.utils.audit import append_audit
from app.utils.email import logo_bytes
from app.utils.email_templates import render_email

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
        "The app password cannot be stored until the server's encryption key is configured.",
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
        return EmailSettingsOut(configured=False, enabled=False, has_app_password=False)
    has_password = bool(row.app_password_encrypted)
    return EmailSettingsOut(
        configured=has_password,
        enabled=row.enabled,
        gmail_address=row.gmail_address,
        from_name=row.from_name,
        has_app_password=has_password,
        last_verified_at=row.last_verified_at,
        updated_at=row.updated_at,
    )


def _audit_state(row: EmailSettings | None) -> dict | None:
    """What the audit log may hold about the settings: never the password,
    not even encrypted, only whether there is one."""
    if row is None:
        return None
    return {
        "gmail_address": row.gmail_address,
        "from_name": row.from_name,
        "enabled": row.enabled,
        "has_app_password": bool(row.app_password_encrypted),
    }


async def get_settings(db: AsyncSession, *, actor_id: UUID) -> EmailSettingsOut:
    return _out(await _row(db, actor_id))


async def update_settings(
    db: AsyncSession, body: EmailSettingsUpdate, *, actor_id: UUID, ip_address: str | None
) -> EmailSettingsOut:
    row = await _row(db, actor_id, for_update=True)
    before = _audit_state(row)
    has_stored_password = row is not None and bool(row.app_password_encrypted)
    address_changed = row is None or row.gmail_address.lower() != body.gmail_address.lower()

    if body.app_password is None:
        # An App Password belongs to one Google account.
        if has_stored_password and address_changed:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "APP_PASSWORD_REQUIRED",
                "Enter the app password for the new Gmail address.",
            )
        if body.enabled and not has_stored_password:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "APP_PASSWORD_REQUIRED",
                "Enter the app password before turning sending on.",
            )

    encrypted = None
    if body.app_password is not None:
        try:
            encrypted = crypto.encrypt_password(body.app_password)
        except crypto.EncryptionUnavailable:
            raise _encryption_unavailable() from None

    if row is None:
        row = EmailSettings(
            user_id=actor_id, gmail_address=body.gmail_address, from_name=body.from_name
        )
        db.add(row)

    credentials_changed = encrypted is not None or address_changed
    row.gmail_address = body.gmail_address
    row.from_name = body.from_name
    row.enabled = body.enabled
    if encrypted is not None:
        row.app_password_encrypted = encrypted
    if credentials_changed:
        # A test email proved the old credentials, not these.
        row.last_verified_at = None

    after = _audit_state(row)
    after["password_changed"] = encrypted is not None
    append_audit(
        db,
        entity_type=_ENTITY,
        entity_id=actor_id,
        action="EMAIL_SETTINGS_UPDATED",
        actor_id=actor_id,
        before_state=before,
        after_state=after,
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


async def clear_settings(
    db: AsyncSession, *, actor_id: UUID, ip_address: str | None
) -> EmailSettingsOut:
    """Remove the admin's connected account entirely, password included."""
    row = await _row(db, actor_id, for_update=True)
    if row is not None:
        append_audit(
            db,
            entity_type=_ENTITY,
            entity_id=actor_id,
            action="EMAIL_SETTINGS_CLEARED",
            actor_id=actor_id,
            before_state=_audit_state(row),
            after_state=None,
            ip_address=ip_address,
        )
        await db.delete(row)
        await db.commit()
    return _out(None)


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


def _render(content: EmailContent, *, for_preview: bool = False) -> tuple[str, str]:
    """(html, text) for an admin's message."""
    context = {
        "heading": content.heading or "",
        "body": content.body,
        "button_label": content.button_label or "",
        "button_url": content.button_url or "",
    }
    if for_preview:
        logo_src = _preview_logo_src()
        if logo_src:
            context["logo_src"] = logo_src
    html = render_email(TEMPLATE_FILES[content.template], **context)

    lines = [content.heading, content.body] if content.heading else [content.body]
    if content.button_url:
        lines.append(f"{content.button_label}: {content.button_url}")
    text = "\n\n".join(lines) + "\n\n--\nFundLok"
    return html, text


def preview(content: EmailContent) -> EmailPreview:
    html, _ = _render(content, for_preview=True)
    return EmailPreview(subject=content.subject, html=html)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


async def _connected_account(
    db: AsyncSession, actor_id: UUID
) -> tuple[EmailSettings, str]:
    """The admin's own settings row and decrypted password, or the 409/503
    that says why there is nothing to send with."""
    row = await _row(db, actor_id)
    if row is None or not row.app_password_encrypted:
        raise _error(
            status.HTTP_409_CONFLICT,
            "EMAIL_NOT_CONFIGURED",
            "Connect your Gmail account under Email settings first.",
        )
    try:
        password = crypto.decrypt_password(row.app_password_encrypted)
    except crypto.EncryptionUnavailable:
        raise _encryption_unavailable() from None
    if password is None:
        raise _error(
            status.HTTP_409_CONFLICT,
            "APP_PASSWORD_UNREADABLE",
            "The stored app password can no longer be read. Enter it again.",
        )
    return row, password


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
    password: str,
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
        undelivered = await run_in_threadpool(
            transport.deliver, from_address, password, messages
        )
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
        raise _error(status.HTTP_502_BAD_GATEWAY, failure.code, failure.message)
    return log


async def send_test(
    db: AsyncSession, *, actor: User, ip_address: str | None
) -> EmailTestResult:
    """Send the test template to the signed-in admin's own address. Allowed
    while sending is paused, so the account can be checked before it is
    switched on."""
    account, password = await _connected_account(db, actor.id)
    # What is being tested, so only these credentials are marked verified.
    tested_address = account.gmail_address
    tested_ciphertext = account.app_password_encrypted
    html = render_email("internal-test", from_address=account.gmail_address)
    text = (
        f"This test email was sent from {account.gmail_address} through the FundLok "
        "admin console. If you can read it, sending from this account works."
    )
    await _send_logged(
        db,
        account=account,
        password=password,
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
    # Stamp the row only if it still holds the credentials just tested.
    verified_at = datetime.now(timezone.utc)
    await db.execute(
        update(EmailSettings)
        .where(
            EmailSettings.user_id == actor.id,
            EmailSettings.gmail_address == tested_address,
            EmailSettings.app_password_encrypted == tested_ciphertext,
        )
        .values(last_verified_at=verified_at)
    )
    await db.commit()
    return EmailTestResult(sent_to=actor.email, last_verified_at=verified_at)


async def send(
    db: AsyncSession, body: SendEmailRequest, *, actor: User, ip_address: str | None
) -> InternalEmailOut:
    account, password = await _connected_account(db, actor.id)
    if not account.enabled:
        raise _error(
            status.HTTP_409_CONFLICT,
            "EMAIL_DISABLED",
            "Sending is paused. Turn it on under Email settings.",
        )
    policy = await get_policy(db)
    recipients = _recipients(list(body.to), policy.allowed_domains)
    html, text = _render(body)
    log = await _send_logged(
        db,
        account=account,
        password=password,
        actor=actor,
        recipients=recipients,
        template=body.template,
        subject=body.subject,
        content={
            "heading": body.heading,
            "body": body.body,
            "button_label": body.button_label,
            "button_url": body.button_url,
        },
        html=html,
        text=text,
        ip_address=ip_address,
    )
    return InternalEmailOut.model_validate(log)


async def list_messages(
    db: AsyncSession, *, actor: User, limit: int
) -> list[InternalEmailOut]:
    """An admin sees their own sends; a system admin sees everyone's, which is
    the oversight of what goes out under FundLok's name."""
    query = select(InternalEmail).order_by(InternalEmail.created_at.desc()).limit(limit)
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
