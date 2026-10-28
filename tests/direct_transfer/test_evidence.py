"""Spec: docs/specs/direct_transfer/transfer-and-evidence.md section 8 (B1 evidence log, B4/B7 receipt flow)."""
import ast
import datetime as dt
import inspect
import pathlib
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.direct_transfer import evidence, receipts, tranches
from app.direct_transfer.config import DirectTransferParams
from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.evidence import log_event, store_evidence_file
from app.direct_transfer.models import AppendOnlyViolation, EvidenceLog, Tranche, Transfer
from app.direct_transfer.receipts import ManualEntryExtractor
from app.direct_transfer.storage import InMemoryStorage

from .helpers import (
    T0,
    accept,
    at,
    attested_tranche,
    good_fields,
    make_account,
    make_investor,
    make_user,
    open_window,
    reserve,
    upload_receipt,
)

M = 1_000_000


async def signed(world, amount=10 * M, now=T0):
    await open_window(world) if world.window is None else None
    inv = await make_investor(world)
    t = await reserve(world, inv, amount, now=now)
    await accept(world, inv, t, now=now + dt.timedelta(minutes=1))
    return inv, t


async def test_receipt_submission_matched_attests_tranche_and_logs(world):
    inv, t = await signed(world)
    res = await upload_receipt(world, inv, t, now=at(minutes=10))
    assert (res.tranche_status, res.check_status, res.failure_codes, res.confirmation_level) == ("ATTESTED", "MATCHED", [], "ATTESTED")
    assert t.attested_at == at(minutes=10)
    transfer = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == t.id))).scalar_one()
    assert transfer.from_account_id == inv.account_id and transfer.to_account_id == world.window.sme_payment_account_id
    events = [
        e.event_type
        for e in (await world.db.execute(select(EvidenceLog).where(EvidenceLog.entity_id.in_([t.id, transfer.id])).order_by(EvidenceLog.created_at))).scalars()
    ]
    assert {"RECEIPT_UPLOADED", "FIELDS_ENTERED", "CHECKS_RUN", "TRANCHE_TRANSITION"} <= set(events)
    logged = (await world.db.execute(select(EvidenceLog).where(EvidenceLog.event_type == "RECEIPT_UPLOADED"))).scalar_one()
    assert len(logged.file_sha256) == 64


async def test_receipt_submission_mismatch_moves_to_under_review(world):
    inv, t = await signed(world)
    bad = good_fields(world, t, inv, when=at(minutes=10), amount_vnd=9_999_999)
    res = await upload_receipt(world, inv, t, now=at(minutes=10), fields=bad)
    assert res.tranche_status == "UNDER_REVIEW" and res.check_status == "MISMATCH" and res.failure_codes == ["AMOUNT_MISMATCH"]
    assert t.review_cause == "RECEIPT_MISMATCH"
    from app.direct_transfer.models import Dispute

    d = (await world.db.execute(select(Dispute).where(Dispute.tranche_id == t.id))).scalar_one()
    assert d.cause == "RECEIPT_MISMATCH" and d.required_next_party == "STAFF"


async def test_receipt_submission_idempotent_by_key(world):
    inv, t = await signed(world)
    r1 = await upload_receipt(world, inv, t, now=at(minutes=10), key="rk-1")
    r2 = await upload_receipt(world, inv, t, now=at(minutes=11), key="rk-1")
    assert r1.transfer_id == r2.transfer_id
    n = len((await world.db.execute(select(Transfer).where(Transfer.tranche_id == t.id))).scalars().all())
    assert n == 1


async def test_duplicate_bank_txn_id_rejected_and_unique_index_enforced(world):
    inv, t = await signed(world)
    inv2, t2 = await signed(world)
    f1 = good_fields(world, t, inv, when=at(minutes=10), bank_txn_id="FT-SAME")
    await upload_receipt(world, inv, t, now=at(minutes=10), fields=f1)
    f2 = good_fields(world, t2, inv2, when=at(minutes=11), bank_txn_id="FT-SAME")
    with pytest.raises(DirectTransferError) as info:
        await upload_receipt(world, inv2, t2, now=at(minutes=11), fields=f2)
    assert info.value.code == "DUPLICATE_BANK_TXN" and info.value.status_code == 409 and info.value.persist is True
    await world.db.refresh(t2)
    assert t2.status == "SIGNED" and t2.receipt_submitted_at is None  # not funded; the hold clock applies again
    rejected = (await world.db.execute(select(Transfer).where(Transfer.tranche_id == t2.id))).scalar_one()
    assert rejected.check_status == "REJECTED" and rejected.failure_codes == ["DUPLICATE_BANK_TXN"]
    assert (await world.db.execute(select(EvidenceLog).where(EvidenceLog.event_type == "DUPLICATE_REJECTED"))).scalars().first() is not None
    # the DB itself refuses a second disbursement with the same bank txn id
    world.db.add(
        Transfer(kind="DISBURSEMENT", tranche_id=t2.id, amount_vnd=1, reference="FLXXXXXXXX", bank_txn_id="FT-SAME")
    )
    with pytest.raises(IntegrityError):
        await world.db.flush()
    await world.db.rollback()


async def test_one_bank_txn_cannot_fund_two_tranches(world):
    inv, t = await signed(world)
    inv2, t2 = await signed(world)
    await upload_receipt(world, inv, t, now=at(minutes=10), fields=good_fields(world, t, inv, when=at(minutes=10), bank_txn_id="FT-ONE"))
    with pytest.raises(DirectTransferError):
        await upload_receipt(world, inv2, t2, now=at(minutes=12), fields=good_fields(world, t2, inv2, when=at(minutes=12), bank_txn_id="FT-ONE"))
    await world.db.refresh(t)
    await world.db.refresh(t2)
    assert t.status == "ATTESTED" and t2.status == "SIGNED"


async def test_duplicate_file_hash_rejected(world):
    inv, t = await signed(world)
    inv2, t2 = await signed(world)
    await upload_receipt(world, inv, t, now=at(minutes=10), content=b"same-bytes")
    with pytest.raises(DirectTransferError) as info:
        await upload_receipt(world, inv2, t2, now=at(minutes=11), content=b"same-bytes")
    assert info.value.code == "DUPLICATE_FILE" and info.value.persist is True
    assert (await world.db.execute(select(EvidenceLog).where(EvidenceLog.event_type == "DUPLICATE_REJECTED", EvidenceLog.entity_id == t2.id))).scalars().first()


async def test_receipt_upload_rejects_wrong_content_type_and_size(world):
    inv, _ = await signed(world)
    storage = InMemoryStorage()
    with pytest.raises(DirectTransferError) as e1:
        await store_evidence_file(world.db, storage, owner_user_id=inv.user.id, purpose="RECEIPT", content=b"x", content_type="application/zip")
    assert e1.value.status_code == 415
    with pytest.raises(DirectTransferError) as e2:
        await store_evidence_file(
            world.db, storage, owner_user_id=inv.user.id, purpose="RECEIPT", content=b"x" * 11, content_type="application/pdf",
            params=DirectTransferParams(max_evidence_bytes=10),
        )
    assert e2.value.status_code == 413
    with pytest.raises(DirectTransferError) as e3:
        await store_evidence_file(world.db, storage, owner_user_id=inv.user.id, purpose="RECEIPT", content=b"", content_type="application/pdf")
    assert e3.value.status_code == 400
    assert storage.objects == {}  # nothing stored before validation passed


async def test_storage_failure_is_503_without_leaking_internals(world):
    inv, _ = await signed(world)

    class Broken:
        async def put(self, *a, **k):
            raise RuntimeError("secret connection string")

    with pytest.raises(DirectTransferError) as info:
        await store_evidence_file(world.db, Broken(), owner_user_id=inv.user.id, purpose="RECEIPT", content=b"x", content_type="application/pdf")
    assert info.value.status_code == 503 and "secret" not in info.value.detail


async def test_investor_cannot_submit_receipt_for_someone_elses_tranche_returns_404(world):
    inv, t = await signed(world)
    other = await make_investor(world)
    with pytest.raises(DirectTransferError) as info:
        await upload_receipt(world, other, t, now=at(minutes=10))
    assert info.value.status_code == 404


async def test_receipt_on_unsigned_tranche_rejected(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    with pytest.raises(DirectTransferError) as info:
        await upload_receipt(world, inv, t, now=at(minutes=10))
    assert info.value.code == "TRANCHE_NOT_SIGNED"


async def test_payment_account_requires_staff_approval_before_use(world):
    await open_window(world)
    user = await make_user(world.db, role="INVESTOR", full_name="Tran Van B")
    pending = await make_account(world.db, user, role="INVESTOR")  # not approved
    with pytest.raises(DirectTransferError) as info:
        from app.direct_transfer import reservation

        await reservation.reserve_ticket(
            world.db, window_id=world.window.id, investor_id=user.id, amount_vnd=10 * M, ack_risk_disclosure=True,
            payment_account_id=pending.id, idempotency_key="k", now=T0, params=world.params,
        )
    assert info.value.code == "ACCOUNT_NOT_APPROVED"
    from app.direct_transfer.accounts import decide_payment_account

    with pytest.raises(DirectTransferError) as info2:
        await decide_payment_account(world.db, account_id=pending.id, decision="APPROVED", reason=" ", staff_id=world.admin.id, now=T0)
    assert info2.value.status_code == 422
    await decide_payment_account(world.db, account_id=pending.id, decision="APPROVED", reason="statement checked", staff_id=world.admin.id, now=T0)
    t = await reservation.reserve_ticket(
        world.db, window_id=world.window.id, investor_id=user.id, amount_vnd=10 * M, ack_risk_disclosure=True,
        payment_account_id=pending.id, idempotency_key="k2", now=T0, params=world.params,
    )
    assert t.status == "RESERVED"


async def test_payment_instructions_carry_a_valid_vietqr(world):
    inv, t = await signed(world)
    pay = await tranches.payment_instructions(world.db, tranche_id=t.id, investor_id=inv.user.id, now=at(minutes=2))
    from app.direct_transfer.vietqr import parse_tlv, verify_crc

    assert verify_crc(pay.vietqr_payload) and pay.reference_code == t.reference_code and pay.account_number == world.sme_account_number
    assert parse_tlv(pay.vietqr_payload)["54"] == str(10 * M)
    other = await make_investor(world)
    with pytest.raises(DirectTransferError) as info:
        await tranches.payment_instructions(world.db, tranche_id=t.id, investor_id=other.user.id, now=at(minutes=2))
    assert info.value.status_code == 404


# ----------------------------------------------------------------------------- append-only
async def test_evidence_log_is_append_only_at_db_level(world):
    row = await log_event(world.db, entity_type="WINDOW", entity_id=uuid.uuid4(), event_type="WINDOW_OPENED", now=T0, payload={"a": 1})
    with pytest.raises(DBAPIError) as e1:
        await world.db.execute(text("UPDATE evidence_log SET reason = 'tampered'"))
    assert "append-only" in str(e1.value)
    await world.db.rollback()
    row = await log_event(world.db, entity_type="WINDOW", entity_id=uuid.uuid4(), event_type="WINDOW_OPENED", now=T0)
    with pytest.raises(DBAPIError) as e2:
        await world.db.execute(text("DELETE FROM evidence_log"))
    assert "append-only" in str(e2.value)
    await world.db.rollback()


async def test_evidence_log_orm_updates_and_deletes_are_refused_at_app_level(world):
    row = await log_event(world.db, entity_type="WINDOW", entity_id=uuid.uuid4(), event_type="WINDOW_OPENED", now=T0)
    row.reason = "edited"
    with pytest.raises(AppendOnlyViolation):
        await world.db.flush()
    await world.db.rollback()
    row = await log_event(world.db, entity_type="WINDOW", entity_id=uuid.uuid4(), event_type="WINDOW_OPENED", now=T0)
    await world.db.delete(row)
    with pytest.raises(AppendOnlyViolation):
        await world.db.flush()
    await world.db.rollback()


def test_evidence_log_has_no_update_or_delete_function_in_service():
    public = [n for n, f in inspect.getmembers(evidence, inspect.isfunction) if f.__module__ == evidence.__name__]
    assert not [n for n in public if any(w in n for w in ("update", "delete", "remove", "edit"))]
    root = pathlib.Path(evidence.__file__).parent
    for path in root.glob("*.py"):
        src = path.read_text()
        assert "update(EvidenceLog" not in src and "delete(EvidenceLog" not in src, path.name


def test_no_external_ocr_called():
    """The only extraction seam is ReceiptExtractor; the MVP implementation reads nothing, and nothing in the receipt
    path imports a network or AI client (I-14)."""
    root = pathlib.Path(receipts.__file__).parent
    for name in ("receipts.py", "tranches.py", "evidence.py", "storage.py"):
        tree = ast.parse((root / name).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"httpx", "requests", "aiohttp", "openai", "anthropic", "boto3", "urllib3"}, (name, imported)
    assert isinstance(receipts.ManualEntryExtractor(), object)


async def test_manual_entry_extractor_is_default_noop(world):
    inv, t = await signed(world)
    res = await tranches.submit_receipt(
        world.db, tranche_id=t.id, investor_id=inv.user.id, content=b"%PDF-1.4 receipt", content_type="application/pdf", filename="r.pdf",
        extractor=ManualEntryExtractor(), storage=world.storage, idempotency_key=None, now=at(minutes=10), params=world.params,
    )
    assert res.check_status == "AWAITING_FIELDS" and res.confirmation_level == "REPORTED" and res.tranche_status == "SIGNED"
