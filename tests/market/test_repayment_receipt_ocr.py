"""A business's repayment receipt read by OCR and checked against the repayment (receipt-ocr.md section 10).

A confident read that matches is shown to the investor and staff as matching, but the investor still confirms the
money arrived: nothing is settled by the system. A confident read that clearly does not match is refused, with
automatic decisions on: nothing is stored or marked paid and the days stay due. Anything unsure, or a mismatch with
automatic decisions off, goes through as before and is shown. The OCR engine is replaced by a fake reader
(tests/uploads/test_bank_receipt_ocr.py covers the reading itself).
"""
import dataclasses
import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select

from app.direct_transfer.calendar_utils import ict_date
from app.direct_transfer.evidence import log_event
from app.direct_transfer.models import EvidenceFile, EvidenceLog, PaymentAccount, RepaymentDay, Tranche, Transfer
from app.direct_transfer.receipts import ReceiptFields
from app.lending.models import AuditLog
from app.main import app
from app.uploads.receipt_ocr import ReceiptOcr, ReceiptReading, receipt_ocr
from tests.admin.test_repayment_receipts import _days, _repaying
from tests.market.test_funding_flow import PNG, SME_ACCOUNT

INVESTOR_HOLDER = "NGUYEN VAN AN"


class FakeReader:
    def __init__(self, reading: ReceiptReading):
        self.reading = reading
        self.calls = 0

    async def read(self, *, content: bytes, content_type: str) -> ReceiptReading:
        self.calls += 1
        return self.reading


@pytest.fixture(autouse=True)
def _no_ocr_left_behind(monkeypatch):
    monkeypatch.delenv("RECEIPT_OCR_ENABLED", raising=False)
    yield
    app.dependency_overrides.pop(receipt_ocr, None)


def _use_ocr(reading: ReceiptReading, *, auto: bool = True) -> FakeReader:
    reader = FakeReader(reading)
    app.dependency_overrides[receipt_ocr] = lambda: ReceiptOcr(reader=reader, auto_verdict=auto)
    return reader


async def _investor_account(db_session, s) -> PaymentAccount:
    tranche = await db_session.get(Tranche, uuid.UUID(s["ticket"]["id"]), populate_existing=True)
    return await db_session.get(PaymentAccount, tranche.investor_account_id, populate_existing=True)


async def _verify_investor_account(db_session, s, holder: str = INVESTOR_HOLDER) -> None:
    """Staff verified the investor's account at the bank (D-031): the holder name the bank showed is on record."""
    account = await _investor_account(db_session, s)
    await log_event(
        db_session, entity_type="PAYMENT_ACCOUNT", entity_id=account.id, event_type="ACCOUNT_VERIFIED",
        now=dt.datetime.now(dt.timezone.utc), payload={"verified_holder_name": holder},
    )
    await db_session.commit()


async def _setup(client, db_session, make_admin, make_user, make_eligible_investor, *, verified: bool = True):
    s = await _repaying(client, db_session, make_admin, make_user, make_eligible_investor)
    if verified:
        await _verify_investor_account(db_session, s)
    payee_account = (await _investor_account(db_session, s)).account_number
    days = await _days(db_session, s["day_ids"])
    return s | {"payee_account": payee_account, "net_vnd": sum(int(d.net_vnd) for d in days)}


def _reading(s, *, confident: bool = True, payee_bank: str | None = "Vietcombank", **overrides) -> ReceiptReading:
    """What a correct receipt of this repayment reads as: the business's transfer to the investor's account."""
    fields = ReceiptFields(
        amount_vnd=s["net_vnd"],
        transferred_at=dt.datetime.now(dt.timezone.utc),
        payer_name="CONG TY ABC",  # the business's account, verified at the bank as "CONG TY ABC"
        payer_account=SME_ACCOUNT,
        payee_account=s["payee_account"],  # the investor's account, at Vietcombank (970436)
        memo=f"{s['ticket']['reference_code']} tra no",
        bank_txn_id=f"FT{uuid.uuid4().hex[:12].upper()}",
        payee_name=INVESTOR_HOLDER,
    )
    fields = dataclasses.replace(fields, **overrides)
    return ReceiptReading(
        engine="fake-ocr",
        fields=fields,
        field_confidence={name: 95.0 for name in fields.to_json()},
        confidence=95.0 if confident else 41.0,
        confident=confident,
        notes=() if confident else ("LOW_CONFIDENCE:amount_vnd",),
        payee_bank=payee_bank,
    )


async def _pay(client, s, *, day_ids=None, key=None):
    return await client.post(
        f"/repayment/tranches/{s['ticket']['id']}/pay",
        data={"repayment_day_ids": day_ids or s["day_ids"]},
        files={"file": ("receipt.png", PNG + uuid.uuid4().bytes, "image/png")},
        headers=s["sme"]["headers"] | {"Idempotency-Key": key or str(uuid.uuid4())},
    )


async def _investor_receipts(client, s):
    resp = await client.get("/repayment/receipts", headers=s["investor"]["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _admin_receipts(client, s):
    resp = await client.get("/admin/funding/repayments", headers=s["admin"]["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def _audit(db_session, action):
    return (await db_session.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()


async def _count(db_session, model, *where):
    return (await db_session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _bell(client, user, event):
    items = (await client.get("/notifications", headers=user["headers"])).json()["items"]
    return [n for n in items if n["event"] == event]


# --------------------------------------------------------------------------- #
# OCR off, and a match
# --------------------------------------------------------------------------- #
async def test_with_ocr_off_a_repayment_receipt_is_not_read(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _pay(client, s)

    assert resp.status_code == 200, resp.text
    [receipt] = await _investor_receipts(client, s)
    assert receipt["system_check"] is None
    [row] = await _admin_receipts(client, s)
    assert row["system_check"] is None
    assert await _count(db_session, EvidenceLog, EvidenceLog.event_type == "RECEIPT_OCR_READ") == 0


async def test_a_matching_repayment_receipt_is_shown_as_matching_and_still_waits_for_the_investor(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)
    # The bank shortened the business's name: still the business.
    reading = _reading(s, payer_name="CTY ABC")
    _use_ocr(reading)

    resp = await _pay(client, s)

    assert resp.status_code == 200, resp.text
    # Marked paid as before, and nothing more: no day is matched or settled by the system.
    days = await _days(db_session, s["day_ids"])
    assert {d.status for d in days} == {"PRESUMED_PAID"}
    assert all(d.settled_at is None and d.matched_at is None for d in days)
    transfers = (await db_session.execute(select(Transfer).where(Transfer.kind == "REPAYMENT"))).scalars().all()
    assert transfers and {t.check_status for t in transfers} == {"AWAITING_FIELDS"}
    # What was read is kept on each transfer.
    assert {t.parsed_fields["bank_txn_id"] for t in transfers} == {reading.fields.bank_txn_id}
    assert all(t.parsed_fields["payee_bank"] == "Vietcombank" for t in transfers)

    # The investor sees it matches, and still confirms it themselves.
    [receipt] = await _investor_receipts(client, s)
    assert receipt["status"] == "AWAITING_CONFIRMATION"
    assert receipt["system_check"]["result"] == "MATCHES" and receipt["system_check"]["failure_codes"] == []
    [notice] = await _bell(client, s["investor"], "REPAYMENT_RECEIPT_SUBMITTED")
    assert notice["data"]["receipt_id"] == receipt["id"]

    # Staff see the reading, the receiver and the payer; nothing was applied.
    [row] = await _admin_receipts(client, s)
    check = row["system_check"]
    assert check["verdict"] == "ACCEPTED" and check["applied"] is False and check["suggested"] == "ACCEPTED"
    assert check["receiver"]["expected_holder"] == INVESTOR_HOLDER and check["receiver"]["expected_bank_bin"] == "970436"
    assert check["receiver"]["bank_identified_bin"] == "970436"
    assert check["payer"] == {"holder_read": "CTY ABC", "expected_holder": "CONG TY ABC", "expected_account": SME_ACCOUNT}
    assert check["fields"]["amount_vnd"] == s["net_vnd"] and check["engine"] == "fake-ocr"
    assert row["needs_action"] is True  # waiting on the investor, as before
    [audit] = await _audit(db_session, "REPAYMENT_RECEIPT_SYSTEM_ACCEPTED")
    assert audit.actor_id is None and str(audit.entity_id) == s["ticket"]["id"]
    assert audit.after_state["receipt_id"] == receipt["id"] and audit.after_state["refused"] is False

    # Confirming works exactly as before.
    confirmed = await client.post(f"/repayment/receipts/{receipt['id']}/confirm", headers=s["investor"]["headers"])
    assert confirmed.status_code == 200, confirmed.text
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"MATCHED"}


# --------------------------------------------------------------------------- #
# refused
# --------------------------------------------------------------------------- #
async def test_a_repayment_receipt_that_does_not_match_is_refused_and_nothing_is_marked(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)
    # The business paid less than the QR asked for.
    _use_ocr(_reading(s, amount_vnd=s["net_vnd"] - 1_000))

    resp = await _pay(client, s)

    assert resp.status_code == 422, resp.text
    assert resp.headers["X-Error-Code"] == "RECEIPT_MISMATCH"
    assert resp.json()["details"] == {"failure_codes": ["AMOUNT_MISMATCH"]}
    assert "AMOUNT_MISMATCH" in resp.json()["detail"]
    # Nothing stored, nothing marked, nobody told.
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"DUE"}
    assert await _count(db_session, Transfer, Transfer.kind == "REPAYMENT") == 0
    assert await _count(
        db_session, EvidenceFile, EvidenceFile.owner_user_id == uuid.UUID(s["sme"]["id"]), EvidenceFile.purpose == "RECEIPT"
    ) == 0
    assert await _investor_receipts(client, s) == []
    assert await _bell(client, s["investor"], "REPAYMENT_RECEIPT_SUBMITTED") == []
    assert await _audit(db_session, "REPAYMENT_DAYS_MARKED_PAID") == []

    # Staff can see what was tried: the reading and the verdict against the investment, with the file's hash.
    events = (
        await db_session.execute(
            select(EvidenceLog).where(
                EvidenceLog.entity_type == "TRANCHE", EvidenceLog.entity_id == uuid.UUID(s["ticket"]["id"])
            ).order_by(EvidenceLog.created_at)
        )
    ).scalars().all()
    ocr = [e for e in events if e.event_type in ("RECEIPT_OCR_READ", "SYSTEM_VERDICT")]
    assert [e.event_type for e in ocr] == ["RECEIPT_OCR_READ", "SYSTEM_VERDICT"]
    assert all(e.actor_role == "SYSTEM" and e.actor_id is None and e.file_sha256 for e in ocr)
    assert ocr[1].payload["verdict"] == "DENIED" and ocr[1].payload["applied"] is True
    [audit] = await _audit(db_session, "REPAYMENT_RECEIPT_SYSTEM_DENIED")
    assert audit.after_state["refused"] is True and audit.after_state["failure_codes"] == ["AMOUNT_MISMATCH"]
    assert audit.after_state["net_vnd"] == s["net_vnd"] and audit.after_state["file_sha256"] == ocr[0].file_sha256
    activity = await client.get(
        "/admin/funding/activity", params={"group": "DECISIONS"}, headers=s["admin"]["headers"]
    )
    assert activity.status_code == 200, activity.text
    [entry] = [e for e in activity.json()["items"] if e["action"] == "REPAYMENT_RECEIPT_SYSTEM_DENIED"]
    assert entry["tranche_id"] == s["ticket"]["id"] and entry["failure_codes"] == ["AMOUNT_MISMATCH"]
    assert entry["amount_vnd"] == s["net_vnd"]

    # The right receipt then goes through.
    _use_ocr(_reading(s))
    again = await _pay(client, s)
    assert again.status_code == 200, again.text
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"PRESUMED_PAID"}


async def test_a_receipt_paid_to_the_wrong_receiver_or_from_another_payer_is_refused(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)
    cases = [
        (_reading(s, payee_account="9999999999"), "PAYEE_MISMATCH"),
        (_reading(s, payee_bank="Techcombank"), "PAYEE_BANK_MISMATCH"),
        (_reading(s, payee_name="TRAN THI B"), "PAYEE_NAME_MISMATCH"),
        (_reading(s, payer_name="NGUYEN VAN C"), "PAYER_NAME_MISMATCH"),
        (_reading(s, memo="tra no thang 11"), "REFERENCE_MISSING"),
        (_reading(s, transferred_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)), "TIME_OUT_OF_RANGE"),
    ]
    for reading, code in cases:
        _use_ocr(reading)
        resp = await _pay(client, s)
        assert resp.status_code == 422, (code, resp.text)
        assert resp.json()["details"]["failure_codes"] == [code]

    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"DUE"}
    assert await _count(db_session, Transfer, Transfer.kind == "REPAYMENT") == 0


async def test_a_bank_transaction_already_used_for_another_repayment_is_refused(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)
    first = _reading(s)
    _use_ocr(first)
    assert (await _pay(client, s)).status_code == 200

    # The next day falls due, and the business uploads another screenshot of the same transfer for it.
    schedule = (
        await client.get(f"/repayment/tranches/{s['ticket']['id']}/schedule", headers=s["sme"]["headers"])
    ).json()
    second_date = sorted({r["due_date"] for r in schedule if r["repayment_day_id"] not in s["day_ids"]})[0]
    next_ids = [r["repayment_day_id"] for r in schedule if r["due_date"] == second_date]
    db_session.expire_all()
    for day in (await db_session.execute(select(RepaymentDay).where(RepaymentDay.id.in_(next_ids)))).scalars():
        day.due_date = ict_date(dt.datetime.now(dt.timezone.utc))
    await db_session.commit()
    next_net = sum(int(d.net_vnd) for d in await _days(db_session, next_ids))
    _use_ocr(_reading(s, amount_vnd=next_net, bank_txn_id=first.fields.bank_txn_id))

    resp = await _pay(client, s, day_ids=next_ids)

    assert resp.status_code == 422, resp.text
    assert resp.json()["details"]["failure_codes"] == ["DUPLICATE_BANK_TXN"]
    assert {d.status for d in await _days(db_session, next_ids)} == {"DUE"}


# --------------------------------------------------------------------------- #
# not refused: unsure, or automatic decisions off
# --------------------------------------------------------------------------- #
async def test_with_automatic_decisions_off_a_mismatch_goes_through_and_is_shown(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s, amount_vnd=s["net_vnd"] - 1_000), auto=False)

    resp = await _pay(client, s)

    assert resp.status_code == 200, resp.text
    assert {d.status for d in await _days(db_session, s["day_ids"])} == {"PRESUMED_PAID"}
    [receipt] = await _investor_receipts(client, s)
    assert receipt["system_check"]["result"] == "DOES_NOT_MATCH"
    assert receipt["system_check"]["failure_codes"] == ["AMOUNT_MISMATCH"]
    [row] = await _admin_receipts(client, s)
    check = row["system_check"]
    assert check["verdict"] == "NEEDS_REVIEW" and check["suggested"] == "DENIED"
    assert check["review_reasons"] == ["AUTO_VERDICT_OFF"] and check["failure_codes"] == ["AMOUNT_MISMATCH"]
    assert len(await _audit(db_session, "REPAYMENT_RECEIPT_SYSTEM_NEEDS_REVIEW")) == 1


async def test_an_unsure_read_never_refuses_a_repayment_receipt(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)
    # Read badly, and wrongly: not confident, so it cannot decide anything.
    _use_ocr(_reading(s, confident=False, amount_vnd=1))

    resp = await _pay(client, s)

    assert resp.status_code == 200, resp.text
    [receipt] = await _investor_receipts(client, s)
    assert receipt["system_check"] == {
        "result": "UNSURE", "failure_codes": [], "checked_at": receipt["system_check"]["checked_at"]
    }
    [row] = await _admin_receipts(client, s)
    assert row["system_check"]["verdict"] == "NEEDS_REVIEW" and row["system_check"]["suggested"] is None
    assert row["system_check"]["review_reasons"] == ["LOW_CONFIDENCE:amount_vnd"]
    # An unsure read keeps nothing on the transfers.
    transfers = (await db_session.execute(select(Transfer).where(Transfer.kind == "REPAYMENT"))).scalars().all()
    assert all(t.parsed_fields is None for t in transfers)


async def test_a_receiver_never_verified_at_the_bank_is_left_to_the_investor(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor, verified=False)
    _use_ocr(_reading(s))

    resp = await _pay(client, s)

    assert resp.status_code == 200, resp.text
    [receipt] = await _investor_receipts(client, s)
    assert receipt["system_check"]["result"] == "UNSURE"
    [row] = await _admin_receipts(client, s)
    assert row["system_check"]["review_reasons"] == ["PAYEE_NAME_UNVERIFIED"]


async def test_a_replayed_repayment_upload_is_not_read_again(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _setup(client, db_session, make_admin, make_user, make_eligible_investor)
    reader = _use_ocr(_reading(s))
    key = str(uuid.uuid4())

    assert (await _pay(client, s, key=key)).status_code == 200
    assert (await _pay(client, s, key=key)).status_code == 200

    assert reader.calls == 1
    assert len(await _audit(db_session, "REPAYMENT_RECEIPT_SYSTEM_ACCEPTED")) == 1
