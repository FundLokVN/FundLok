"""Spec: docs/specs/direct_transfer/transfer-and-evidence.md sections 6-8 (exceptions queue, manual entry) and B14."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.direct_transfer import admin, close, tranches
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import Dispute, EvidenceLog, Tranche, Transfer
from app.direct_transfer.receipts import ManualEntryExtractor, ReceiptFields

from .helpers import T0, accept, at, fund_window, good_fields, make_investor, open_window, reserve, upload_receipt

M = 1_000_000


async def signed_with_manual_receipt(world, amount=10 * M):
    if world.window is None:
        await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, amount)
    await accept(world, inv, t, now=at(minutes=1))
    res = await tranches.submit_receipt(
        world.db, tranche_id=t.id, investor_id=inv.user.id, content=f"manual-{t.id}".encode(), content_type="image/png",
        filename="r.png", extractor=ManualEntryExtractor(), storage=world.storage, idempotency_key=None, now=at(minutes=10), params=world.params,
    )
    transfer = (await world.db.execute(select(Transfer).where(Transfer.id == res.transfer_id))).scalar_one()
    return inv, t, transfer


async def test_manual_entry_extractor_leaves_transfer_awaiting_fields_and_hold_does_not_expire(world):
    inv, t, transfer = await signed_with_manual_receipt(world)
    assert transfer.check_status == "AWAITING_FIELDS" and transfer.confirmation_level == "REPORTED"
    assert t.status == "SIGNED" and t.receipt_submitted_at == at(minutes=10)
    assert await tranches.expire_holds(world.db, now=at(days=3)) == 0
    await world.db.refresh(t)
    assert t.status == "SIGNED"


async def test_exceptions_queue_lists_awaiting_fields_with_file_and_timeline(world):
    inv, t, transfer = await signed_with_manual_receipt(world)
    items = await admin.list_exceptions(world.db)
    assert [i["kind"] for i in items] == ["AWAITING_FIELDS"]
    item = items[0]
    assert item["tranche_id"] == t.id and item["transfer_id"] == transfer.id and item["file_key"] in world.storage.objects
    assert item["parsed_fields"] is None and item["amount_vnd"] == 10 * M
    assert [e["event_type"] for e in item["timeline"]] == ["RECEIPT_UPLOADED"]


async def test_staff_field_entry_runs_checks_and_attests(world):
    inv, t, transfer = await signed_with_manual_receipt(world)
    fields = good_fields(world, t, inv, when=at(minutes=8))
    res = await tranches.staff_enter_receipt_fields(
        world.db, transfer_id=transfer.id, fields=fields, reason="read from the uploaded receipt", staff_id=world.admin.id, now=at(minutes=30), params=world.params
    )
    assert (res.tranche_status, res.check_status, res.confirmation_level) == ("ATTESTED", "MATCHED", "ATTESTED")
    assert t.attested_at == at(minutes=30)
    entered = (await world.db.execute(select(EvidenceLog).where(EvidenceLog.event_type == "FIELDS_ENTERED"))).scalar_one()
    assert entered.actor_id == world.admin.id and entered.actor_role == "ADMIN" and entered.reason == "read from the uploaded receipt"
    assert entered.payload["fields"]["bank_txn_id"] == fields.bank_txn_id
    assert await admin.list_exceptions(world.db) == []
    with pytest.raises(DirectTransferError) as info:  # fields can only be entered once
        await tranches.staff_enter_receipt_fields(
            world.db, transfer_id=transfer.id, fields=fields, reason="again", staff_id=world.admin.id, now=at(minutes=31), params=world.params
        )
    assert info.value.status_code == 400


async def test_staff_field_entry_mismatch_sends_to_review(world):
    inv, t, transfer = await signed_with_manual_receipt(world)
    fields = good_fields(world, t, inv, when=at(minutes=8), payer_name="SOMEONE ELSE")
    res = await tranches.staff_enter_receipt_fields(
        world.db, transfer_id=transfer.id, fields=fields, reason="entered", staff_id=world.admin.id, now=at(minutes=30), params=world.params
    )
    assert res.tranche_status == "UNDER_REVIEW" and res.failure_codes == ["PAYER_NAME_MISMATCH"]
    kinds = [i["kind"] for i in await admin.list_exceptions(world.db)]
    assert kinds == ["RECEIPT_MISMATCH"]


async def test_staff_action_requires_reason(world):
    inv, t, transfer = await signed_with_manual_receipt(world)
    for reason in ("", "   "):
        with pytest.raises(DirectTransferError) as info:
            await tranches.staff_enter_receipt_fields(
                world.db, transfer_id=transfer.id, fields=good_fields(world, t, inv, when=at(minutes=8)), reason=reason,
                staff_id=world.admin.id, now=at(minutes=30), params=world.params,
            )
        assert info.value.status_code == 422 and info.value.code == "REASON_REQUIRED"


async def test_late_receipt_goes_to_exceptions_queue(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    assert await tranches.expire_holds(world.db, now=at(hours=3)) == 1
    res = await upload_receipt(world, inv, t, now=at(hours=4), fields=None)  # money moved after the hold lapsed
    assert res.tranche_status == "UNDER_REVIEW"
    assert t.review_cause == "LATE_RECEIPT"
    d = (await world.db.execute(select(Dispute).where(Dispute.tranche_id == t.id))).scalar_one()
    assert d.cause == "LATE_RECEIPT" and d.required_next_party == "STAFF"
    items = await admin.list_exceptions(world.db)
    assert [i["kind"] for i in items] == ["LATE_RECEIPT"]
    # a late tranche holds no capacity until staff revive it
    from app.direct_transfer import windows

    assert await windows.capacity_left(world.db, world.window) == 100 * M


async def test_lazy_expiry_treats_lapsed_hold_as_late_receipt(world):
    """A receipt arriving after the hold lapsed but before the job ran is accepted for review, not refused."""
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    res = await upload_receipt(world, inv, t, now=at(hours=5), fields=None)
    assert res.tranche_status == "UNDER_REVIEW" and t.expired_at == at(hours=5)


async def test_receipt_after_window_finalized_is_refused(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    await fund_window(world, [60 * M])
    await close.advance_window(world.db, window_id=world.window.id, now=at(days=14), params=world.params)
    await world.db.refresh(world.window)
    assert world.window.status == "REPAYING"
    with pytest.raises(DirectTransferError) as info:
        await upload_receipt(world, inv, t, now=at(days=15), fields=None)
    assert info.value.code == "TRANCHE_NOT_SIGNED"


# ----------------------------------------------------------------------------- refund receipts
async def failed_window_with_refunds(world):
    tr = await fund_window(world, [10 * M, 10 * M])
    await close.advance_window(world.db, window_id=world.window.id, now=at(days=14), params=world.params)
    await world.db.refresh(world.window)
    assert world.window.status == "FAILED"
    refunds = (await world.db.execute(select(Transfer).where(Transfer.kind == "REFUND").order_by(Transfer.created_at))).scalars().all()
    return tr, refunds


async def test_refund_receipt_is_tracked_like_any_transfer(world):
    tr, refunds = await failed_window_with_refunds(world)
    refund = refunds[0]
    tranche = await world.db.get(Tranche, refund.tranche_id)
    investor_account = await world.db.get(__import__("app.direct_transfer.models", fromlist=["PaymentAccount"]).PaymentAccount, tranche.investor_account_id)
    world.extractor.next_fields = ReceiptFields(
        amount_vnd=10 * M, transferred_at=at(days=15), payer_name="CONG TY ABC", payer_account=world.sme_account_number,
        payee_account=investor_account.account_number, memo=f"hoan tien {tranche.reference_code}", bank_txn_id="FT-REFUND-1",
    )
    out = await tranches.submit_refund_receipt(
        world.db, transfer_id=refund.id, sme_user_id=world.sme.id, content=b"refund-receipt-1", content_type="application/pdf", filename="refund.pdf",
        extractor=world.extractor, storage=world.storage, now=at(days=15, hours=1), params=world.params,
    )
    assert out.check_status == "MATCHED" and out.confirmation_level == "ATTESTED" and out.bank_txn_id == "FT-REFUND-1"
    other = await make_investor(world)
    with pytest.raises(DirectTransferError) as info:
        await tranches.submit_refund_receipt(
            world.db, transfer_id=refunds[1].id, sme_user_id=other.user.id, content=b"x", content_type="application/pdf", filename="x",
            extractor=world.extractor, storage=world.storage, now=at(days=15), params=world.params,
        )
    assert info.value.status_code == 404
    items = await admin.list_exceptions(world.db)
    assert items == []  # the second refund has no receipt yet, so nothing for staff to enter


async def test_refund_receipt_via_staff_entry_and_mismatch(world):
    tr, refunds = await failed_window_with_refunds(world)
    refund = refunds[0]
    tranche = await world.db.get(Tranche, refund.tranche_id)
    res = await tranches.submit_refund_receipt(
        world.db, transfer_id=refund.id, sme_user_id=world.sme.id, content=b"refund-receipt-2", content_type="application/pdf", filename="r.pdf",
        extractor=ManualEntryExtractor(), storage=world.storage, now=at(days=15), params=world.params,
    )
    assert res.check_status == "AWAITING_FIELDS"
    assert [i["kind"] for i in await admin.list_exceptions(world.db)] == ["REFUND_AWAITING_FIELDS"]
    wrong = ReceiptFields(
        amount_vnd=5 * M, transferred_at=at(days=15), payer_name="CONG TY ABC", payer_account="1", payee_account="999",
        memo="x", bank_txn_id="FT-REFUND-2",
    )
    out = await tranches.staff_enter_receipt_fields(
        world.db, transfer_id=refund.id, fields=wrong, reason="entered", staff_id=world.admin.id, now=at(days=15, hours=2), params=world.params
    )
    assert out.check_status == "MISMATCH" and set(out.failure_codes) == {"AMOUNT_MISMATCH", "REFERENCE_MISSING", "PAYEE_MISMATCH"}
