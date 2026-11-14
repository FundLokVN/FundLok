"""Admin > Transactions > Service fees.

Each SME activation-fee invoice (Edward's `fee_invoices`, issued when a round
closes) with the investment package it is charged on: the round, its business,
and the investments whose confirmed total the 3% is taken on. Staff mark one
paid once the SME's transfer shows on FundLok's statement. The money side
(invoice PAID, the FEE_PAYMENT transfer, the ledger posting, the evidence log)
is Edward's `fees.mark_invoice_paid`; this adds the staff-facing read, the
audit row and the error mapping.

Until an invoice is paid the SME cannot open another funding round
(`fees.sme_has_unpaid_invoice`, SME_INVOICE_UNPAID).
"""

import datetime as dt
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.fee_notice.models import FeeInvoiceNotice
from app.admin.fee_schemas import (
    AdminFeeInvestmentOut,
    AdminFeeInvoiceOut,
    AdminFeeMarkPaidRequest,
    AdminRoundFeeOut,
    FeeInvoiceFilter,
)
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.fees import UNPAID, mark_invoice_paid
from app.direct_transfer.models import FeeInvoice, FundingWindow, Tranche
from app.lending.models import Project
from app.users.models import User
from app.utils.audit import append_audit

KIND = "SME_ACTIVATION"
# The investments a round's fee is charged on: the ones that became loans.
# ACTIVE is a tranche's last state, so this holds for a round of any age.
_CHARGED = "ACTIVE"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _is_overdue(invoice: FeeInvoice, now: dt.datetime) -> bool:
    # Computed rather than read from status: the job that would mark it
    # OVERDUE is not deployed.
    return invoice.status in UNPAID and invoice.due_at < now


def round_fee(invoice: FeeInvoice, now: dt.datetime) -> AdminRoundFeeOut:
    return AdminRoundFeeOut(
        id=invoice.id,
        status=invoice.status,
        total_vnd=invoice.total_vnd,
        due_at=invoice.due_at,
        is_overdue=_is_overdue(invoice, now),
    )


async def round_fees(db: AsyncSession, window_ids: list[UUID]) -> dict[UUID, FeeInvoice]:
    """Each round's activation invoice (one query), for the rounds table."""
    if not window_ids:
        return {}
    rows = (
        await db.execute(
            select(FeeInvoice).where(
                FeeInvoice.kind == KIND,
                FeeInvoice.window_id.in_(window_ids),
                FeeInvoice.status != "VOID",
            )
        )
    ).scalars().all()
    return {row.window_id: row for row in rows}


async def _out(
    db: AsyncSession,
    invoice: FeeInvoice,
    window: FundingWindow,
    project: Project,
    sme: User,
    now: dt.datetime,
) -> AdminFeeInvoiceOut:
    investments = (
        await db.execute(
            select(Tranche, User)
            .join(User, User.id == Tranche.investor_id)
            .where(Tranche.window_id == window.id, Tranche.status == _CHARGED)
            .order_by(Tranche.confirmed_at, Tranche.id)
        )
    ).all()
    notice = await db.get(FeeInvoiceNotice, invoice.id)
    return AdminFeeInvoiceOut(
        id=invoice.id,
        status=invoice.status,
        is_overdue=_is_overdue(invoice, now),
        invoice_number=invoice.invoice_number,
        reference_code=invoice.reference_code,
        base_vnd=invoice.base_vnd,
        fee_vnd=invoice.fee_vnd,
        vat_vnd=invoice.vat_vnd,
        late_fee_vnd=invoice.late_fee_vnd,
        total_vnd=invoice.total_vnd,
        issued_at=invoice.issued_at,
        due_at=invoice.due_at,
        paid_at=invoice.paid_at,
        paid_bank_txn_id=invoice.paid_bank_txn_id,
        window_id=window.id,
        round_status=window.status,
        project_id=project.id,
        project_name=project.legal_name,
        sme_user_id=sme.id,
        sme_email=sme.email,
        sme_full_name=sme.full_name,
        investments=[
            AdminFeeInvestmentOut(
                tranche_id=tranche.id,
                investor_name=investor.full_name,
                investor_email=investor.email,
                amount_vnd=tranche.amount_vnd,
                reference_code=tranche.reference_code,
                confirmed_at=tranche.confirmed_at,
            )
            for tranche, investor in investments
        ],
        email_status=notice.status if notice is not None else None,
        emailed_at=notice.sent_at if notice is not None else None,
    )


def _invoices_query():
    return (
        select(FeeInvoice, FundingWindow, Project, User)
        .join(FundingWindow, FundingWindow.id == FeeInvoice.window_id)
        .join(Project, Project.id == FundingWindow.project_id)
        .join(User, User.id == FeeInvoice.payer_user_id)
        .where(FeeInvoice.kind == KIND)
    )


async def list_fee_invoices(
    db: AsyncSession, *, status_filter: FeeInvoiceFilter, limit: int
) -> list[AdminFeeInvoiceOut]:
    """Unpaid first (soonest due first), then the most recently paid."""
    stmt = _invoices_query()
    if status_filter == "UNPAID":
        stmt = stmt.where(FeeInvoice.status.in_(UNPAID))
    elif status_filter == "PAID":
        stmt = stmt.where(FeeInvoice.status == "PAID")
    stmt = stmt.order_by(
        (FeeInvoice.status == "PAID").asc(),
        FeeInvoice.due_at.asc(),
        FeeInvoice.paid_at.desc().nulls_last(),
    ).limit(limit)
    now = _now()
    return [
        await _out(db, invoice, window, project, sme, now)
        for invoice, window, project, sme in (await db.execute(stmt)).all()
    ]


async def _one(db: AsyncSession, invoice_id: UUID) -> AdminFeeInvoiceOut:
    row = (
        await db.execute(
            _invoices_query()
            .where(FeeInvoice.id == invoice_id)
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    invoice, window, project, sme = row
    return await _out(db, invoice, window, project, sme, _now())


async def mark_paid(
    db: AsyncSession,
    invoice_id: UUID,
    body: AdminFeeMarkPaidRequest,
    *,
    actor: User,
    ip_address: str | None,
) -> AdminFeeInvoiceOut:
    """Record the SME's transfer and mark the invoice PAID (Edward's
    mark_invoice_paid). Pressing it again for a paid invoice changes nothing;
    a bank transaction already used for another invoice is refused."""
    # Locked for the transaction: mark_invoice_paid checks "already PAID?"
    # without a lock, so two staff pressing at once would both record a
    # payment and post the ledger twice. The second waits here, then sees
    # PAID (fresh, populate_existing) and changes nothing.
    invoice = (
        await db.execute(
            select(FeeInvoice)
            .where(FeeInvoice.id == invoice_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if invoice is None or invoice.kind != KIND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    before = invoice.status
    try:
        await mark_invoice_paid(
            db,
            invoice_id=invoice_id,
            bank_txn_id=body.bank_txn_id,
            reason=body.reason,
            staff_id=actor.id,
            now=_now(),
        )
    except DirectTransferError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code}
        ) from None
    if before != "PAID":
        append_audit(
            db,
            entity_type="FEE_INVOICE",
            entity_id=invoice_id,
            action="FEE_INVOICE_MARKED_PAID",
            actor_id=actor.id,
            before_state={"status": before},
            after_state={
                "status": "PAID",
                "bank_txn_id": body.bank_txn_id,
                "total_vnd": invoice.total_vnd,
                "reason": body.reason,
            },
            ip_address=ip_address,
        )
    await db.commit()
    return await _one(db, invoice_id)
