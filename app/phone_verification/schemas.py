import re
from typing import Literal

from pydantic import BaseModel, field_validator

from app.phone_verification.phone import canonical_vn_mobile

_SIX_DIGITS = re.compile(r"[0-9]{6}")


class PhoneCodeSendRequest(BaseModel):
    phone: str
    turnstile_token: str | None = None

    @field_validator("phone")
    @classmethod
    def vietnamese_mobile(cls, v: str) -> str:
        canonical = canonical_vn_mobile(v)
        if canonical is None:
            raise ValueError("phone must be a Vietnamese mobile number (0xxxxxxxxx or +84xxxxxxxxx)")
        return canonical


class PhoneCodeSentOut(BaseModel):
    # Canonical +84 form, so the page can show where the code went.
    phone: str
    # "zalo"; "log" only in development (PHONE_OTP_CHANNEL=log).
    channel: Literal["zalo", "log"]
    # Seconds until the code stops working, and until another may be requested.
    expires_in: int
    resend_in: int


class PhoneCodeVerifyRequest(BaseModel):
    code: str

    @field_validator("code", mode="before")
    @classmethod
    def six_digits(cls, v):
        v = str(v).strip()
        # Not str.isdigit(), which also accepts non-ASCII digits.
        if not _SIX_DIGITS.fullmatch(v):
            raise ValueError("code must be 6 digits")
        return v
