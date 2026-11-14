from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConsentStatusOut(BaseModel):
    # The Terms version the platform asks for now.
    version: str
    # True until the user has accepted all three documents of `version`.
    # Always false for admins (FundLok staff are not platform users).
    required: bool
    # When the current version was accepted.
    accepted_at: datetime | None = None


class ConsentCodeOut(BaseModel):
    # The account's address with most of the name hidden, e.g. "ph***@fundlok.com".
    sent_to: str
    expires_in_seconds: int
    resend_after_seconds: int


class ConsentAcceptRequest(BaseModel):
    """The three tick boxes, each its own field so none can be implied by
    another, and the emailed code. A box sent as false is a 422."""

    model_config = ConfigDict(extra="forbid")

    # Must equal documents.CURRENT_VERSION: a page showing an older text
    # cannot accept a newer one (409 CONSENT_VERSION_CHANGED).
    version: str = Field(min_length=1, max_length=40)
    terms: Literal[True]
    data_processing: Literal[True]
    data_sharing_tpv: Literal[True]
    code: str = Field(pattern=r"^\d{6}$")
