"""Ledger posting for the direct-transfer path (confirmed-transfer model, decision E2 / D-026).

Spec: docs/specs/direct_transfer/ledger-amendment.md. Thin layer over `app.ledger.service.post_transaction`
(unchanged). NO OMNIBUS_CASH leg is ever posted from here: the counterparty is the platform-level `EXTERNAL`
account ("the parties' own banks"). Each posting is its own idempotent ledger transaction with a stable key.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.models import FeeInvoice, RepaymentDay, Tranche
from app.ledger.models import LedgerAccount, LedgerTransaction
from app.ledger.service import (
    LedgerLeg,
    get_account_balance,
    get_or_create_account,
    post_transaction,
    resolve_custodial_account,
)


class LedgerPostingError(Exception):
    pass


async def _accounts(db: AsyncSession, contract_id: UUID):
    custodial = await resolve_custodial_account(db, contract_id=contract_id)
    external = await get_or_create_account(db, account_type="EXTERNAL", custodial_account_id=custodial.id)
    return custodial, external


async def post_tranche_claims(
    db: AsyncSession, *, tranche: Tranche, contract_id: UUID, sme_user_id: UUID, created_by: UUID | None = None
) -> list[LedgerTransaction]:
    """Claim legs when a tranche joins the loan (CONFIRMED -> ACTIVE). Two separate transactions so the shared
    EXTERNAL counterparty is touched once per transaction (see ledger-amendment.md rule 3)."""
    if tranche.status != "ACTIVE":
        raise LedgerPostingError("tranche must be ACTIVE")
    custodial, external = await _accounts(db, contract_id)
    lender = await get_or_create_account(
        db, account_type="LENDER", custodial_account_id=custodial.id, owner_user_id=tranche.investor_id, contract_id=contract_id
    )
    borrower = await get_or_create_account(
        db, account_type="BORROWER", custodial_account_id=custodial.id, owner_user_id=sme_user_id, contract_id=contract_id
    )
    amount = Decimal(tranche.amount_vnd)
    t1 = await post_transaction(
        db,
        event_type="FUNDING",
        legs=[LedgerLeg(external.id, lender.id, amount, "FUNDING", contract_id, tranche.reference_code)],
        idempotency_key=f"dt:claim:{tranche.id}:lender",
        reference=tranche.reference_code,
        created_by=created_by,
    )
    t2 = await post_transaction(
        db,
        event_type="DISBURSEMENT",
        legs=[LedgerLeg(external.id, borrower.id, amount, "DISBURSEMENT", contract_id, tranche.reference_code)],
        idempotency_key=f"dt:claim:{tranche.id}:borrower",
        reference=tranche.reference_code,
        created_by=created_by,
    )
    return [t1, t2]


async def post_repayment_day_settled(
    db: AsyncSession,
    *,
    repayment_day: RepaymentDay,
    tranche: Tranche,
    contract_id: UUID,
    sme_user_id: UUID,
    created_by: UUID | None = None,
) -> list[LedgerTransaction]:
    """Principal component only (interest/tax/fee obligations are not in the ledger yet, PROVISIONAL(Q-D9))."""
    if repayment_day.principal_vnd <= 0:
        return []
    custodial, external = await _accounts(db, contract_id)
    lender = await get_or_create_account(
        db, account_type="LENDER", custodial_account_id=custodial.id, owner_user_id=tranche.investor_id, contract_id=contract_id
    )
    borrower = await get_or_create_account(
        db, account_type="BORROWER", custodial_account_id=custodial.id, owner_user_id=sme_user_id, contract_id=contract_id
    )
    amount = Decimal(repayment_day.principal_vnd)
    ref = f"{tranche.reference_code}:{repayment_day.due_date.isoformat()}"
    t1 = await post_transaction(
        db,
        event_type="REPAYMENT",
        legs=[LedgerLeg(borrower.id, external.id, amount, "REPAYMENT", contract_id, ref)],
        idempotency_key=f"dt:repay:{repayment_day.id}:borrower",
        reference=ref,
        created_by=created_by,
    )
    t2 = await post_transaction(
        db,
        event_type="DISTRIBUTION",
        legs=[LedgerLeg(lender.id, external.id, amount, "DISTRIBUTION", contract_id, ref)],
        idempotency_key=f"dt:repay:{repayment_day.id}:lender",
        reference=ref,
        created_by=created_by,
    )
    return [t1, t2]


async def post_fee_paid(db: AsyncSession, *, invoice: FeeInvoice, created_by: UUID | None = None) -> LedgerTransaction | None:
    """Fee revenue (excluding VAT, which is owed to the tax authority) when an invoice is paid."""
    amount = invoice.fee_vnd + invoice.late_fee_vnd
    if amount <= 0:
        return None
    custodial, external = await _accounts(db, invoice.contract_id)
    revenue = await get_or_create_account(db, account_type="FL_REVENUE", custodial_account_id=custodial.id)
    return await post_transaction(
        db,
        event_type="FEE",
        legs=[LedgerLeg(external.id, revenue.id, Decimal(amount), "FEE", invoice.contract_id, invoice.invoice_number)],
        idempotency_key=f"dt:fee:{invoice.id}",
        reference=invoice.invoice_number,
        created_by=created_by,
    )


@dataclasses.dataclass(frozen=True)
class ClaimSummary:
    borrower_obligation_vnd: int
    lender_claims_vnd: dict[UUID, int]  # investor user id -> claim

    @property
    def lender_total_vnd(self) -> int:
        return sum(self.lender_claims_vnd.values())


async def contract_claim_summary(db: AsyncSession, *, contract_id: UUID) -> ClaimSummary:
    from sqlalchemy import select

    accounts = (
        await db.execute(
            select(LedgerAccount).where(
                LedgerAccount.contract_id == contract_id, LedgerAccount.account_type.in_(("LENDER", "BORROWER"))
            )
        )
    ).scalars().all()
    borrower = 0
    lenders: dict[UUID, int] = {}
    for account in accounts:
        balance = int(await get_account_balance(db, account.id))
        if account.account_type == "BORROWER":
            borrower += balance
        else:
            lenders[account.owner_user_id] = balance
    return ClaimSummary(borrower_obligation_vnd=borrower, lender_claims_vnd=lenders)
