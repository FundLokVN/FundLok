from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.payments.models import PayoutInstruction


class DisbursementCreate(BaseModel):
    """The caller names the contract and a verified beneficiary only. The
    amount is derived from the contract and the destination from the
    registry; extra fields (e.g. a client-supplied `amount` or
    `bank_account`) are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    contract_id: UUID
    beneficiary_id: UUID


class PayoutInstructionOut(BaseModel):
    id: UUID
    client_request_id: str
    purpose: str
    contract_id: UUID | None
    amount: int
    currency: str
    status: str
    beneficiary_id: UUID
    beneficiary_account_masked: str
    created_by: UUID
    approved_by: UUID | None
    approved_at: datetime | None
    created_at: datetime

    @classmethod
    def from_instruction(cls, instruction: PayoutInstruction) -> "PayoutInstructionOut":
        return cls(
            id=instruction.id,
            client_request_id=instruction.client_request_id,
            purpose=instruction.purpose,
            contract_id=instruction.contract_id,
            amount=int(instruction.amount),
            currency=instruction.currency,
            status=instruction.status,
            beneficiary_id=instruction.beneficiary_id,
            beneficiary_account_masked=f"****{instruction.beneficiary_account_number[-4:]}",
            created_by=instruction.created_by,
            approved_by=instruction.approved_by,
            approved_at=instruction.approved_at,
            created_at=instruction.created_at,
        )


class RepaymentCreate(BaseModel):
    contract_id: UUID
    amount: int
    paid_at: datetime
    reference: str | None = None


class DistributionOut(BaseModel):
    ledger_entry_id: UUID
    amount: int


class RepaymentOut(BaseModel):
    repayment_id: UUID
    balances: list[DistributionOut]
