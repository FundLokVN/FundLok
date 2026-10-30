"""Staff service layer for the exceptions and dispute queues (HANDOFF-04 B14).

Phat wires routers in `app/admin/` that call these functions and the decision functions they re-export
(`staff_enter_receipt_fields`, `decide_dispute`, `escalate_to_bank_trace`, `decide_payment_account`,
`mark_invoice_paid`, `match_day`, `cancel_window`). Every staff action requires a reason and records who/when.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.accounts import decide_payment_account  # noqa: F401  (re-export)
from app.direct_transfer.close import cancel_window  # noqa: F401
from app.direct_transfer.disputes import decide_dispute, escalate_to_bank_trace, list_disputes  # noqa: F401
from app.direct_transfer.evidence import timeline
from app.direct_transfer.fees import blocked_parties, mark_invoice_paid  # noqa: F401
from app.direct_transfer.models import Dispute, EvidenceFile, EvidenceLog, PaymentAccount, Tranche, Transfer
from app.direct_transfer.repayment import match_day  # noqa: F401
from app.direct_transfer.tranches import staff_enter_receipt_fields  # noqa: F401


def _timeline_row(row: EvidenceLog) -> dict[str, Any]:
    return {
        "event_type": row.event_type,
        "actor_id": row.actor_id,
        "actor_role": row.actor_role,
        "reason": row.reason,
        "payload": row.payload,
        "occurred_at": row.occurred_at,
    }


async def list_exceptions(db: AsyncSession) -> list[dict[str, Any]]:
    """The exceptions queue: receipts waiting for staff entry, mismatches, late receipts, and refund receipts
    waiting for entry. Each item carries the file key (the router mints a download URL), the extracted fields, the
    failure codes and the append-only timeline."""
    items: list[dict[str, Any]] = []

    waiting = (
        await db.execute(
            select(Transfer, Tranche)
            .join(Tranche, Tranche.id == Transfer.tranche_id)
            .where(Transfer.check_status == "AWAITING_FIELDS", Transfer.kind.in_(("DISBURSEMENT", "REFUND")), Transfer.evidence_file_id.is_not(None))
            .order_by(Transfer.created_at)
        )
    ).all()
    for transfer, tranche in waiting:
        file = await db.get(EvidenceFile, transfer.evidence_file_id)
        late = tranche.status == "UNDER_REVIEW" and tranche.review_cause == "LATE_RECEIPT"
        kind = "REFUND_AWAITING_FIELDS" if transfer.kind == "REFUND" else ("LATE_RECEIPT" if late else "AWAITING_FIELDS")
        items.append(
            {
                "kind": kind,
                "tranche_id": tranche.id,
                "transfer_id": transfer.id,
                "dispute_id": None,
                "amount_vnd": transfer.amount_vnd,
                "reference_code": tranche.reference_code,
                "failure_codes": list(transfer.failure_codes or []),
                "parsed_fields": transfer.parsed_fields,
                "file_key": file.storage_key if file else None,
                "timeline": [_timeline_row(r) for r in await timeline(db, entity_type="TRANSFER", entity_id=transfer.id)],
            }
        )

    mismatches = (
        await db.execute(
            select(Dispute)
            .where(Dispute.status != "DECIDED", Dispute.cause.in_(("RECEIPT_MISMATCH", "LATE_RECEIPT")), Dispute.subject_type == "TRANCHE")
            .order_by(Dispute.created_at)
        )
    ).scalars().all()
    for dispute in mismatches:
        tranche = await db.get(Tranche, dispute.tranche_id)
        transfer = (
            await db.execute(select(Transfer).where(Transfer.tranche_id == tranche.id, Transfer.kind == "DISBURSEMENT").order_by(Transfer.created_at))
        ).scalars().first()
        file = await db.get(EvidenceFile, transfer.evidence_file_id) if transfer and transfer.evidence_file_id else None
        if transfer is not None and transfer.check_status == "AWAITING_FIELDS" and dispute.cause == "LATE_RECEIPT":
            continue  # already listed above as LATE_RECEIPT awaiting entry
        items.append(
            {
                "kind": dispute.cause,
                "tranche_id": tranche.id,
                "transfer_id": transfer.id if transfer else None,
                "dispute_id": dispute.id,
                "amount_vnd": tranche.amount_vnd,
                "reference_code": tranche.reference_code,
                "failure_codes": list(transfer.failure_codes or []) if transfer else [],
                "parsed_fields": transfer.parsed_fields if transfer else None,
                "file_key": file.storage_key if file else None,
                "timeline": [_timeline_row(r) for r in await timeline(db, entity_type="TRANCHE", entity_id=tranche.id)],
            }
        )
    return items


async def dispute_queue(db: AsyncSession, *, only_open: bool = True) -> list[dict[str, Any]]:
    out = []
    for d in await list_disputes(db, only_open=only_open):
        out.append(
            {
                "id": d.id,
                "subject_type": d.subject_type,
                "subject_id": d.tranche_id or d.repayment_day_id,
                "cause": d.cause,
                "status": d.status,
                "required_next_party": d.required_next_party,
                "response_deadline": d.response_deadline,
                "outcome": d.outcome,
                "evidence": [
                    {"party": e.party, "kind": e.kind, "statement": e.statement, "file_id": e.file_id, "submitted_at": e.submitted_at}
                    for e in d.evidence
                ],
                "timeline": [_timeline_row(r) for r in await timeline(db, entity_type="DISPUTE", entity_id=d.id)],
            }
        )
    return out


async def pending_payment_accounts(db: AsyncSession) -> list[PaymentAccount]:
    """The staff review queue. With the name-match rule (accounts.py) this holds SME accounts whose name did not
    match the verified KYB name, or that have no approved KYB. Each carries `name_match` and `expected_name`."""
    from app.direct_transfer.accounts import annotate_name_match

    accounts = list((await db.execute(select(PaymentAccount).where(PaymentAccount.status == "PENDING").order_by(PaymentAccount.created_at))).scalars())
    await annotate_name_match(db, accounts)
    return accounts
