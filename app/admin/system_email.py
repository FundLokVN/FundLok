"""Send one system email (a fee invoice, a repayment reminder) to one person.

From the admin Gmail account chosen under System settings (the reminder
sender, through the Gmail API) when one is chosen and can send. Otherwise,
or when Gmail refuses (paused, revoked, the hourly limit, Google down),
through the platform's SMTP relay, so the email still goes out.
"""

import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.internal_email import service as email_service
from app.admin.internal_email.schemas import EmailContent
from app.core.config import settings
from app.utils.email import send_email

logger = logging.getLogger(__name__)

CHANNEL_GMAIL = "GMAIL"
CHANNEL_PLATFORM = "PLATFORM"


class NotSent(Exception):
    """The platform relay refused the address outright."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def deliver(
    db: AsyncSession, *, to: str, content: EmailContent, record: dict, label: str
) -> tuple[str, str]:
    """Send `content` to `to`; (channel, from address). `record` is what the
    Gmail send log keeps as the content; `label` names the email in logs
    (never the address). Raises NotSent, or the relay's own exception."""
    html_body, text = email_service.render_system_email(content)
    sender = await email_service.resolve_reminder_sender(db)
    if sender is not None:
        try:
            await email_service.send_as_reminder_sender(
                db,
                sender,
                recipients=[to],
                template=content.template,
                subject=content.subject,
                content=record,
                html=html_body,
                text=text,
            )
            return CHANNEL_GMAIL, sender.account.gmail_address
        except HTTPException as exc:
            # The send may have left a transaction open (the hourly-limit lock).
            await db.rollback()
            code = (exc.headers or {}).get("X-Error-Code", str(exc.status_code))
            logger.warning(
                "%s: the reminder sender could not send (%s); using the platform email", label, code
            )
    if not await asyncio.to_thread(send_email, to, content.subject, html_body, text):
        raise NotSent("INVALID_ADDRESS")
    return CHANNEL_PLATFORM, settings.EMAILS_FROM_EMAIL
