"""Admin > Transactions > Service fees: the SME activation-fee invoices and
the investment package each one is charged on (app/admin/fees.py).

FundLok charges the SME one platform fee per funding round: 3% of the money
the investors confirmed sending, plus VAT, paid separately into FundLok's own
account (ADR-009, D-023). There is no bank connection, so staff mark an
invoice paid once its transfer shows on FundLok's statement.
"""
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

FeeInvoiceStatus = Literal["ISSUED", "PAID", "OVERDUE", "VOID"]
FeeEmailStatus = Literal["SENDING", "SENT", "FAILED"]
# The list's filter: UNPAID is ISSUED and OVERDUE.
FeeInvoiceFilter = Literal["UNPAID", "PAID", "ALL"]

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class AdminRoundFeeOut(BaseModel):
    """A round's service fee, as the rounds table shows it."""

    id: UUID
    status: FeeInvoiceStatus
    total_vnd: int
    due_at: datetime
    # Unpaid past its due date. Computed: the job that would set OVERDUE is
    # not deployed.
    is_overdue: bool


class AdminFeeInvestmentOut(BaseModel):
    """One investment in the package the fee is charged on."""

    tranche_id: UUID
    investor_name: str | None = None
    investor_email: str
    amount_vnd: int
    reference_code: str
    confirmed_at: datetime | None = None


class AdminFeeInvoiceOut(BaseModel):
    id: UUID
    status: FeeInvoiceStatus
    is_overdue: bool
    invoice_number: str
    # The memo the SME puts on the transfer, to match it on the statement.
    reference_code: str
    base_vnd: int
    fee_vnd: int
    vat_vnd: int
    late_fee_vnd: int
    total_vnd: int
    issued_at: datetime
    due_at: datetime
    paid_at: datetime | None = None
    paid_bank_txn_id: str | None = None
    # The investment package: the round, its business, and the investments
    # whose confirmed total (base_vnd) the 3% is charged on.
    window_id: UUID
    round_status: str
    project_id: UUID
    project_name: str
    sme_user_id: UUID
    sme_email: str
    sme_full_name: str | None = None
    investments: list[AdminFeeInvestmentOut]
    # Whether the invoice email reached the SME (app/admin/fee_notice).
    email_status: FeeEmailStatus | None = None
    emailed_at: datetime | None = None


class AdminFeeMarkPaidRequest(BaseModel):
    """The SME's transfer as it shows on FundLok's bank statement."""

    model_config = ConfigDict(extra="forbid")

    # The bank's transaction ID. One transaction pays one invoice.
    bank_txn_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("bank_txn_id")
    @classmethod
    def _txn(cls, value: str) -> str:
        value = value.strip()
        if not value or _CONTROL.search(value):
            raise ValueError("The bank transaction ID must be a single line")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A reason is required")
        return value
