import logging
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from email_validator import EmailNotValidError, validate_email

from app.core.config import settings
from app.utils.email_templates import LOGO_CID, render_email

logger = logging.getLogger(__name__)

# The brand mark, embedded in every HTML email rather than linked.
#
# WHY CID AND NOT A URL
# A hosted <img src="https://..."> is simpler, but it fails in the two places
# that matter: mail clients that block remote images show a broken box, and
# local development has no publicly reachable host at all, so every email sent
# through Mailpit would be logo-less. A CID part travels inside the message, so
# it renders offline, with images blocked, and in dev. A base64 data: URI is the
# third option and is not viable -- Gmail strips them.
#
# The HTML itself comes from the React Email templates (emails/), exported to
# app/email_templates/ and filled in by app/utils/email_templates.py. They
# reference the logo as <img src="cid:fundlok-logo">.
_LOGO_PATH = Path(__file__).resolve().parents[1] / "assets" / "email-logo.png"


@lru_cache(maxsize=1)
def logo_bytes() -> bytes | None:
    """The logo, read once per process. `None` if the asset is missing: the
    email still goes, showing the image's alt text ("FundLok"), because a
    password reset must not be blocked by a missing image."""
    try:
        return _LOGO_PATH.read_bytes()
    except OSError:
        return None


if TYPE_CHECKING:
    # Type-only: app.contact.router imports this module, so a runtime import
    # back into app.contact would close the loop.
    from app.contact.schemas import ContactPurpose


def valid_email_address(address: object) -> str | None:
    """The address in normalised form if it is a well-formed email, else None.

    Syntax only (check_deliverability=False): no DNS lookup, so it is fast,
    works offline, and never makes a send depend on a resolver. CR/LF are
    refused outright because the address goes into a mail header, where a
    newline would let the value add headers of its own (Bcc: ...).
    """
    if not isinstance(address, str) or "\r" in address or "\n" in address:
        return None
    try:
        return validate_email(address, check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def send_email(
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str | None = None,
    reply_to: str | None = None
) -> bool:
    """Send one email. Returns False, without sending, if ``to_email`` is not a
    well-formed address.

    Checked here, at the one place every email leaves through, so no caller
    can skip it. Callers run as background tasks after the response has gone,
    so a bad address is logged and skipped rather than raised: raising would
    only produce a traceback nobody sees. The address itself is not logged;
    the subject says which email was skipped.
    """
    recipient = valid_email_address(to_email)
    if recipient is None:
        logger.warning("Email not sent, recipient is not a valid address: %s", subject)
        return False
    # Reply-To is a convenience, never a reason to drop an email: if it is
    # malformed the email goes without it.
    if reply_to is not None:
        reply_to = valid_email_address(reply_to)
        if reply_to is None:
            logger.warning("Reply-To dropped, not a valid address: %s", subject)

    # 1. Create message
    #
    # multipart/related wrapping multipart/alternative -- the nesting matters.
    # The alternative part holds the text and HTML bodies so a client picks one;
    # the related part carries the HTML's inline images alongside it. Attaching
    # the image directly to an alternative part instead would advertise the logo
    # as a THIRD body the client may choose to render in place of the email.
    message = MIMEMultipart("related")
    message["Subject"] = subject
    message["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
    message["To"] = recipient
    if reply_to:
        message["Reply-To"] = reply_to

    if not text_content:
        text_content = "Please view this email in an HTML-compatible client."

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text_content, "plain"))
    body.attach(MIMEText(html_content, "html"))
    message.attach(body)

    logo = logo_bytes()
    if logo is not None:
        image = MIMEImage(logo, "png")
        # Angle brackets are part of the Content-ID header syntax; the HTML
        # refers to the bare id. Getting this wrong is the usual reason a CID
        # image renders as a broken box.
        image.add_header("Content-ID", f"<{LOGO_CID}>")
        # "inline" keeps it out of the client's attachment list -- without it
        # every email arrives looking like it has a file attached.
        image.add_header("Content-Disposition", "inline", filename="fundlok.png")
        message.attach(image)

    # 2. Connect and send
    if settings.SMTP_SECURE:
        server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT)
    else:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT)
        
    try:
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.EMAILS_FROM_EMAIL, recipient, message.as_string())
    finally:
        server.quit()
    return True


def send_verification_email(to_email: str, token: str) -> None:
    # Points at the frontend, which calls the API with the token.
    verification_link = f"{settings.FRONTEND_URL}/verify-email?token={token}"

    subject = "Verify your email for FundLok"

    html_content = render_email("verification", link=verification_link)

    text_content = f"Please verify your email by visiting this link: {verification_link}"
    
    send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content,
        text_content=text_content
    )

def send_contact_autoreply(to_email: str) -> None:
    """Acknowledge a contact-form submission to the address the sender typed.

    Carries NO text from the submission, not even the name, and that is the
    point. The form is anonymous and the address is unverified, so whatever
    this email contains goes, under FundLok's brand and from FundLok's domain,
    to any inbox the submitter chooses. It used to greet "Hi {name}" with the
    name unescaped: a "name" of HTML became a FundLok-branded phishing email
    (links, fake buttons) sent to a victim of the attacker's choosing. Escaping
    would stop the HTML but not plain-text bait ("your account is locked, call
    ..."), so the greeting is generic instead. The submitted details still
    reach the team, escaped, in send_contact_notification.
    """
    subject = "We have received your message - FundLok"

    # The template takes no variables: nothing submitted can reach it.
    html_content = render_email("contact-autoreply")

    text_content = "Hello,\n\nWe have successfully received your information. Our team will review your message and get back to you as soon as possible.\n\nBest regards,\nThe FundLok Team"

    send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content,
        text_content=text_content
    )


def send_contact_notification(
    name: str,
    email: str,
    message: str,
    subject: str | None = None,
    purpose: "ContactPurpose | None" = None
) -> None:
    """Forward a contact-form submission to the team inbox.

    Everything here comes from an anonymous public form, so every field is
    HTML-escaped before it reaches the template. The submitter's address is set
    as Reply-To rather than From: sending as them would fail SPF/DKIM for our
    domain and land the notification in spam.

    The subject is prefixed with the purpose the sender picked
    ("[Partnership] …"), so the inbox can be filtered and triaged on the header
    alone. The prefix comes from the server-side enum label, never from the
    request body.
    """
    # Subject and name land in a mail header, so newlines are stripped out
    # (they would otherwise let a submitter inject extra headers) and the
    # result is capped to keep the header a sane length.
    def header_safe(value: str) -> str:
        return " ".join(value.split())[:120]

    topic = header_safe(subject or "") or "(no subject)"
    name = header_safe(name) or "(no name)"
    # "General" covers a submission from a cached bundle that predates the
    # dropdown, so the subject shape stays constant for inbox filters.
    purpose_label = purpose.label if purpose else "General"

    # render_email escapes every value; the template runs `message` through
    # nl2br, which escapes each line and joins them with <br/>.
    html_content = render_email(
        "contact-notification",
        purpose=purpose_label,
        name=name,
        email=email,
        topic=topic,
        message=message,
    )

    text_content = (
        f"New contact form submission\n\n"
        f"Purpose: {purpose_label}\n"
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Subject: {topic}\n\n"
        f"Message:\n{message}\n"
    )

    send_email(
        to_email=settings.CONTACT_INBOX_EMAIL,
        subject=f"[{purpose_label}] {topic} - {name}",
        html_content=html_content,
        text_content=text_content,
        reply_to=email
    )


def send_password_reset_email(to_email: str, token: str) -> None:
    reset_link = f"{settings.FRONTEND_URL}/reset-password?token={token}"

    subject = "Reset your password for FundLok"

    html_content = render_email("password-reset", link=reset_link)

    text_content = f"Please reset your password by visiting this link: {reset_link}"

    send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content,
        text_content=text_content
    )


def send_admin_invite_email(to_email: str, token: str, invited_by: str, expires_hours: int) -> None:
    """Invite a new operator to the admin console.

    The account already exists (role ADMIN, no password). The link is an
    ordinary password-reset token, so setting a password through it is the
    whole of onboarding and the link dies once used. `invited_by` is a person's
    display name and is escaped: it is user-editable profile text.
    """
    setup_link = f"{settings.FRONTEND_URL}/reset-password?token={token}&invite=1"
    forgot_link = f"{settings.FRONTEND_URL}/forgot-password"

    subject = "You've been added as a FundLok admin"

    html_content = render_email(
        "admin-invite",
        inviter=invited_by,
        setup_link=setup_link,
        forgot_link=forgot_link,
        expires_hours=expires_hours,
    )

    text_content = (
        f"{invited_by} has created a FundLok admin account for this email address. "
        f"Choose a password to finish setting it up: {setup_link} "
        f"The link works once and expires in {expires_hours} hours. If it has expired, "
        f"request a new one at {forgot_link}."
    )

    send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )


def send_password_changed_notice(to_email: str, first_time: bool = False) -> None:
    """Tell the account owner their password just changed.

    Purely a security notification -- it carries no token and grants nothing.
    Its whole job is to make an unauthorised change visible to the real owner
    while they can still act on it, which is why it always names the recovery
    route. Send it AFTER the change has been committed, so the email never
    describes something that didn't happen.

    `first_time` swaps the wording for an account setting its first password
    (the OAuth case) -- "was changed" would be untrue there.
    """
    reset_link = f"{settings.FRONTEND_URL}/forgot-password"

    subject = (
        "Your FundLok password was set"
        if first_time
        else "Your FundLok password was changed"
    )

    html_content = render_email(
        "password-set" if first_time else "password-changed", reset_link=reset_link
    )

    text_content = (
        (
            "A password was just added to your FundLok account. You can now sign in "
            "with your email address and password."
            if first_time
            else "The password on your FundLok account was just changed."
        )
        + " If this was you, no action is needed. If it wasn't, reset your password "
        f"immediately at {reset_link} and contact us so we can secure your account."
    )

    send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )


def send_existing_account_notice(to_email: str) -> None:
    """Notify the real owner that someone tried to register with their email.

    Sent instead of a verification email when registration hits an
    already-registered address. This lets the legitimate owner react (log in
    or reset their password) without the registration endpoint ever confirming
    to the submitter that the account exists — the anti-enumeration guarantee.
    """
    login_link = f"{settings.FRONTEND_URL}/login"
    reset_link = f"{settings.FRONTEND_URL}/forgot-password"

    subject = "About your FundLok account"

    html_content = render_email(
        "existing-account", login_link=login_link, reset_link=reset_link
    )

    text_content = (
        "We received a request to create a FundLok account with this email, but "
        f"you already have one. Sign in at {login_link} or reset your password at "
        f"{reset_link}. If this wasn't you, you can ignore this email."
    )

    send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )


def send_new_device_signin_alert(
    to_email: str, *, device: str, browser: str, ip_address: str | None
) -> None:
    """Tell the owner a device we have not seen before just signed in.

    Same contract as send_password_changed_notice: a notification, no token, no
    action it can be tricked into performing. Its job is to make an
    unauthorised sign-in visible while the owner can still act, so it names the
    two things they would do next -- change the password, or sign the device out
    from the security screen.

    Sent only when the account has sign-in alerts switched on
    (users.signin_alerts_enabled), and only for an unrecognised device: mailing
    on every sign-in trains people to ignore it.
    """
    security_link = f"{settings.FRONTEND_URL}/dashboard/security"
    reset_link = f"{settings.FRONTEND_URL}/forgot-password"
    # Escaped (render_email escapes every value) although safe today:
    # device/browser come from fixed labels plus digits (app/auth/user_agent.py),
    # but they are derived from a header the signing-in party controls, and this
    # email goes to the account owner.
    html_content = render_email(
        "new-device-signin",
        browser=browser,
        device=device,
        where=ip_address or "an unknown address",
        security_link=security_link,
        reset_link=reset_link,
    )

    send_email(
        to_email=to_email,
        subject="New sign-in to your FundLok account",
        html_content=html_content,
    )


def send_sign_in_locked_notice(
    to_email: str, *, attempts: int, minutes: int, ip_address: str | None
) -> None:
    """Tell the owner that wrong passwords have paused password sign-in.

    Same contract as send_new_device_signin_alert: a notification, no token,
    nothing it can be tricked into doing. A run of wrong passwords means
    someone is guessing, and the owner is the one person who can tell whether
    it was them, so it names what to do either way. app/auth/service.py sends
    it at most once an hour per account, so a guesser who waits out each lock
    cannot turn it into a flood.
    """
    security_link = f"{settings.FRONTEND_URL}/dashboard/security"
    reset_link = f"{settings.FRONTEND_URL}/forgot-password"
    where = ip_address or "an unknown address"
    html_content = render_email(
        "sign-in-locked",
        attempts=attempts,
        minutes=minutes,
        where=where,
        security_link=security_link,
        reset_link=reset_link,
    )
    text_content = (
        f"Someone entered the wrong password for your FundLok account {attempts} "
        f"times, most recently from {where}. None of those attempts signed in. "
        f"Signing in with a password is paused for {minutes} minutes. If that was "
        f"you, wait and try again, or reset your password at {reset_link}. If it "
        "was not you, choose a strong password and turn on two-factor "
        f"authentication at {security_link}."
    )

    send_email(
        to_email=to_email,
        subject="Too many failed sign-in attempts on your FundLok account",
        html_content=html_content,
        text_content=text_content,
    )


def send_application_updated_email(to_email: str) -> None:
    """Tell an applicant their loan application has an update, with a button to
    read it on the site.

    Deliberately says only that it was UPDATED, not what the decision was: the
    verdict, the reason and the next steps belong behind the sign-in, where the
    page can show them with the disclosures that must accompany them. An email
    that announced "approved" would read as a promise of funds, and an
    investor-funded facility is not one until it is signed. Vietnamese first,
    English beneath, because the account has no stored language.
    """
    link = f"{settings.FRONTEND_URL}/dashboard"
    subject = "Hồ sơ vay của bạn đã được cập nhật | Your loan application has been updated"

    html_content = render_email("application-updated", link=link)

    text_content = (
        "Hồ sơ vay của bạn trên FundLok đã được cập nhật. Đăng nhập để xem chi tiết: "
        f"{link}\n\n"
        "Your loan application on FundLok has been updated. Sign in to see the "
        f"details: {link}"
    )

    send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )
