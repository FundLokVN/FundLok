"""Deliver messages through the Gmail API as the admin's connected account.

Each send trades the stored refresh token for a fresh access token, then
posts each message (RFC 2822, base64url) to users.messages.send. Google's
hosts are constants in google.py; nothing on the settings screen can point
the server anywhere else.

Async httpx with a timeout per request, and an overall budget per send, so a
stalled Google cannot hold the request past the proxy in front of the API.
"""

import logging
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import httpx

from app.admin.internal_email import google
from app.utils.email import logo_bytes
from app.utils.email_templates import LOGO_CID

logger = logging.getLogger(__name__)

# Overall budget for one send's loop of messages. Past it, no new message is
# started and the rest are returned undelivered, as when the connection
# drops. It keeps the request well inside the 30 s proxy in front of the API,
# so a long list never times out in the browser while still sending (which
# would invite a duplicate send to everyone). The console puts the
# undelivered addresses back in the To list for a second send.
SEND_BUDGET_SECONDS = 25

# Gmail API answers that mean the token cannot send at all: revoked or
# expired (401), or connected without the gmail.send scope.
_RECONNECT_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "authError"}
# A 400 is about the one message (an address Gmail will not take) unless it
# says the account itself cannot send, e.g. "Mail service not enabled".
_ACCOUNT_REASONS = _RECONNECT_REASONS | {"failedPrecondition", "FAILED_PRECONDITION"}
# The Gmail API is not enabled on the OAuth client's Google Cloud project: a
# setup step, so retrying is pointless until someone enables it.
_API_DISABLED_REASONS = {"SERVICE_DISABLED", "accessNotConfigured"}


class DeliveryError(Exception):
    """A send that did not happen. `code` is the X-Error-Code the console
    translates; `message` is the English detail. Neither carries Gmail's raw
    response, which goes to the log instead."""

    def __init__(self, code: str, message: str, *, reconnect: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        # Google refused the token itself: the stored connection is dead and
        # only "Connect with Google" again can fix it.
        self.reconnect = reconnect


def _reconnect_required() -> DeliveryError:
    return DeliveryError(
        "GMAIL_RECONNECT_REQUIRED",
        "Google no longer accepts this connection. Reconnect your Google account under Email settings.",
        reconnect=True,
    )


def _unavailable() -> DeliveryError:
    return DeliveryError(
        "GMAIL_UNAVAILABLE", "Could not send through Gmail. Try again in a few minutes."
    )


def _api_disabled() -> DeliveryError:
    return DeliveryError(
        "GMAIL_API_DISABLED",
        "The Gmail API is not enabled in the Google Cloud project behind this "
        "connection. Ask the developer to enable it, then try again.",
    )


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


async def deliver(
    refresh_token: str, messages: list[tuple[str, EmailMessage]]
) -> list[str]:
    """Send each (recipient, message) as the connected account. Returns the
    recipients that did not get theirs.

    One message per recipient, so nobody sees anyone else's address: the
    recipients can be customers picked from the user list, not colleagues.

    Raises DeliveryError when nothing was sent at all: a revoked token
    (reconnect=True), Google unreachable, or every recipient refused. Google
    failing part-way leaves the rest undelivered and returns normally, so the
    sends that did go out are recorded as sent.

    Logs say which step failed and Google's status, never a token.
    """
    undelivered: list[str] = []
    sent = 0
    deadline = time.monotonic() + SEND_BUDGET_SECONDS
    async with google._client() as http:
        try:
            access_token = await google.refresh_access_token(refresh_token, http)
        except google.GoogleTokenRevoked as exc:
            raise _reconnect_required() from exc
        except google.GoogleUnavailable as exc:
            raise _unavailable() from exc

        for index, (recipient, message) in enumerate(messages):
            if index > 0 and time.monotonic() > deadline:
                logger.warning("Send budget used up; %d left undelivered", len(messages) - index)
                undelivered.extend(r for r, _ in messages[index:])
                break
            stop: DeliveryError | None = None
            try:
                resp = await google.send_raw(http, access_token, message.as_bytes())
            except httpx.HTTPError as exc:
                logger.warning("Gmail API send failed: %s", type(exc).__name__)
                stop = _unavailable()
            else:
                if resp.is_success:
                    sent += 1
                    continue
                reasons = google.gmail_error_reasons(resp)
                logger.warning(
                    "Gmail API refused a message (HTTP %s, %s)",
                    resp.status_code,
                    ",".join(sorted(reasons)) or "no reason",
                )
                if reasons & _API_DISABLED_REASONS:
                    stop = _api_disabled()
                elif resp.status_code == 400 and not reasons & _ACCOUNT_REASONS:
                    # A malformed or refused address: this recipient only.
                    undelivered.append(recipient)
                    continue
                elif resp.status_code == 401 or reasons & _RECONNECT_REASONS:
                    stop = _reconnect_required()
                else:
                    # 403 rate limits, 429, 5xx: stop, keep what went.
                    stop = _unavailable()
            if sent == 0:
                raise stop
            undelivered.extend(r for r, _ in messages[index:])
            break

    if sent == 0:
        raise DeliveryError("GMAIL_RECIPIENT_REFUSED", "Gmail refused the recipient address.")
    return undelivered
