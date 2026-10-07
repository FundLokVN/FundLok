"""Deliver one message through Gmail SMTP with the stored app password.

FIXED HOST
Gmail's SMTP endpoint is a constant, not a setting. If the host and port were
editable, the settings screen could point the server at anything on its
network and have it open connections there (SSRF over SMTP). Only the address
and password are configurable.

TLS
Implicit TLS on 465 with `ssl.create_default_context()`, which verifies the
certificate and host name. smtplib's own default context does neither, which
would hand the app password to anyone able to intercept the connection.

Blocking smtplib, called from the async service through a thread
(run_in_threadpool), with a timeout so a stalled connection cannot hold a
worker forever.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.utils.email import logo_bytes
from app.utils.email_templates import LOGO_CID

logger = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465
TIMEOUT_SECONDS = 20


class DeliveryError(Exception):
    """A send that did not happen. `code` is the X-Error-Code the console
    translates; `message` is the English detail. Neither carries Gmail's raw
    response, which goes to the log instead."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def build_message(
    *,
    from_name: str,
    from_address: str,
    recipients: list[str],
    subject: str,
    html: str,
    text: str,
) -> EmailMessage:
    """multipart/alternative (text, then HTML), with the logo attached to the
    HTML part as a related inline image, so a client never offers it as a
    third body or lists it as an attachment.

    EmailMessage refuses a header value containing CR or LF, a second guard
    behind the schema's single-line validation.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((from_name, from_address))
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=from_address.rsplit("@", 1)[-1])
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    logo = logo_bytes()
    if logo is not None:
        html_part = message.get_payload()[1]
        html_part.add_related(
            logo,
            maintype="image",
            subtype="png",
            cid=f"<{LOGO_CID}>",
            filename="fundlok.png",
            disposition="inline",
        )
    return message


def _connect() -> smtplib.SMTP_SSL:
    # A seam: tests replace this with a fake server.
    return smtplib.SMTP_SSL(
        GMAIL_SMTP_HOST,
        GMAIL_SMTP_PORT,
        timeout=TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )


def deliver(gmail_address: str, app_password: str, message: EmailMessage) -> list[str]:
    """Send `message`. Returns the recipients Gmail refused while accepting the
    rest (normally none). Raises DeliveryError when nothing was sent.

    Logs say which step failed and Gmail's numeric code, never the password.
    """
    try:
        with _connect() as server:
            server.login(gmail_address, app_password)
            refused = server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        logger.warning("Gmail rejected the login (SMTP %s)", exc.smtp_code)
        raise DeliveryError(
            "GMAIL_AUTH_FAILED", "Google rejected the Gmail address or app password."
        ) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        logger.warning("Gmail refused every recipient")
        raise DeliveryError(
            "GMAIL_RECIPIENT_REFUSED", "Gmail refused the recipient address."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        # OSError covers DNS failure, refused connections, TLS errors, timeouts.
        logger.warning("Gmail send failed: %s", type(exc).__name__)
        raise DeliveryError(
            "GMAIL_UNAVAILABLE", "Could not send through Gmail. Try again in a few minutes."
        ) from exc
    return sorted(refused)
