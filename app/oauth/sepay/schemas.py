import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field


class SepayStatusOut(BaseModel):
    configured: bool
    connected: bool
    status: Literal["ACTIVE", "NEEDS_RECONNECT"] | None = None
    connected_at: dt.datetime | None = None
    scopes: list[str] = []


class SepayConnectOut(BaseModel):
    authorize_url: str


class SepayCallbackIn(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=4096)


class SepayBankAccountOut(BaseModel):
    # The balance (`accumulated`) is deliberately not part of this model.
    id: int
    label: str | None = None
    account_number: str
    account_holder_name: str | None = None
    bank_short_name: str | None = None
    bank_full_name: str | None = None
    bank_bin: str | None = None
    bank_logo_url: str | None = None
    active: bool = True
