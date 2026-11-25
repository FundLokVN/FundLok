"""Fees: SME activation invoice, investor service accrual, blocks, commission flag (HANDOFF-04 B12).

Spec: docs/specs/direct_transfer/fees.md. Fees are LIVE by default (decision E1 / D-023, supersedes S6): the
unpaid-invoice blocks are on unless `params.fees_enabled` is False. A fee is NEVER netted from principal or from
any repayment row; it is invoiced separately and paid into FundLok's own revenue account (not custody).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import ICT
from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, conflict, not_found
from app.direct_transfer.evidence import log_event
from app.direct_transfer.ledger_posting import post_fee_paid
from app.direct_transfer.models import (
    Dispute,
    FeeInvoice,
    FundingWindow,
    InvestorFeeAccrual,
    RepaymentDay,
    Tranche,
    Transfer,
)
from app.direct_transfer.refs import unique_reference_code
from app.direct_transfer.vietqr import build_vietqr_payload

UNPAID = ("ISSUED", "OVERDUE")


def round_half_up_div(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


# --------------------------------------------------------------------------- #
# Pure formulas (golden numbers: 100M -> 3,000,000; 10M x 4 months -> 83,333; 40M x 2 months -> 166,667)
# --------------------------------------------------------------------------- #
def sme_fee_parts(confirmed_total_vnd: int, params: DirectTransferParams | None = None) -> tuple[int, int, int]:
    """(fee, vat, total) for the SME activation fee. Fee = 3% of confirmed principal, VAT on top (PROVISIONAL(Q-D10))."""
    p = resolve(params)
    fee = round_half_up_div(confirmed_total_vnd * p.sme_fee_bps, 10_000)
    vat = round_half_up_div(fee * p.vat_bps, 10_000)
    return fee, vat, fee + vat


def investor_fee_total(amount_vnd: int, term_months: int, params: DirectTransferParams | None = None) -> int:
    """2.5%/yr fixed at signing: F = P * 2.5% * M / 12, to the nearest dong (40M x 2 months -> 166,667)."""
    p = resolve(params)
    return round_half_up_div(amount_vnd * p.investor_fee_bps_per_year * term_months, 10_000 * 12)


def investor_fee_for_month(fee_total: int, charged: int, received: int, owed: int) -> int:
    """(F - fee already charged) x instalments received in the month / balance owed at the start of the month (or
    at disbursement if later), to the nearest dong. Collecting the whole balance charges exactly what is left of F."""
    return round_half_up_div((fee_total - charged) * received, owed)


# --------------------------------------------------------------------------- #
# Invoice provider interface (no vendor, I-13)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class FeeInvoiceDraft:
    kind: str
    payer_user_id: UUID
    base_vnd: int
    fee_vnd: int
    vat_vnd: int
    reference_code: str
    issued_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class ProviderResult:
    provider: str
    invoice_number: str
    provider_ref: str | None = None


class InvoiceProvider(Protocol):
    async def issue(self, *, invoice: FeeInvoiceDraft) -> ProviderResult: ...


class ManualInvoiceProvider:
    """Assigns an internal number; staff issue the real VAT e-invoice out of band."""

    provider = "MANUAL"

    async def issue(self, *, invoice: FeeInvoiceDraft) -> ProviderResult:
        return ProviderResult(self.provider, f"FL-{invoice.issued_at.astimezone(ICT).year}-{invoice.reference_code}")


def invoice_vietqr(invoice: FeeInvoice, params: DirectTransferParams | None = None) -> str | None:
    p = resolve(params)
    if not (p.fee_account_bin and p.fee_account_number):
        return None
    total = invoice.fee_vnd + invoice.vat_vnd + invoice.late_fee_vnd
    return build_vietqr_payload(
        bank_bin=p.fee_account_bin, account_number=p.fee_account_number, amount_vnd=total, memo=invoice.reference_code
    )


def invoice_out(invoice: FeeInvoice, params: DirectTransferParams | None = None):
    """The invoice as the API returns it, carrying the payment QR (None when the fee account is not configured)."""
    from app.direct_transfer.schemas import FeeInvoiceOut

    out = FeeInvoiceOut.model_validate(invoice)
    return out.model_copy(update={"vietqr_payload": invoice_vietqr(invoice, params)})


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #
async def _has_overdue_invoice(db: AsyncSession, user_id: UUID, kind: str, now: dt.datetime) -> bool:
    """An unpaid invoice of `kind` whose 7 days (`due_at`) are over at `now`. Inside them nothing is blocked (D-047):
    the block starts on day 8, not on the issue date. Time is injected, never read from the wall clock."""
    row = (
        await db.execute(
            select(FeeInvoice.id).where(
                FeeInvoice.payer_user_id == user_id,
                FeeInvoice.kind == kind,
                FeeInvoice.status.in_(UNPAID),
                FeeInvoice.due_at < now,
            )
        )
    ).first()
    return row is not None


async def sme_has_unpaid_invoice(
    db: AsyncSession, user_id: UUID, params: DirectTransferParams | None = None, *, now: dt.datetime
) -> bool:
    """Blocks the SME's NEXT listing, and only after the 7 days from issue (D-047, Appendix M 5.7)."""
    if not resolve(params).fees_enabled:
        return False
    return await _has_overdue_invoice(db, user_id, "SME_ACTIVATION", now)


async def investor_has_unpaid_invoice(
    db: AsyncSession, user_id: UUID, params: DirectTransferParams | None = None, *, now: dt.datetime
) -> bool:
    """Blocks NEW commitments only, and only after the 7 days from issue (D-047, Appendix M 8.3)."""
    if not resolve(params).fees_enabled:
        return False
    return await _has_overdue_invoice(db, user_id, "INVESTOR_SERVICE", now)


async def blocked_parties(db: AsyncSession) -> dict[str, list[dict]]:
    rows = (
        await db.execute(select(FeeInvoice).where(FeeInvoice.status.in_(UNPAID)).order_by(FeeInvoice.issued_at))
    ).scalars().all()
    out: dict[str, dict[UUID, list[str]]] = {"smes": {}, "investors": {}}
    for inv in rows:
        bucket = out["smes"] if inv.kind == "SME_ACTIVATION" else out["investors"]
        bucket.setdefault(inv.payer_user_id, []).append(str(inv.id))
    return {
        "smes": [{"user_id": str(u), "invoice_ids": ids} for u, ids in out["smes"].items()],
        "investors": [{"user_id": str(u), "invoice_ids": ids} for u, ids in out["investors"].items()],
    }


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #
async def _issue(
    db: AsyncSession,
    *,
    kind: str,
    window: FundingWindow,
    payer_user_id: UUID,
    base_vnd: int,
    fee_vnd: int,
    vat_vnd: int,
    now: dt.datetime,
    due_days: int,
    provider: InvoiceProvider,
    period_month: dt.date | None = None,
) -> FeeInvoice:
    reference = await unique_reference_code(db)
    draft = FeeInvoiceDraft(kind, payer_user_id, base_vnd, fee_vnd, vat_vnd, reference, now)
    result = await provider.issue(invoice=draft)
    invoice = FeeInvoice(
        kind=kind,
        window_id=window.id,
        contract_id=window.contract_id,
        payer_user_id=payer_user_id,
        period_month=period_month,
        base_vnd=base_vnd,
        fee_vnd=fee_vnd,
        vat_vnd=vat_vnd,
        late_fee_vnd=0,
        total_vnd=fee_vnd + vat_vnd,
        status="ISSUED",
        invoice_number=result.invoice_number,
        provider=result.provider,
        provider_ref=result.provider_ref,
        reference_code=reference,
        issued_at=now,
        due_at=now + dt.timedelta(days=due_days),
    )
    db.add(invoice)
    await db.flush()
    await log_event(
        db,
        entity_type="FEE_INVOICE",
        entity_id=invoice.id,
        event_type="FEE_INVOICE_EVENT",
        now=now,
        payload={"event": "ISSUED", "kind": kind, "fee_vnd": fee_vnd, "vat_vnd": vat_vnd, "base_vnd": base_vnd},
    )
    return invoice


async def issue_sme_activation_invoice(
    db: AsyncSession,
    *,
    window: FundingWindow,
    confirmed_total_vnd: int,
    now: dt.datetime,
    provider: InvoiceProvider | None = None,
    params: DirectTransferParams | None = None,
) -> FeeInvoice | None:
    """Issued when the loan forms (never when the window FAILED: no fee below 50%). Never netted from principal."""
    p = resolve(params)
    if not p.fees_enabled:
        return None
    fee, vat, _ = sme_fee_parts(confirmed_total_vnd, p)
    return await _issue(
        db,
        kind="SME_ACTIVATION",
        window=window,
        payer_user_id=window.sme_user_id,
        base_vnd=confirmed_total_vnd,
        fee_vnd=fee,
        vat_vnd=vat,
        now=now,
        due_days=p.sme_invoice_due_days,
        provider=provider or ManualInvoiceProvider(),
    )


def _first_of_month(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _next_month(d: dt.date) -> dt.date:
    return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


async def accrue_investor_fees(db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams | None = None) -> int:
    """Monthly accrual, pro rata to the amount actually received (settled rows). Idempotent by
    UNIQUE(tranche_id, period_month). Returns the number of accrual rows created."""
    p = resolve(params)
    if not p.fees_enabled:
        return 0
    today = ict_date(now)
    this_month = _first_of_month(today)
    tranches = (
        await db.execute(select(Tranche).where(Tranche.status == "ACTIVE", Tranche.investor_fee_total_vnd > 0))
    ).scalars().all()
    created = 0
    for tranche in tranches:
        rows = (await db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tranche.id))).scalars().all()
        if not rows:
            continue
        scheduled_total = sum(r.gross_vnd for r in rows)
        months = sorted({_first_of_month(r.due_date) for r in rows})
        last_month = months[-1]
        all_settled = all(r.settled_at is not None for r in rows)
        existing = {
            a.period_month: a
            for a in (
                await db.execute(select(InvestorFeeAccrual).where(InvestorFeeAccrual.tranche_id == tranche.id))
            ).scalars()
        }
        accrued_so_far = sum(a.fee_vnd for a in existing.values())
        for month in months:
            if month in existing or month >= this_month:
                continue
            month_rows = [r for r in rows if _first_of_month(r.due_date) == month]
            # A month is accruable when ended and every row in it is resolved (settled, reported missed, or LATE: a
            # late day is not received, so it counts for nothing until it settles; the final true-up picks it up).
            if not all(r.settled_at is not None or r.status in ("REPORTED_MISSED", "LATE") for r in month_rows):
                continue
            received = sum(r.gross_vnd for r in month_rows if r.settled_at is not None)
            if received <= 0:
                continue
            owed = scheduled_total - sum(a.received_gross_vnd for a in existing.values() if a.period_month < month)
            fee = investor_fee_for_month(tranche.investor_fee_total_vnd, accrued_so_far, received, owed)
            if all_settled and month == last_month:
                fee = tranche.investor_fee_total_vnd - accrued_so_far
            fee = max(0, fee)
            accrual = InvestorFeeAccrual(
                tranche_id=tranche.id,
                investor_id=tranche.investor_id,
                period_month=month,
                received_gross_vnd=received,
                fee_vnd=fee,
            )
            db.add(accrual)
            existing[month] = accrual
            accrued_so_far += fee
            created += 1
        # Late days that settle after every due month has its accrual: nothing above bills them, so they are billed
        # for the month they settle in, once it has ended (late-status.md rule 6), and never in a due month's row.
        # Everything settled: the fee is completed to its total; days still unpaid: pro rata, as in any month.
        settled = [r for r in rows if r.settled_at is not None]
        if last_month in existing and settled:
            counted = sum(a.received_gross_vnd for a in existing.values())
            uncounted = sum(r.gross_vnd for r in settled) - counted
            month = max(_first_of_month(ict_date(max(r.settled_at for r in settled))), _next_month(last_month))
            if uncounted > 0 and month < this_month and month not in existing:
                if all_settled:
                    fee = tranche.investor_fee_total_vnd - accrued_so_far
                else:
                    fee = investor_fee_for_month(
                        tranche.investor_fee_total_vnd, accrued_so_far, uncounted, scheduled_total - counted
                    )
                if fee > 0:
                    db.add(
                        InvestorFeeAccrual(
                            tranche_id=tranche.id,
                            investor_id=tranche.investor_id,
                            period_month=month,
                            received_gross_vnd=uncounted,
                            fee_vnd=fee,
                        )
                    )
                    created += 1
    await db.flush()
    return created


async def issue_investor_invoices(
    db: AsyncSession,
    *,
    now: dt.datetime,
    provider: InvoiceProvider | None = None,
    params: DirectTransferParams | None = None,
) -> list[FeeInvoice]:
    """One INVESTOR_SERVICE invoice per (investor, month, window) from un-invoiced accruals."""
    p = resolve(params)
    if not p.fees_enabled:
        return []
    provider = provider or ManualInvoiceProvider()
    accruals = (
        await db.execute(
            select(InvestorFeeAccrual).where(InvestorFeeAccrual.invoice_id.is_(None)).order_by(InvestorFeeAccrual.period_month)
        )
    ).scalars().all()
    groups: dict[tuple[UUID, dt.date, UUID], list[InvestorFeeAccrual]] = {}
    for accrual in accruals:
        tranche = await db.get(Tranche, accrual.tranche_id)
        groups.setdefault((accrual.investor_id, accrual.period_month, tranche.window_id), []).append(accrual)
    invoices: list[FeeInvoice] = []
    for (investor_id, month, window_id), items in groups.items():
        fee = sum(a.fee_vnd for a in items)
        if fee <= 0:
            continue
        window = await db.get(FundingWindow, window_id)
        vat = round_half_up_div(fee * p.vat_bps, 10_000)
        invoice = await _issue(
            db,
            kind="INVESTOR_SERVICE",
            window=window,
            payer_user_id=investor_id,
            base_vnd=sum(a.received_gross_vnd for a in items),
            fee_vnd=fee,
            vat_vnd=vat,
            now=now,
            due_days=p.investor_invoice_due_days,
            provider=provider,
            period_month=month,
        )
        for a in items:
            a.invoice_id = invoice.id
        invoices.append(invoice)
    await db.flush()
    return invoices


# --------------------------------------------------------------------------- #
# Overdue, late fee, payment
# --------------------------------------------------------------------------- #
def late_fee_for(invoice: FeeInvoice, now: dt.datetime, params: DirectTransferParams) -> int:
    if params.late_fee_bps_per_day <= 0 or now <= invoice.due_at:
        return 0
    days = (now.astimezone(ICT).date() - invoice.due_at.astimezone(ICT).date()).days
    return max(0, invoice.fee_vnd * params.late_fee_bps_per_day * days // 10_000)


async def mark_overdue_invoices(db: AsyncSession, *, now: dt.datetime, params: DirectTransferParams | None = None) -> int:
    p = resolve(params)
    rows = (
        await db.execute(select(FeeInvoice).where(FeeInvoice.status.in_(UNPAID), FeeInvoice.due_at < now))
    ).scalars().all()
    changed = 0
    for invoice in rows:
        late = late_fee_for(invoice, now, p)
        newly_overdue = invoice.status == "ISSUED"
        if newly_overdue:
            invoice.status = "OVERDUE"
        if late != invoice.late_fee_vnd:
            invoice.late_fee_vnd = late
            invoice.total_vnd = invoice.fee_vnd + invoice.vat_vnd + late
        if newly_overdue or late != 0:
            await log_event(
                db,
                entity_type="FEE_INVOICE",
                entity_id=invoice.id,
                event_type="FEE_INVOICE_EVENT",
                now=now,
                payload={"event": "OVERDUE", "late_fee_vnd": invoice.late_fee_vnd},
            )
            changed += 1 if newly_overdue else 0
    await db.flush()
    return changed


async def mark_invoice_paid(
    db: AsyncSession,
    *,
    invoice_id: UUID,
    bank_txn_id: str,
    reason: str,
    staff_id: UUID,
    now: dt.datetime,
    params: DirectTransferParams | None = None,
) -> FeeInvoice:
    p = resolve(params)
    if not reason or not reason.strip():
        raise DirectTransferError(422, "A reason is required", "REASON_REQUIRED")
    invoice = await db.get(FeeInvoice, invoice_id)
    if invoice is None:
        raise not_found("Invoice not found")
    if invoice.status == "PAID":
        return invoice  # idempotent replay
    if invoice.status == "VOID":
        raise conflict("INVOICE_VOID", "This invoice is void")
    dup = (
        await db.execute(select(FeeInvoice.id).where(FeeInvoice.paid_bank_txn_id == bank_txn_id, FeeInvoice.id != invoice.id))
    ).first()
    if dup is not None:
        raise conflict("DUPLICATE_BANK_TXN", "This bank transaction was already used")
    late = late_fee_for(invoice, now, p)
    invoice.late_fee_vnd = late
    invoice.total_vnd = invoice.fee_vnd + invoice.vat_vnd + late
    invoice.status = "PAID"
    invoice.paid_at = now
    invoice.paid_bank_txn_id = bank_txn_id
    db.add(
        Transfer(
            kind="FEE_PAYMENT",
            fee_invoice_id=invoice.id,
            amount_vnd=invoice.total_vnd,
            reference=invoice.reference_code,
            rail="MANUAL",
            confirmation_level="CONFIRMED",
            check_status="MATCHED",
            bank_txn_id=bank_txn_id,
            transferred_at=now,
            reported_by=staff_id,
        )
    )
    await db.flush()
    await post_fee_paid(db, invoice=invoice, created_by=staff_id)
    await log_event(
        db,
        entity_type="FEE_INVOICE",
        entity_id=invoice.id,
        event_type="FEE_INVOICE_EVENT",
        now=now,
        actor_id=staff_id,
        actor_role="ADMIN",
        reason=reason,
        payload={"event": "PAID", "bank_txn_id": bank_txn_id, "total_vnd": invoice.total_vnd},
    )
    return invoice


async def list_invoices(db: AsyncSession, *, user_id: UUID) -> list[FeeInvoice]:
    result = await db.execute(select(FeeInvoice).where(FeeInvoice.payer_user_id == user_id).order_by(FeeInvoice.issued_at.desc()))
    return list(result.scalars())


# --------------------------------------------------------------------------- #
# Commission eligibility (D-024): only settled tranches; nothing here pays commission.
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class CommissionEligibility:
    eligible: bool
    settled_at: dt.datetime | None


async def commission_eligible(db: AsyncSession, *, tranche_id: UUID) -> CommissionEligibility:
    tranche = await db.get(Tranche, tranche_id)
    if tranche is None or tranche.status != "ACTIVE":
        return CommissionEligibility(False, None)
    rows = (await db.execute(select(RepaymentDay).where(RepaymentDay.tranche_id == tranche_id))).scalars().all()
    if not rows or any(r.settled_at is None for r in rows):
        return CommissionEligibility(False, None)
    open_dispute = (
        await db.execute(select(func.count()).select_from(Dispute).where(Dispute.tranche_id == tranche_id, Dispute.status != "DECIDED"))
    ).scalar_one()
    if open_dispute:
        return CommissionEligibility(False, None)
    return CommissionEligibility(True, max(r.settled_at for r in rows))
