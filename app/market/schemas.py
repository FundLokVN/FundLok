import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.direct_transfer.schemas import TrancheOut


class ListingCreate(BaseModel):
    contract_id: UUID
    # Same missing-bound class as OrderCreate.amount below: the service checks
    # `min_ticket <= 0` but never bounded target_amount, so a zero or negative
    # target produced a listing no order could ever fill (remaining <= 0).
    target_amount: int = Field(gt=0)
    min_ticket: int = Field(gt=0)


class ListingOut(BaseModel):
    id: UUID
    contract_id: UUID
    target_amount: int
    min_ticket: int | None
    status: str
    funded_amount: int

    model_config = {"from_attributes": True}


class OrderCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # gt=0 is load-bearing, not decoration. The service's lower bound is
    # `amount < min_ticket`, which is skipped when a listing has no min_ticket
    # (the column is nullable), and `amount > remaining` cannot catch a negative
    # — so without this a negative order would be accepted, decrementing the
    # listing's funded_amount and writing a FILLED order for negative money.
    amount: int = Field(gt=0)
    ack_risk_disclosure: bool = Field(default=False, alias="ackRiskDisclosure")


class OrderOut(BaseModel):
    id: UUID
    listing_id: UUID
    amount: int
    status: str

    model_config = {"from_attributes": True}

class ReservationCreate(BaseModel):
    """An investor's ticket on a funding window: the amount, and their agreement to the investing terms."""

    # Integer VND (CLAUDE.md). The bounds are the window's (minimum ticket, capacity left): reserve_ticket checks them.
    amount_vnd: int = Field(gt=0)
    # The risk disclosure and the investing terms are one agreement in the UI; both must be accepted.
    ack_risk_disclosure: bool = False
    # funding.INVESTOR_TERMS_VERSION of the text the investor read (409 TERMS_OUTDATED otherwise).
    terms_version: str = Field(min_length=1, max_length=40)
    # The investor's account the SME repays to; their latest approved one when omitted.
    payment_account_id: UUID | None = None


class InvestorTrancheOut(TrancheOut):
    """An investor's own ticket. Adds when they uploaded their transfer receipt: null until they do, so the page
    knows whether to ask for one."""

    receipt_submitted_at: dt.datetime | None = None
