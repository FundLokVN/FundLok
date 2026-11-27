import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# The templates an admin can compose with. Each maps to an exported React
# Email template in service.TEMPLATE_FILES.
ComposeTemplate = Literal["general", "announcement", "action_required"]

# Per email. Each recipient gets their own copy (service.send), so this bounds
# one burst of SMTP sends, not who can see whom.
MAX_RECIPIENTS = 50

# Any control character, CR/LF included. Values that land in a mail header
# (subject, from name) or on a single line of the email must not carry one:
# a newline in a header would let the value add headers of its own (Bcc: …).
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# A lower-case ASCII host name with at least one dot. IDNs go in their
# punycode (xn--) form, which is what the recipient check compares against.
_DOMAIN = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})"
)


def _single_line(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    # splitlines() as well as the control class: the email package treats
    # U+0085, U+2028 and U+2029 as line breaks too, and refuses a header that
    # contains one (a 500 if it got that far).
    if _CONTROL.search(value) or len(value.splitlines()) > 1:
        raise ValueError(f"{field} must be a single line of text")
    return value


def _plain_address(address: str) -> str:
    """Refuse an RFC 2047 encoded word ("=?utf-8?q?...?=") in an address. It
    passes email validation, but the header parser decodes it and may find a
    CR/LF inside, which it refuses: a 500 instead of a 422."""
    if "=?" in address:
        raise ValueError(f"'{address[:100]}' is not a plain email address")
    return address


class EmailSettingsOut(BaseModel):
    """The signed-in admin's own Gmail connection. Never the OAuth token:
    only whether one is stored (`configured`)."""

    # A Google account is connected and can send.
    configured: bool
    enabled: bool
    # The connected Google account, from Google's verified ID token. Also set
    # when `needs_reconnect`, to say which account to reconnect.
    gmail_address: str | None = None
    from_name: str | None = None
    # An account was connected but Google no longer accepts it (revoked in
    # the Google account, or an App Password connection from before OAuth).
    needs_reconnect: bool = False
    # When the current token was granted.
    connected_at: datetime | None = None
    last_verified_at: datetime | None = None
    updated_at: datetime | None = None
    # The admin's email signature (appended when "Include my email signature"
    # is ticked). Optional: nothing is appended when name is blank.
    signature_name: str | None = None
    signature_title: str | None = None
    signature_phone: str | None = None
    # A system admin chose this account to send FundLok's automatic
    # repayment reminders (System settings), so disconnecting it or pausing
    # sending also stops those.
    is_reminder_sender: bool = False


class EmailSettingsUpdate(BaseModel):
    """What an admin edits by hand. The Google account itself comes from the
    OAuth flow (GET /admin/email/oauth/authorize), never from this body."""

    model_config = ConfigDict(extra="forbid")

    from_name: str = Field(min_length=1, max_length=80)
    # Turning it on needs a connected account (400 EMAIL_NOT_CONFIGURED).
    enabled: bool
    # The admin's email signature. Blank keeps the stored value for the
    # existing fields (the whole row is sent each save).
    signature_name: str | None = Field(default=None, max_length=80)
    signature_title: str | None = Field(default=None, max_length=120)
    signature_phone: str | None = Field(default=None, max_length=40)

    @field_validator("from_name")
    @classmethod
    def _from_name(cls, value: str) -> str:
        value = _single_line(value, "The sender name") or ""
        if not value:
            raise ValueError("The sender name cannot be blank")
        return value

    @field_validator("signature_name", "signature_title", "signature_phone")
    @classmethod
    def _signature_single_line(cls, value: str | None) -> str | None:
        # A newline in a signature line would let it inject extra lines into
        # the message body. Single line, blank kept as None.
        return _single_line(value, "A signature field") or None


class EmailOAuthStart(BaseModel):
    """Where to send the admin's browser to connect their Google account."""

    authorization_url: str


class EmailPolicyOut(BaseModel):
    """Org-wide rules for every admin's email. Any admin can read them (the
    composer shows them); only a system admin can change them."""

    allowed_domains: list[str]
    updated_at: datetime | None = None
    updated_by: UUID | None = None


class EmailPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Recipient domains any admin may send to. Empty means any domain.
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            domain = raw.strip().lower().lstrip("@")
            if not domain:
                continue
            if not _DOMAIN.fullmatch(domain):
                raise ValueError(f"'{raw[:100]}' is not a domain name, e.g. fundlok.com")
            if domain not in cleaned:
                cleaned.append(domain)
        return cleaned


class EmailContent(BaseModel):
    """What an admin writes. Plain text throughout: the template escapes it."""

    model_config = ConfigDict(extra="forbid")

    template: ComposeTemplate
    subject: str = Field(min_length=1, max_length=200)
    heading: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)
    button_label: str | None = Field(default=None, max_length=60)
    button_url: str | None = Field(default=None, max_length=2_000)
    # Append the admin's signature (from their email settings) to the message.
    include_signature: bool = False

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        value = _single_line(value, "The subject") or ""
        if not value:
            raise ValueError("The subject cannot be blank")
        return value

    @field_validator("heading")
    @classmethod
    def _heading(cls, value: str | None) -> str | None:
        return _single_line(value, "The heading") or None

    @field_validator("button_label")
    @classmethod
    def _button_label(cls, value: str | None) -> str | None:
        return _single_line(value, "The button label") or None

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The message cannot be blank")
        return value.strip("\r\n")

    @field_validator("button_url")
    @classmethod
    def _button_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        # https only: no javascript:, data:, or plain-http links under our
        # brand. No whitespace or control characters anywhere in it.
        parts = urlsplit(value)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or re.search(r"\s", value)
            or _CONTROL.search(value)
        ):
            raise ValueError("The button link must be a full https:// address")
        return value

    @model_validator(mode="after")
    def _button_pair(self) -> "EmailContent":
        if bool(self.button_url) != bool(self.button_label):
            raise ValueError("A button needs both a label and a link")
        return self


class SendEmailRequest(EmailContent):
    to: list[EmailStr] = Field(min_length=1, max_length=MAX_RECIPIENTS)

    @field_validator("to")
    @classmethod
    def _to(cls, values: list[str]) -> list[str]:
        return [_plain_address(value) for value in values]


# Whether an admin's connected account can send repayment reminders now.
#   READY:           connected, sending on, the owner an active admin.
#   PAUSED:          connected, but its owner switched sending off.
#   NEEDS_RECONNECT: Google no longer accepts the token, or it belongs to
#                    another address than the owner's sign-in email.
#   NOT_CONNECTED:   no Google account connected (or disconnected since).
#   NOT_ADMIN:       the owner is no longer an active admin.
ReminderSenderStatus = Literal[
    "READY", "PAUSED", "NEEDS_RECONNECT", "NOT_CONNECTED", "NOT_ADMIN"
]


class ReminderSenderAccount(BaseModel):
    """An admin's connected account, as the reminder-sender picker shows it.
    Never the token."""

    user_id: UUID
    full_name: str | None = None
    gmail_address: str | None = None
    from_name: str | None = None
    status: ReminderSenderStatus


class ReminderSenderOut(BaseModel):
    """SYSTEM_ADMIN only. Whose connected Gmail account sends the automatic
    repayment reminders, and which accounts could."""

    # Null: none chosen, so no automatic reminder goes out.
    sender: ReminderSenderAccount | None = None
    # Active admins with a connected Google account, the chosen one included
    # whatever its state.
    candidates: list[ReminderSenderAccount]
    updated_at: datetime | None = None
    updated_by: UUID | None = None


class ReminderSenderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The admin whose connected account sends reminders. Null stops them.
    user_id: UUID | None


class RecipientOut(BaseModel):
    """A user an admin can add to the To list."""

    id: UUID
    full_name: str | None
    email: str
    role: str | None
    # The address is on a FundLok domain (service.FUNDLOK_EMAIL_DOMAINS).
    is_fundlok: bool


class EmailPreview(BaseModel):
    subject: str
    html: str


class EmailTestResult(BaseModel):
    sent_to: str
    last_verified_at: datetime


class InternalEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sent_by: UUID | None
    from_address: str
    recipients: list[str]
    template: str
    subject: str
    status: str
    error_code: str | None
    # Recipients whose copy did not go out (the rest did).
    undelivered: list[str]
    created_at: datetime
    sent_at: datetime | None


# --------------------------------------------------------------------------- #
# "Write with Claude" (docs/specs/admin/email-assistant.md)
# --------------------------------------------------------------------------- #


class EmailAssistantStatus(BaseModel):
    """Whether the composer shows "Write with Claude" to the signed-in admin."""

    enabled: bool


class EmailAssistantCurrent(BaseModel):
    """What is already in the form, so Claude can rework it. Plain text."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(default="", max_length=200)
    heading: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=10_000)
    button_label: str = Field(default="", max_length=60)


class EmailAssistantRequest(BaseModel):
    """The admin's instruction. Recipients are never part of it."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2_000)
    language: Literal["vi", "en"]
    template: ComposeTemplate
    current: EmailAssistantCurrent = Field(default_factory=EmailAssistantCurrent)

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Say what the email should cover")
        return value


class EmailAssistantDraft(BaseModel):
    """A draft for the composer: body_html goes into the editor as is (every
    piece of text in it is escaped), body_text is for showing it."""

    subject: str
    heading: str
    body_html: str
    body_text: str
    # "[deadline]"-style gaps Claude left instead of inventing a fact; the
    # composer refuses to send while any is still in the email.
    placeholders: list[str]
    # Figures in the draft that are not in the instruction or the old draft.
    unverified_numbers: list[str]
