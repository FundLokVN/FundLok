import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# The templates an admin can compose with. Each maps to an exported React
# Email template in service.TEMPLATE_FILES.
ComposeTemplate = Literal["general", "announcement", "action_required"]

# Any control character, CR/LF included. Values that land in a mail header
# (subject, from name) or on a single line of the email must not carry one:
# a newline in a header would let the value add headers of its own (Bcc: …).
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Google shows an App Password as four groups of four letters. ASCII only:
# [A-Za-z0-9], not \w, which would also accept non-ASCII letters and digits.
_APP_PASSWORD = re.compile(r"[A-Za-z0-9]{16}")

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
    """The signed-in admin's own Gmail connection. Never the password itself:
    only whether one is stored."""

    configured: bool
    enabled: bool
    gmail_address: str | None = None
    from_name: str | None = None
    has_app_password: bool
    last_verified_at: datetime | None = None
    updated_at: datetime | None = None


class EmailSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gmail_address: EmailStr
    from_name: str = Field(min_length=1, max_length=80)
    # None or blank keeps the stored password. Spaces are dropped, so it can be
    # pasted exactly as Google displays it ("abcd efgh ijkl mnop").
    app_password: str | None = Field(default=None, max_length=64)
    enabled: bool

    @field_validator("gmail_address")
    @classmethod
    def _gmail_address(cls, value: str) -> str:
        return _plain_address(value)

    @field_validator("from_name")
    @classmethod
    def _from_name(cls, value: str) -> str:
        value = _single_line(value, "The sender name") or ""
        if not value:
            raise ValueError("The sender name cannot be blank")
        return value

    @field_validator("app_password")
    @classmethod
    def _app_password(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = "".join(value.split())
        if not value:
            return None
        if not _APP_PASSWORD.fullmatch(value):
            raise ValueError("Enter the 16-character app password from your Google account")
        return value

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
    to: list[EmailStr] = Field(min_length=1, max_length=20)

    @field_validator("to")
    @classmethod
    def _to(cls, values: list[str]) -> list[str]:
        return [_plain_address(value) for value in values]


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
    created_at: datetime
    sent_at: datetime | None
