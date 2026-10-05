from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.payments import payouts
from app.payments.idempotency import idempotent_request, require_idempotency_key
from app.payments.schemas import (
    DisbursementCreate,
    DistributionOut,
    PayoutInstructionOut,
    RepaymentCreate,
    RepaymentOut,
)
from app.payments.service import record_repayment
from app.users.models import Role, User
from app.utils.audit import append_audit
from app.utils.rbac import require_roles

router = APIRouter(prefix="/payments", tags=["payments"])

require_admin = require_roles(Role.ADMIN)

# Idempotency scopes: one per endpoint, the route template (path params go
# into the request fingerprint instead).
DISBURSEMENT_CREATE_SCOPE = "POST /payments/disbursements"
DISBURSEMENT_APPROVE_SCOPE = "POST /payments/disbursements/{instruction_id}/approve"
DISBURSEMENT_CANCEL_SCOPE = "POST /payments/disbursements/{instruction_id}/cancel"
REPAYMENT_SCOPE = "POST /payments/repayments"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/disbursements", response_model=PayoutInstructionOut, status_code=202)
async def create_disbursement(
    body: DisbursementCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Maker step: records the contract's single disbursement instruction as
    PENDING_APPROVAL. Nothing is sent to the bank until a different admin
    approves it."""
    async with idempotent_request(
        db,
        actor_id=current_user.id,
        scope=DISBURSEMENT_CREATE_SCOPE,
        key=idempotency_key,
        body=body.model_dump(mode="json"),
    ) as claim:
        if claim.replay:
            return claim.replay_response()
        instruction = await payouts.create_disbursement_instruction(
            db, contract_id=body.contract_id, beneficiary_id=body.beneficiary_id, maker_id=current_user.id
        )
        await db.refresh(instruction)
        out = PayoutInstructionOut.from_instruction(instruction)
        append_audit(
            db,
            entity_type="PAYOUT_INSTRUCTION",
            entity_id=instruction.id,
            action="CREATE",
            actor_id=current_user.id,
            after_state={"status": out.status, "amount": str(out.amount), "contract_id": str(body.contract_id)},
            ip_address=_client_ip(request),
        )
        await claim.complete(status_code=202, body=out.model_dump(mode="json"))
        await db.commit()
        return out


@router.post("/disbursements/{instruction_id}/approve", response_model=PayoutInstructionOut)
async def approve_disbursement(
    instruction_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Checker step: a different admin approves; the dispatcher then sends it."""
    async with idempotent_request(
        db,
        actor_id=current_user.id,
        scope=DISBURSEMENT_APPROVE_SCOPE,
        key=idempotency_key,
        path_params={"instruction_id": str(instruction_id)},
    ) as claim:
        if claim.replay:
            return claim.replay_response()
        instruction = await payouts.approve_instruction(db, instruction_id=instruction_id, checker_id=current_user.id)
        await db.refresh(instruction)
        out = PayoutInstructionOut.from_instruction(instruction)
        append_audit(
            db,
            entity_type="PAYOUT_INSTRUCTION",
            entity_id=instruction.id,
            action="APPROVE",
            actor_id=current_user.id,
            before_state={"status": "PENDING_APPROVAL"},
            after_state={"status": out.status},
            ip_address=_client_ip(request),
        )
        await claim.complete(status_code=200, body=out.model_dump(mode="json"))
        await db.commit()
        return out


@router.post("/disbursements/{instruction_id}/cancel", response_model=PayoutInstructionOut)
async def cancel_disbursement(
    instruction_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    idempotency_key: str = Depends(require_idempotency_key),
):
    """Withdraw an instruction that has not been sent yet."""
    async with idempotent_request(
        db,
        actor_id=current_user.id,
        scope=DISBURSEMENT_CANCEL_SCOPE,
        key=idempotency_key,
        path_params={"instruction_id": str(instruction_id)},
    ) as claim:
        if claim.replay:
            return claim.replay_response()
        instruction = await payouts.cancel_instruction(db, instruction_id=instruction_id, actor_id=current_user.id)
        await db.refresh(instruction)
        out = PayoutInstructionOut.from_instruction(instruction)
        append_audit(
            db,
            entity_type="PAYOUT_INSTRUCTION",
            entity_id=instruction.id,
            action="CANCEL",
            actor_id=current_user.id,
            after_state={"status": out.status},
            ip_address=_client_ip(request),
        )
        await claim.complete(status_code=200, body=out.model_dump(mode="json"))
        await db.commit()
        return out


@router.post("/repayments", response_model=RepaymentOut, status_code=201)
async def post_repayment(
    body: RepaymentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
    idempotency_key: str = Depends(require_idempotency_key),
):
    async with idempotent_request(
        db,
        actor_id=current_user.id,
        scope=REPAYMENT_SCOPE,
        key=idempotency_key,
        body=body.model_dump(mode="json"),
    ) as claim:
        if claim.replay:
            return claim.replay_response()
        # The ledger key is derived from the claim, not the client's header:
        # the ledger_transactions key space is shared by every event type, and
        # a client-chosen key could otherwise squat a "payout:<id>" key.
        rep, dists, _ = await record_repayment(
            db,
            contract_id=body.contract_id,
            amount=body.amount,
            paid_at=body.paid_at,
            reference=body.reference,
            idempotency_key=f"idem:{claim.record_id}",
            created_by=current_user.id,
        )
        append_audit(
            db,
            entity_type="LEDGER",
            entity_id=rep.id,
            action="REPAYMENT",
            actor_id=current_user.id,
            after_state={"amount": str(body.amount), "distributions": len(dists)},
            ip_address=_client_ip(request),
        )
        out = RepaymentOut(
            repayment_id=rep.id,
            balances=[DistributionOut(ledger_entry_id=d.id, amount=d.amount) for d in dists],
        )
        await claim.complete(status_code=201, body=out.model_dump(mode="json"))
        await db.commit()
        return out
