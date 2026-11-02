"""The admin Transactions tab: the funding rounds taking investment, and every investor transfer with the receipt
checks staff do by hand in the MVP (ADR-009: no bank feed, so a receipt is read by a person).

The rules are Edward's (`app/direct_transfer/admin.py` re-exports `staff_enter_receipt_fields` and
`decide_dispute` for these routes). This module lists, adds the who/why to the audit log, and maps errors.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    ActorOut,
    AdminFundingActivityOut,
    AdminFundingRoundOut,
    AdminInvestorTransferOut,
    AdminReceiptFieldsRequest,
    AdminReceiptUrlResponse,
    AdminTransferDisputeOut,
    AdminTransferExpectedOut,
    AdminTransferReceiptOut,
    Page,
)
from app.direct_transfer.admin import decide_dispute, staff_enter_receipt_fields
from app.direct_transfer.close import advance_window, form_investment_loan
from app.direct_transfer.config import resolve
from app.direct_transfer.disputes import open_tranche_dispute
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import Dispute, EvidenceFile, FundingWindow, PaymentAccount, Tranche, Transfer
from app.direct_transfer.receipts import ReceiptFields
from app.direct_transfer.schemas import FundingWindowOut
from app.direct_transfer.tranche_state import transition_tranche
from app.direct_transfer.tranches import resolve_kyc_name
from app.direct_transfer.windows import get_window_progress, lock_window
from app.lending.models import AuditLog, Project
from app.market.emails import receipt_notice
from app.market.funding import expire_lapsed_holds
from app.notifications import realtime
from app.notifications.models import EVENT_INVESTMENT_TRANSFER_APPROVED
from app.notifications.service import create_notification
from app.users.models import User
from app.utils import r2
from app.utils.audit import append_audit

# Rounds still taking or settling investment: the ones the tab opens on.
OPEN_ROUND_STATUSES = ("OPEN", "FULL", "CLOSING")

# How the transfer list is filtered. NEEDS_ACTION is the queue: a receipt waiting to be read, or a ticket under
# review waiting for a decision. The others group the ticket states.
TRANSFER_FILTERS = ("ALL", "NEEDS_ACTION", "AWAITING_TRANSFER", "RECEIVED", "ENDED")
_FILTER_STATUSES = {
    "AWAITING_TRANSFER": ("RESERVED", "SIGNED"),
    "RECEIVED": ("ATTESTED", "UNDER_REVIEW", "CONFIRMED", "ACTIVE"),
    "ENDED": ("EXPIRED", "VOID"),
}

RECEIPT_URL_TTL_SECONDS = 600


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _http(exc: DirectTransferError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers={"X-Error-Code": exc.code})


def _receipt_waiting():
    return exists().where(
        Transfer.tranche_id == Tranche.id,
        Transfer.kind == "DISBURSEMENT",
        Transfer.check_status == "AWAITING_FIELDS",
        Transfer.evidence_file_id.is_not(None),
    )


def _open_dispute():
    return exists().where(
        Dispute.tranche_id == Tranche.id, Dispute.subject_type == "TRANCHE", Dispute.status != "DECIDED"
    )


def _needs_action():
    # A ticket left ATTESTED waits on staff too: approval now means received, so staff settle it.
    return or_(_receipt_waiting(), _open_dispute(), Tranche.status == "ATTESTED")


# --------------------------------------------------------------------------- #
# rounds
# --------------------------------------------------------------------------- #
async def list_rounds(db: AsyncSession, *, open_only: bool) -> list[AdminFundingRoundOut]:
    """Funding rounds, closing soonest first, each with its progress and how many of its tickets need staff."""
    now = _now()
    stmt = (
        select(FundingWindow, Project.legal_name, User)
        .join(Project, Project.id == FundingWindow.project_id)
        .join(User, User.id == FundingWindow.sme_user_id)
        .order_by(FundingWindow.window_end, FundingWindow.id)
    )
    if open_only:
        stmt = stmt.where(FundingWindow.status.in_(OPEN_ROUND_STATUSES))
    rows = (await db.execute(stmt)).all()

    # Lapsed holds are released first, so the totals are what an investor would see.
    released = 0
    for window, _, _ in rows:
        if window.status in OPEN_ROUND_STATUSES:
            released += await expire_lapsed_holds(db, window_id=window.id, now=now)
    if released:
        await db.commit()

    out = []
    for window, project_name, sme in rows:
        progress = await get_window_progress(db, listing_id=window.listing_id, now=now)
        needs_action = (
            await db.execute(
                select(func.count()).select_from(Tranche).where(Tranche.window_id == window.id, _needs_action())
            )
        ).scalar_one()
        out.append(
            AdminFundingRoundOut.model_validate(
                {
                    **FundingWindowOut.model_validate(progress).model_dump(),
                    "project_id": window.project_id,
                    "project_name": project_name,
                    "sme_email": sme.email,
                    "sme_full_name": sme.full_name,
                    "needs_action": int(needs_action),
                }
            )
        )
    return out


async def advance_round(
    db: AsyncSession, window_id: UUID, *, actor: User, ip_address: str | None
) -> AdminFundingRoundOut:
    """Do whatever is due on a round, now: confirm approved tickets whose dispute window lapsed, start the close
    (day 14, full with nothing pending, or the business asked to close early), and once nothing is pending form the
    loan: the facility, each investor's holding and the daily repayment days (Edward's advance_window, which the
    undeployed worker would run every 10 minutes). Safe to press again: it only does what is due."""
    window = await db.get(FundingWindow, window_id)
    if window is None:
        raise HTTPException(status_code=404, detail="Round not found")
    before = window.status
    now = _now()
    try:
        # Approved investments that are not repaying yet (approved before repayment started at approval) start now.
        if resolve(None).repay_from_approval and window.status in OPEN_ROUND_STATUSES:
            waiting = (
                await db.execute(select(Tranche).where(Tranche.window_id == window_id, Tranche.status == "CONFIRMED"))
            ).scalars().all()
            for tranche in waiting:
                await form_investment_loan(db, tranche=tranche, window=window, now=now)
        window = await advance_window(db, window_id=window_id, now=now)
    except DirectTransferError as exc:
        raise _http(exc) from exc
    append_audit(
        db,
        entity_type="FUNDING_WINDOW",
        entity_id=window_id,
        action="FUNDING_ROUND_ADVANCED",
        actor_id=actor.id,
        before_state={"status": before},
        after_state={"status": window.status, "close_reason": window.close_reason},
        ip_address=ip_address,
    )
    await db.commit()
    [row] = [r for r in await list_rounds(db, open_only=False) if r.id == window_id]
    return row


# --------------------------------------------------------------------------- #
# transfers
# --------------------------------------------------------------------------- #
async def _row(db: AsyncSession, tranche: Tranche) -> AdminInvestorTransferOut:
    window = await db.get(FundingWindow, tranche.window_id)
    project = await db.get(Project, window.project_id)
    investor = await db.get(User, tranche.investor_id)
    payee = await db.get(PaymentAccount, window.sme_payment_account_id)
    transfer = (
        await db.execute(
            select(Transfer)
            .where(Transfer.tranche_id == tranche.id, Transfer.kind == "DISBURSEMENT")
            .order_by(Transfer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    file = await db.get(EvidenceFile, transfer.evidence_file_id) if transfer and transfer.evidence_file_id else None
    dispute = (
        await db.execute(
            select(Dispute)
            .where(Dispute.tranche_id == tranche.id, Dispute.subject_type == "TRANCHE")
            .order_by(case((Dispute.status != "DECIDED", 0), else_=1), Dispute.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    receipt_waiting = bool(transfer and file and transfer.check_status == "AWAITING_FIELDS")
    dispute_open = bool(dispute and dispute.status != "DECIDED")
    return AdminInvestorTransferOut(
        id=tranche.id,
        window_id=window.id,
        project_id=project.id,
        project_name=project.legal_name,
        investor_id=investor.id,
        investor_email=investor.email,
        investor_full_name=investor.full_name,
        amount_vnd=tranche.amount_vnd,
        reference_code=tranche.reference_code,
        status=tranche.status,
        review_cause=tranche.review_cause,
        void_reason=tranche.void_reason,
        reserved_at=tranche.reserved_at,
        hold_expires_at=tranche.hold_expires_at,
        receipt_submitted_at=tranche.receipt_submitted_at,
        attested_at=tranche.attested_at,
        confirmed_at=tranche.confirmed_at,
        needs_action=receipt_waiting or dispute_open or tranche.status == "ATTESTED",
        transfer=(
            AdminTransferReceiptOut(
                id=transfer.id,
                check_status=transfer.check_status,
                failure_codes=list(transfer.failure_codes or []),
                parsed_fields=transfer.parsed_fields,
                has_file=file is not None,
                original_filename=file.original_filename if file else None,
                content_type=file.content_type if file else None,
                created_at=transfer.created_at,
            )
            if transfer
            else None
        ),
        dispute=(
            AdminTransferDisputeOut(id=dispute.id, cause=dispute.cause, status=dispute.status, outcome=dispute.outcome)
            if dispute
            else None
        ),
        expected=AdminTransferExpectedOut(
            payee_bank_name=payee.bank_name if payee else None,
            payee_account_number=payee.account_number if payee else None,
            payee_account_name=payee.account_name if payee else None,
            payer_kyc_name=await resolve_kyc_name(db, tranche.investor_id),
        ),
    )


async def list_transfers(
    db: AsyncSession, *, status: str, window_id: UUID | None, page: int, page_size: int
) -> Page[AdminInvestorTransferOut]:
    """Investor tickets, the ones needing staff first, then newest."""
    needs_action = _needs_action()
    stmt = select(Tranche)
    if window_id is not None:
        stmt = stmt.where(Tranche.window_id == window_id)
    if status == "NEEDS_ACTION":
        stmt = stmt.where(needs_action)
    elif status in _FILTER_STATUSES:
        stmt = stmt.where(Tranche.status.in_(_FILTER_STATUSES[status]))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    tranches = (
        await db.execute(
            stmt.order_by(case((needs_action, 0), else_=1), Tranche.reserved_at.desc(), Tranche.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return Page[AdminInvestorTransferOut](
        items=[await _row(db, t) for t in tranches],
        total=int(total),
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-int(total) // page_size)),
    )


async def get_transfer(db: AsyncSession, tranche_id: UUID) -> AdminInvestorTransferOut:
    """One ticket as the review panel shows it, read fresh so a decision made elsewhere is reflected."""
    tranche = await db.get(Tranche, tranche_id)
    if tranche is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return await _row(db, tranche)


async def _transfer_and_tranche(db: AsyncSession, transfer_id: UUID) -> tuple[Transfer, Tranche]:
    row = (
        await db.execute(
            select(Transfer, Tranche)
            .join(Tranche, Tranche.id == Transfer.tranche_id)
            .where(Transfer.id == transfer_id, Transfer.kind == "DISBURSEMENT")
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return row[0], row[1]


def _not_own(tranche: Tranche, actor: User) -> None:
    # The same rule as bank-account review: nobody checks money they sent themselves.
    if tranche.investor_id == actor.id:
        raise HTTPException(status_code=403, detail="You cannot review your own transfer")


async def _notify_business_of_approval(db: AsyncSession, tranche_id: UUID, status: str) -> UUID | None:
    """Ring the business's bell: staff approved an investor's transfer to its account. Staged on the caller's
    transaction, so it commits with the approval or not at all. The business's id, to publish to after the
    commit; None if the ticket has no business any more."""
    notice = await receipt_notice(db, tranche_id=tranche_id)
    if notice is None:
        return None
    create_notification(
        db,
        user_id=notice.sme_user_id,
        event=EVENT_INVESTMENT_TRANSFER_APPROVED,
        entity_type="TRANCHE",
        entity_id=tranche_id,
        data=notice.notification_data(status),
    )
    return notice.sme_user_id


async def get_receipt_url(
    db: AsyncSession, transfer_id: UUID, *, actor: User, ip_address: str | None
) -> AdminReceiptUrlResponse:
    """A 10-minute read URL for the investor's receipt. Every view is audited: it shows a person's bank details."""
    transfer, tranche = await _transfer_and_tranche(db, transfer_id)
    file = await db.get(EvidenceFile, transfer.evidence_file_id) if transfer.evidence_file_id else None
    if file is None:
        raise HTTPException(status_code=404, detail="No receipt was uploaded for this transfer")
    append_audit(
        db,
        entity_type="TRANSFER",
        entity_id=transfer.id,
        action="TRANSFER_RECEIPT_VIEWED",
        actor_id=actor.id,
        after_state={"tranche_id": str(tranche.id), "expires_in": RECEIPT_URL_TTL_SECONDS},
        ip_address=ip_address,
    )
    await db.commit()
    return AdminReceiptUrlResponse(
        url=await asyncio.to_thread(r2.presign_get, file.storage_key, expires_in=RECEIPT_URL_TTL_SECONDS),
        expires_in=RECEIPT_URL_TTL_SECONDS,
        content_type=file.content_type,
        original_filename=file.original_filename,
    )


def _blank_to_none(value: str | None) -> str | None:
    return (value or "").strip() or None


async def enter_receipt_fields(
    db: AsyncSession, transfer_id: UUID, body: AdminReceiptFieldsRequest, *, actor: User, ip_address: str | None
) -> AdminInvestorTransferOut:
    """What staff read off the receipt. The checks then attest the ticket (everything matches the QR FundLok
    issued, and the business's bell rings) or send it to review; a field left blank, because the receipt does not
    show it, fails the checks."""
    _, tranche = await _transfer_and_tranche(db, transfer_id)
    _not_own(tranche, actor)
    fields = ReceiptFields(
        amount_vnd=body.amount_vnd,
        transferred_at=body.transferred_at,
        payer_name=_blank_to_none(body.payer_name),
        payer_account=_blank_to_none(body.payer_account),
        payee_account=_blank_to_none(body.payee_account),
        memo=_blank_to_none(body.memo),
        bank_txn_id=_blank_to_none(body.bank_txn_id),
    )
    try:
        result = await staff_enter_receipt_fields(
            db, transfer_id=transfer_id, fields=fields, reason=body.reason.strip(), staff_id=actor.id, now=_now()
        )
    except DirectTransferError as exc:
        if exc.persist:
            # The service rejected the receipt and kept that on record (a bank transaction already used): the
            # rejection is a staff decision like any other, so it lands in the audit log too.
            append_audit(
                db,
                entity_type="TRANCHE",
                entity_id=tranche.id,
                action="TRANSFER_RECEIPT_REJECTED",
                actor_id=actor.id,
                after_state={
                    "transfer_id": str(transfer_id),
                    "check_status": "REJECTED",
                    "failure_codes": [exc.code],
                    "reason": body.reason.strip(),
                },
                ip_address=ip_address,
            )
            await db.commit()
        raise _http(exc) from exc
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche.id,
        action="TRANSFER_RECEIPT_CHECKED",
        actor_id=actor.id,
        after_state={
            "transfer_id": str(transfer_id),
            "check_status": result.check_status,
            "failure_codes": result.failure_codes,
            "tranche_status": result.tranche_status,
            "reason": body.reason.strip(),
        },
        ip_address=ip_address,
    )
    # A receipt that matched is an approval: the business hears of it (a mismatch goes to review first).
    business = (
        await _notify_business_of_approval(db, tranche.id, "ATTESTED")
        if result.tranche_status == "ATTESTED"
        else None
    )
    await db.commit()
    if business is not None:
        await realtime.publish([business])
    return await _row(db, await db.get(Tranche, tranche.id))


async def _settle_receipt(
    db: AsyncSession, transfer: Transfer, tranche: Tranche, *, approve: bool, reason: str, actor: User, now: dt.datetime
) -> None:
    """Staff looked at the receipt of a ticket awaiting it and approve or deny it, with no fields typed in.

    Approve: staff checked the money arrived, so it is received now: the ticket is attested and confirmed in one
    step, with no wait for the business to confirm it too. The transfer reads MATCHED with no parsed_fields; the
    evidence log says it was staff, by eye.
    Deny: the receipt is rejected as a duplicate one is: the ticket keeps waiting for a receipt, so the investor
    can upload the right one while the hold lasts, and it lapses if they do not.
    """
    # Locked in staff_enter_receipt_fields' order (transfer, ticket, round), so the two never deadlock.
    await db.refresh(transfer, with_for_update=True)
    await db.refresh(tranche, with_for_update=True)
    await lock_window(db, tranche.window_id)
    if transfer.check_status != "AWAITING_FIELDS" or tranche.status != "SIGNED":
        raise HTTPException(
            status_code=409, detail="This receipt was already decided", headers={"X-Error-Code": "ALREADY_DECIDED"}
        )
    await log_event(
        db, entity_type="TRANSFER", entity_id=transfer.id,
        event_type="STAFF_APPROVED_RECEIPT" if approve else "STAFF_REJECTED_RECEIPT", now=now,
        actor_id=actor.id, actor_role="ADMIN", reason=reason, payload={"method": "VIEWED", "fields_entered": False},
    )
    if approve:
        transfer.check_status = "MATCHED"
        transfer.confirmation_level = "CONFIRMED"
        await transition_tranche(
            db, tranche, "ATTESTED", now=now, actor_id=actor.id, actor_role="ADMIN", reason=reason,
            confirm_deadline=now,
        )
        await transition_tranche(db, tranche, "CONFIRMED", now=now, actor_id=actor.id, actor_role="ADMIN", reason=reason)
    else:
        transfer.check_status = "REJECTED"
        transfer.failure_codes = ["STAFF_REJECTED"]
        tranche.receipt_submitted_at = None
        await db.flush()


async def _settle_attested(
    db: AsyncSession, transfer: Transfer, tranche: Tranche, *, approve: bool, reason: str, actor: User, now: dt.datetime
) -> None:
    """A ticket left ATTESTED, waiting for the business to confirm (approved before approval meant received, or
    checked field by field): staff settle it. Approve: CONFIRMED. Deny: reviewed and void, its place freed."""
    await db.refresh(transfer, with_for_update=True)
    await db.refresh(tranche, with_for_update=True)
    await lock_window(db, tranche.window_id)
    if tranche.status != "ATTESTED":
        raise HTTPException(
            status_code=409, detail="This transfer was already decided", headers={"X-Error-Code": "ALREADY_DECIDED"}
        )
    if approve:
        transfer.confirmation_level = "CONFIRMED"
        await transition_tranche(db, tranche, "CONFIRMED", now=now, actor_id=actor.id, actor_role="ADMIN", reason=reason)
        return
    # ATTESTED cannot go straight to VOID: through a review, decided as not received.
    await transition_tranche(
        db, tranche, "UNDER_REVIEW", now=now, actor_id=actor.id, actor_role="ADMIN", reason=reason,
        review_cause="RECEIPT_MISMATCH",
    )
    review = await open_tranche_dispute(db, tranche=tranche, cause="RECEIPT_MISMATCH", now=now, raised_by=actor.id)
    await decide_dispute(db, dispute_id=review.id, staff_id=actor.id, outcome="NOT_RECEIVED", reason=reason, now=now)


# What an empty note records: the decision still says how it was made.
_DEFAULT_REASON = {True: "Approved after viewing the receipt", False: "Denied after viewing the receipt"}


async def decide_receipt(
    db: AsyncSession, transfer_id: UUID, *, outcome: str, reason: str | None, actor: User, ip_address: str | None
) -> AdminInvestorTransferOut:
    """Approve or deny an investor's transfer after looking at the receipt. Nothing is typed in or compared.

    Approving means the money is received: the ticket is CONFIRMED and counts toward the round, with nothing left
    for the business to confirm. A ticket under review (a late receipt) is settled as received or not
    (decide_dispute); a receipt waiting is approved or denied (_settle_receipt); a ticket left ATTESTED is
    settled too (_settle_attested). An approval rings the business's bell.
    """
    approve = outcome == "APPROVED"
    reason = (reason or "").strip() or _DEFAULT_REASON[approve]
    transfer, tranche = await _transfer_and_tranche(db, transfer_id)
    _not_own(tranche, actor)
    tranche_id, now = tranche.id, _now()
    review = (
        await db.execute(
            select(Dispute).where(
                Dispute.tranche_id == tranche_id, Dispute.subject_type == "TRANCHE", Dispute.status != "DECIDED"
            )
        )
    ).scalar_one_or_none()
    receipt_waiting = transfer.check_status == "AWAITING_FIELDS" and transfer.evidence_file_id is not None
    try:
        if review is not None:
            await decide_dispute(
                db, dispute_id=review.id, staff_id=actor.id, outcome="RECEIVED" if approve else "NOT_RECEIVED",
                reason=reason, now=now,
            )
            # A late receipt nobody typed in is settled with its review, so it leaves the queue.
            if transfer.check_status == "AWAITING_FIELDS":
                transfer.check_status = "MATCHED" if approve else "REJECTED"
        elif receipt_waiting and tranche.status == "SIGNED":
            await _settle_receipt(db, transfer, tranche, approve=approve, reason=reason, actor=actor, now=now)
        elif tranche.status == "ATTESTED":
            await _settle_attested(db, transfer, tranche, approve=approve, reason=reason, actor=actor, now=now)
        else:
            raise HTTPException(
                status_code=409,
                detail="Nothing on this transfer waits for a decision",
                headers={"X-Error-Code": "NOTHING_TO_DECIDE"},
            )
        # Received, so it starts repaying now: its own loan and daily days, in this same transaction.
        if approve and tranche.status == "CONFIRMED" and resolve(None).repay_from_approval:
            window = await db.get(FundingWindow, tranche.window_id)
            await form_investment_loan(db, tranche=tranche, window=window, now=now)
    except DirectTransferError as exc:
        raise _http(exc) from exc
    status = tranche.status
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche_id,
        action="TRANSFER_RECEIVED" if approve else "TRANSFER_NOT_RECEIVED",
        actor_id=actor.id,
        after_state={
            "transfer_id": str(transfer_id),
            "dispute_id": str(review.id) if review else None,
            "cause": review.cause if review else "RECEIPT",
            "check_status": transfer.check_status,
            "tranche_status": status,
            "reason": reason,
        },
        ip_address=ip_address,
    )
    business = (
        await _notify_business_of_approval(db, tranche_id, status)
        if approve and status in ("ATTESTED", "CONFIRMED", "ACTIVE")
        else None
    )
    await db.commit()
    if business is not None:
        await realtime.publish([business])
    db.expire_all()
    return await _row(db, await db.get(Tranche, tranche_id))


async def decide_transfer(
    db: AsyncSession, dispute_id: UUID, *, outcome: str, reason: str, actor: User, ip_address: str | None
) -> AdminInvestorTransferOut:
    """Settle a ticket under review: the money reached the business (RECEIVED, the ticket counts, and the
    business's bell rings) or it did not (NOT_RECEIVED, the ticket is void and frees its place in the round)."""
    dispute = await db.get(Dispute, dispute_id)
    if dispute is None or dispute.subject_type != "TRANCHE":
        raise HTTPException(status_code=404, detail="Review not found")
    tranche = await db.get(Tranche, dispute.tranche_id)
    _not_own(tranche, actor)
    try:
        await decide_dispute(
            db, dispute_id=dispute_id, staff_id=actor.id, outcome=outcome, reason=reason.strip(), now=_now()
        )
    except DirectTransferError as exc:
        raise _http(exc) from exc
    append_audit(
        db,
        entity_type="TRANCHE",
        entity_id=tranche.id,
        action=f"TRANSFER_{outcome}",
        actor_id=actor.id,
        after_state={"dispute_id": str(dispute_id), "cause": dispute.cause, "reason": reason.strip()},
        ip_address=ip_address,
    )
    tranche_id = tranche.id
    # Received and counted is an approval. Received too late to join a loan that already formed ends VOID, with a
    # refund owed, and is not news of money arriving; nor is NOT_RECEIVED.
    business = (
        await _notify_business_of_approval(db, tranche_id, "CONFIRMED")
        if outcome == "RECEIVED" and tranche.status == "CONFIRMED"
        else None
    )
    await db.commit()
    if business is not None:
        await realtime.publish([business])
    db.expire_all()  # the decision moved the ticket, its transfer and the dispute
    return await _row(db, await db.get(Tranche, tranche_id))


# --------------------------------------------------------------------------- #
# activity log
# --------------------------------------------------------------------------- #
# The audit actions the Activity tab shows, by group. DECISIONS is what staff decided about a transfer: a receipt
# checked (matched: recorded as paid; mismatch: sent to review), rejected, or a review settled either way.
ACTIVITY_GROUPS: dict[str, tuple[str, ...]] = {
    "DECISIONS": (
        "TRANSFER_RECEIPT_CHECKED", "TRANSFER_RECEIPT_REJECTED", "TRANSFER_RECEIVED", "TRANSFER_NOT_RECEIVED",
        "REPAYMENT_RECEIPT_APPROVED", "REPAYMENT_RECEIPT_DENIED",
    ),
    "INVESTOR": ("INVESTMENT_RESERVED", "INVESTMENT_RECEIPT_UPLOADED"),
    "BUSINESS": ("REPAYMENT_DAYS_MARKED_PAID",),
    "VIEWS": ("TRANSFER_RECEIPT_VIEWED", "REPAYMENT_RECEIPT_VIEWED"),
}
ACTIVITY_GROUPS["ALL"] = tuple(action for actions in list(ACTIVITY_GROUPS.values()) for action in actions)


def _activity_tranche_id(log: AuditLog) -> UUID | None:
    """The ticket an entry is about: its entity, or for a receipt view (entity TRANSFER) the one it names."""
    if log.entity_type == "TRANCHE":
        return log.entity_id
    raw = (log.after_state or {}).get("tranche_id")
    try:
        return UUID(raw) if raw else None
    except ValueError:
        return None


async def list_activity(
    db: AsyncSession, *, group: str, tranche_id: UUID | None, page: int, page_size: int
) -> Page[AdminFundingActivityOut]:
    """The audit trail of investor transfers, newest first: who did what to which ticket, and why. Read from the
    append-only audit log, so it is exactly what was recorded at the time."""
    stmt = select(AuditLog).where(AuditLog.action.in_(ACTIVITY_GROUPS[group]))
    if tranche_id is not None:
        stmt = stmt.where(
            or_(
                and_(AuditLog.entity_type == "TRANCHE", AuditLog.entity_id == tranche_id),
                AuditLog.after_state["tranche_id"].astext == str(tranche_id),
            )
        )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    logs = (
        await db.execute(
            stmt.order_by(AuditLog.created_at.desc(), AuditLog.id).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    # The tickets, their projects and every person named, in three queries for the page.
    ids = {tid for tid in (_activity_tranche_id(log) for log in logs) if tid is not None}
    tickets = {
        tranche.id: (tranche, project_name)
        for tranche, project_name in (
            await db.execute(
                select(Tranche, Project.legal_name)
                .join(FundingWindow, FundingWindow.id == Tranche.window_id)
                .join(Project, Project.id == FundingWindow.project_id)
                .where(Tranche.id.in_(ids))
            )
        ).all()
    } if ids else {}
    people_ids = {log.actor_id for log in logs if log.actor_id} | {t.investor_id for t, _ in tickets.values()}
    people = {
        user.id: ActorOut.model_validate(user)
        for user in (await db.execute(select(User).where(User.id.in_(people_ids)))).scalars()
    } if people_ids else {}

    items = []
    for log in logs:
        state = log.after_state or {}
        tid = _activity_tranche_id(log)
        tranche, project_name = tickets.get(tid, (None, None))
        items.append(
            AdminFundingActivityOut(
                id=log.id,
                action=log.action,
                created_at=log.created_at,
                ip_address=log.ip_address,
                actor=people.get(log.actor_id),
                tranche_id=tid,
                reference_code=tranche.reference_code if tranche else state.get("reference_code"),
                # A repayment entry is about what was repaid, not the investment's size.
                amount_vnd=(
                    state.get("net_vnd")
                    if log.action.startswith("REPAYMENT_") and "net_vnd" in state
                    else tranche.amount_vnd if tranche else state.get("amount_vnd")
                ),
                project_name=project_name,
                investor=people.get(tranche.investor_id) if tranche else None,
                check_status=state.get("check_status"),
                failure_codes=list(state.get("failure_codes") or []),
                tranche_status=state.get("tranche_status") or state.get("status"),
                reason=state.get("reason"),
            )
        )
    return Page[AdminFundingActivityOut](
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
        total_pages=max(1, -(-int(total) // page_size)),
    )
