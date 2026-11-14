"""Receipts read by OCR and decided by the system, and staff looking at them again
(docs/specs/direct_transfer/receipt-ocr.md).

A confident read that matches confirms the ticket at once, but its loan waits for staff; one that does not match
rejects the receipt so the investor can upload the right one; anything unsure waits for staff as before. Staff can
approve or deny any receipt the system decided, keeping or reversing it, once. The OCR engine is replaced by a fake
reader returning a chosen reading (tests/uploads/test_bank_receipt_ocr.py covers the reading itself).
"""
import dataclasses
import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.direct_transfer.models import EvidenceLog, Tranche, Transfer
from app.direct_transfer.receipts import ReceiptFields
from app.lending.models import AuditLog
from app.main import app
from app.market.receipt_auto import identify_bank, receiver_names_match
from app.uploads.receipt_ocr import ReceiptOcr, ReceiptReading, receipt_ocr
from tests.market.test_funding_flow import PNG, SME_ACCOUNT, _lapse, _open_project, _reserve, _upload


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


def _reading(tranche, *, confident: bool = True, payee_bank: str | None = "Techcombank", **overrides) -> ReceiptReading:
    fields = ReceiptFields(
        amount_vnd=tranche["amount_vnd"],
        transferred_at=dt.datetime.now(dt.timezone.utc),
        payer_name="NGUYEN VAN AN",
        payer_account="0123456789",
        payee_account=SME_ACCOUNT,
        memo=f"{tranche['reference_code']} chuyen tien",
        bank_txn_id=f"FT{uuid.uuid4().hex[:12].upper()}",
        payee_name="CONG TY ABC",
    )
    fields = dataclasses.replace(fields, **overrides)
    return ReceiptReading(
        engine="fake-ocr",
        fields=fields,
        field_confidence={name: 95.0 for name in fields.to_json()},
        confidence=95.0 if confident else 41.0,
        confident=confident,
        notes=() if confident else ("LOW_CONFIDENCE:memo",),
        payee_bank=payee_bank,  # the SME's account is at Techcombank (970407), held by "CONG TY ABC"
    )


async def _reserved(client, db_session, make_admin, make_user, make_eligible_investor, amount=20_000_000):
    setup = await _open_project(client, db_session, make_admin, make_user)
    window_id = setup["window"].id
    investor = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Nguyễn Văn An"))
    tranche = (await _reserve(client, investor, window_id, amount)).json()
    return setup | {"window_id": window_id, "investor": investor, "tranche": tranche}


async def _row(client, s):
    resp = await client.get(f"/admin/funding/transfers/{s['tranche']['id']}", headers=s["admin"]["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _decide(client, s, transfer_id, outcome, reason=None):
    body = {"outcome": outcome} | ({"reason": reason} if reason else {})
    return await client.post(
        f"/admin/funding/transfers/{transfer_id}/decision", json=body, headers=s["admin"]["headers"]
    )


async def _bell(client, sme, event):
    items = (await client.get("/notifications", headers=sme["headers"])).json()["items"]
    return [n for n in items if n["event"] == event]


async def _events(db_session, transfer_id):
    rows = (
        await db_session.execute(
            select(EvidenceLog)
            .where(EvidenceLog.entity_type == "TRANSFER", EvidenceLog.entity_id == uuid.UUID(transfer_id))
            .order_by(EvidenceLog.created_at)
        )
    ).scalars().all()
    return rows


async def _auto_accepted(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    reading = _reading(s["tranche"])
    _use_ocr(reading)
    upload = await _upload(client, s["investor"], s["tranche"]["id"], PNG + uuid.uuid4().bytes)
    assert upload.status_code == 202, upload.text
    assert upload.json()["tranche_status"] == "CONFIRMED"
    return s | {"transfer_id": upload.json()["transfer_id"], "reading": reading}


async def _auto_denied(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    # The OCR misread one digit of the amount.
    reading = _reading(s["tranche"], amount_vnd=s["tranche"]["amount_vnd"] - 1_000_000)
    _use_ocr(reading)
    upload = await _upload(client, s["investor"], s["tranche"]["id"], PNG + uuid.uuid4().bytes)
    assert upload.status_code == 202, upload.text
    assert upload.json()["check_status"] == "REJECTED"
    return s | {"transfer_id": upload.json()["transfer_id"], "reading": reading}


# --------------------------------------------------------------------------- #
# the automatic paths
# --------------------------------------------------------------------------- #
async def test_with_ocr_off_every_receipt_waits_for_staff(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.status_code == 202 and resp.json()["check_status"] == "AWAITING_FIELDS"
    events = await _events(db_session, resp.json()["transfer_id"])
    assert [e.event_type for e in events] == ["RECEIPT_UPLOADED"]
    assert (await _row(client, s))["transfer"]["system_check"] is None


async def test_a_matching_receipt_is_confirmed_automatically_and_its_loan_waits_for_staff(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    sent = []
    monkeypatch.setattr("app.utils.email.send_email", lambda **kw: sent.append(kw))
    s = await _auto_accepted(client, db_session, make_admin, make_user, make_eligible_investor)

    db_session.expire_all()
    transfer = await db_session.get(Transfer, uuid.UUID(s["transfer_id"]))
    tranche = await db_session.get(Tranche, uuid.UUID(s["tranche"]["id"]))
    assert transfer.check_status == "MATCHED" and transfer.confirmation_level == "CONFIRMED"
    assert transfer.bank_txn_id == s["reading"].fields.bank_txn_id
    assert transfer.parsed_fields["amount_vnd"] == 20_000_000
    assert tranche.status == "CONFIRMED" and tranche.holding_id is None  # no loan until staff look

    # The reading, the checks and the verdict are on record, by the system (no person).
    events = await _events(db_session, s["transfer_id"])
    assert [e.event_type for e in events] == [
        "RECEIPT_UPLOADED", "RECEIPT_OCR_READ", "FIELDS_ENTERED", "CHECKS_RUN", "SYSTEM_VERDICT"
    ]
    verdict = events[-1]
    assert verdict.actor_id is None and verdict.actor_role == "SYSTEM"
    assert verdict.payload["verdict"] == "ACCEPTED" and verdict.payload["applied"] is True
    assert "matches" in verdict.reason
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "TRANSFER_SYSTEM_ACCEPTED"))).scalar_one()
    assert audit.actor_id is None and audit.after_state["tranche_status"] == "CONFIRMED"

    # The business hears it was approved (not merely submitted), and is still emailed to look for the money.
    [ring] = await _bell(client, s["sme"], "INVESTMENT_TRANSFER_APPROVED")
    assert ring["data"]["status"] == "CONFIRMED"
    assert await _bell(client, s["sme"], "INVESTMENT_RECEIPT_SUBMITTED") == []
    assert len(sent) == 1


async def test_a_receipt_that_does_not_match_is_denied_and_the_investor_can_upload_another(
    client, db_session, make_admin, make_user, make_eligible_investor, monkeypatch
):
    sent = []
    monkeypatch.setattr("app.utils.email.send_email", lambda **kw: sent.append(kw))
    s = await _auto_denied(client, db_session, make_admin, make_user, make_eligible_investor)

    db_session.expire_all()
    transfer = await db_session.get(Transfer, uuid.UUID(s["transfer_id"]))
    assert transfer.failure_codes == ["AMOUNT_MISMATCH"]
    assert transfer.bank_txn_id is None  # not claimed: a correct receipt of the same transfer may still come
    mine = (await client.get("/funding/me/tranches", headers=s["investor"]["headers"])).json()
    assert mine[0]["status"] == "SIGNED" and mine[0]["receipt_submitted_at"] is None
    # Nothing for the business: the receipt was rejected.
    assert sent == [] and await _bell(client, s["sme"], "INVESTMENT_RECEIPT_SUBMITTED") == []
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "TRANSFER_SYSTEM_DENIED"))).scalar_one()
    assert audit.after_state["failure_codes"] == ["AMOUNT_MISMATCH"]

    _use_ocr(_reading(s["tranche"], bank_txn_id=s["reading"].fields.bank_txn_id))
    again = await _upload(client, s["investor"], s["tranche"]["id"], PNG + uuid.uuid4().bytes)

    assert again.status_code == 202, again.text
    assert again.json()["tranche_status"] == "CONFIRMED" and again.json()["check_status"] == "MATCHED"


async def test_an_unsure_read_is_left_for_staff_with_the_reading_shown(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s["tranche"], confident=False))

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.json()["check_status"] == "AWAITING_FIELDS" and resp.json()["tranche_status"] == "SIGNED"
    row = await _row(client, s)
    check = row["transfer"]["system_check"]
    assert check["verdict"] == "NEEDS_REVIEW" and check["applied"] is False and check["suggested"] is None
    assert check["review_reasons"] == ["LOW_CONFIDENCE:memo"]
    assert check["fields"]["memo"].startswith(s["tranche"]["reference_code"]) and check["engine"] == "fake-ocr"
    assert check["reviewable"] is False and row["needs_action"] is True  # the receipt waits, as before OCR

    approved = await _decide(client, s, resp.json()["transfer_id"], "APPROVED")
    assert approved.status_code == 200 and approved.json()["status"] == "ACTIVE"


async def test_with_automatic_decisions_off_the_reading_is_only_a_suggestion(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s["tranche"]), auto=False)

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.json()["check_status"] == "AWAITING_FIELDS"
    check = (await _row(client, s))["transfer"]["system_check"]
    assert check["verdict"] == "NEEDS_REVIEW" and check["suggested"] == "ACCEPTED"
    assert check["review_reasons"] == ["AUTO_VERDICT_OFF"]


async def test_a_bank_transaction_that_already_funds_a_ticket_is_denied(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_accepted(client, db_session, make_admin, make_user, make_eligible_investor)
    other = await make_eligible_investor(await make_user(role="INVESTOR", full_name="Nguyễn Văn An"))
    second = (await _reserve(client, other, s["window_id"], 10_000_000)).json()
    _use_ocr(_reading(second, bank_txn_id=s["reading"].fields.bank_txn_id))

    resp = await _upload(client, other, second["id"], PNG + uuid.uuid4().bytes)

    assert resp.json()["check_status"] == "REJECTED" and resp.json()["failure_codes"] == ["DUPLICATE_BANK_TXN"]


async def test_a_late_receipt_is_read_but_left_for_staff(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    await _lapse(db_session, s["tranche"]["id"])
    _use_ocr(_reading(s["tranche"]))

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.json()["tranche_status"] == "UNDER_REVIEW" and resp.json()["check_status"] == "AWAITING_FIELDS"
    check = (await _row(client, s))["transfer"]["system_check"]
    assert check["verdict"] == "NEEDS_REVIEW" and check["suggested"] == "ACCEPTED"
    assert check["review_reasons"] == ["LATE_RECEIPT"]


async def test_a_replayed_upload_is_not_read_again(client, db_session, make_admin, make_user, make_eligible_investor):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    reader = _use_ocr(_reading(s["tranche"]))
    key = str(uuid.uuid4())

    first = await _upload(client, s["investor"], s["tranche"]["id"], key=key)
    again = await _upload(client, s["investor"], s["tranche"]["id"], key=key)

    assert first.status_code == again.status_code == 202
    assert again.json()["transfer_id"] == first.json()["transfer_id"]
    assert reader.calls == 1


# --------------------------------------------------------------------------- #
# what staff see
# --------------------------------------------------------------------------- #
async def test_staff_see_the_verdict_and_the_ocr_evidence_and_the_ticket_waits_for_them(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_accepted(client, db_session, make_admin, make_user, make_eligible_investor)

    row = await _row(client, s)

    assert row["status"] == "CONFIRMED" and row["needs_action"] is True
    check = row["transfer"]["system_check"]
    assert check["verdict"] == "ACCEPTED" and check["applied"] is True
    assert check["awaiting_review"] is True and check["reviewable"] is True and check["reviewed_at"] is None
    assert check["failure_codes"] == [] and check["review_reasons"] == []
    assert check["fields"]["payee_account"] == SME_ACCOUNT and check["field_confidence"]["amount_vnd"] == 95.0
    assert check["confidence"] == 95.0 and check["engine"] == "fake-ocr"

    headers = s["admin"]["headers"]
    queue = (await client.get("/admin/funding/transfers", params={"status": "NEEDS_ACTION"}, headers=headers)).json()
    assert [r["id"] for r in queue["items"]] == [s["tranche"]["id"]]
    decided = (await client.get("/admin/funding/transfers", params={"status": "SYSTEM_DECIDED"}, headers=headers)).json()
    assert [r["id"] for r in decided["items"]] == [s["tranche"]["id"]]
    [round_] = (await client.get("/admin/funding/rounds", headers=headers)).json()
    assert round_["needs_action"] == 1
    activity = (await client.get("/admin/funding/activity", headers=headers)).json()["items"]
    [entry] = [a for a in activity if a["action"] == "TRANSFER_SYSTEM_ACCEPTED"]
    assert entry["actor"] is None and entry["tranche_id"] == s["tranche"]["id"]


# --------------------------------------------------------------------------- #
# the receiver: bank, account number and holder name against the business's verified account
# --------------------------------------------------------------------------- #
def test_banks_are_identified_however_the_receipt_names_them():
    for text in ("Techcombank", "TCB", "Ngân hàng TMCP Kỹ thương Việt Nam", "NH TMCP KY THUONG VIET NAM (TECHCOMBANK)"):
        assert identify_bank(text).bin == "970407", text
    assert identify_bank("MB Bank").short_name == "MBBank"
    assert identify_bank("Vietcombank").bin == "970436"
    assert identify_bank("Ngân hàng của tôi") is None


def test_holder_names_match_across_abbreviations_and_truncation():
    assert receiver_names_match("CTY CP ABC", "CÔNG TY CỔ PHẦN ABC")
    assert receiver_names_match("CONG TY TNHH MOT THANH VIEN XYZ", "CTY TNHH MTV XYZ")
    assert receiver_names_match("CONG TY CO PHAN DAU TU VA PHA", "CÔNG TY CỔ PHẦN ĐẦU TƯ VÀ PHÁT TRIỂN ABC")  # cut off
    assert not receiver_names_match("NGUYEN VAN B", "CONG TY ABC")
    assert not receiver_names_match("CTY ABD", "CONG TY ABC")


async def test_the_receiver_is_compared_and_shown_to_staff(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s["tranche"], payee_name="CTY ABC"))  # the bank abbreviates the company name

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.json()["tranche_status"] == "CONFIRMED"
    row = await _row(client, s)
    assert row["expected"]["payee_holder_name"] == "CONG TY ABC"
    receiver = row["transfer"]["system_check"]["receiver"]
    assert receiver == {
        "bank_read": "Techcombank", "bank_identified": "Techcombank", "bank_identified_bin": "970407",
        "expected_bank": "Techcombank", "expected_bank_bin": "970407",
        "account_read": SME_ACCOUNT, "expected_account": SME_ACCOUNT,
        "holder_read": "CTY ABC", "expected_holder": "CONG TY ABC",
    }
    assert row["transfer"]["parsed_fields"]["payee_bank"] == "Techcombank"


@pytest.mark.parametrize(
    "overrides, failure",
    [
        ({"payee_bank": "Vietcombank"}, "PAYEE_BANK_MISMATCH"),  # the same number at another bank
        ({"payee_name": "NGUYEN VAN B"}, "PAYEE_NAME_MISMATCH"),  # someone else's account
        ({"payee_account": "19036789099"}, "PAYEE_MISMATCH"),
    ],
)
async def test_a_receipt_paid_to_the_wrong_receiver_is_denied(
    client, db_session, make_admin, make_user, make_eligible_investor, overrides, failure
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s["tranche"], **overrides))

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.json()["check_status"] == "REJECTED" and resp.json()["failure_codes"] == [failure]
    assert resp.json()["tranche_status"] == "SIGNED"


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"payee_bank": None}, "PAYEE_BANK_NOT_SHOWN"),
        ({"payee_bank": "Ngân hàng của tôi"}, "PAYEE_BANK_UNRECOGNIZED"),
        ({"payee_name": None}, "PAYEE_NAME_NOT_SHOWN"),
    ],
)
async def test_a_receiver_the_receipt_does_not_confirm_is_left_for_staff(
    client, db_session, make_admin, make_user, make_eligible_investor, overrides, reason
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s["tranche"], **overrides))

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.json()["check_status"] == "AWAITING_FIELDS"
    check = (await _row(client, s))["transfer"]["system_check"]
    assert check["verdict"] == "NEEDS_REVIEW" and check["suggested"] is None
    assert check["review_reasons"] == [reason]


async def test_a_wrong_detail_denies_even_when_the_receiving_bank_is_not_shown(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _reserved(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s["tranche"], payee_bank=None, amount_vnd=1_000_000))

    resp = await _upload(client, s["investor"], s["tranche"]["id"])

    assert resp.json()["check_status"] == "REJECTED" and resp.json()["failure_codes"] == ["AMOUNT_MISMATCH"]


# --------------------------------------------------------------------------- #
# staff override
# --------------------------------------------------------------------------- #
async def test_staff_approving_a_receipt_the_system_accepted_starts_its_loan(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_accepted(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _decide(client, s, s["transfer_id"], "APPROVED", "Checked the SME's statement")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["status"] == "ACTIVE" and row["needs_action"] is False
    check = row["transfer"]["system_check"]
    assert check["review_outcome"] == "APPROVED" and check["overrides_system"] is False
    assert check["reviewed_by"]["id"] == s["admin"]["id"] and check["review_reason"] == "Checked the SME's statement"
    assert check["reviewable"] is False and check["awaiting_review"] is False
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "TRANSFER_RECEIVED"))).scalar_one()
    assert audit.after_state["system_verdict"] == "ACCEPTED" and audit.after_state["overrides_system"] is False
    assert len(await _bell(client, s["sme"], "INVESTMENT_TRANSFER_APPROVED")) == 1  # not told twice

    again = await _decide(client, s, s["transfer_id"], "DENIED")
    assert again.status_code == 409


async def test_staff_denying_a_receipt_the_system_accepted_voids_the_ticket(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_accepted(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _decide(client, s, s["transfer_id"], "DENIED", "No such credit on the SME's statement")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["status"] == "VOID" and row["void_reason"] == "NOT_RECEIVED" and row["needs_action"] is False
    check = row["transfer"]["system_check"]
    assert check["review_outcome"] == "DENIED" and check["overrides_system"] is True
    events = await _events(db_session, s["transfer_id"])
    review = events[-1]
    assert review.event_type == "SYSTEM_VERDICT_REVIEWED" and str(review.actor_id) == s["admin"]["id"]
    assert review.payload == {"system_verdict": "ACCEPTED", "outcome": "DENIED", "overrides_system": True}
    [round_] = (await client.get("/admin/funding/rounds", headers=s["admin"]["headers"])).json()
    assert round_["confirmed_vnd"] == 0


async def test_staff_approving_a_receipt_the_system_denied_records_it_received(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_denied(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _decide(client, s, s["transfer_id"], "APPROVED", "OCR misread the amount; the receipt shows 20M")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["status"] == "ACTIVE"
    assert row["transfer"]["check_status"] == "MATCHED" and row["transfer"]["failure_codes"] == []
    assert row["transfer"]["system_check"]["overrides_system"] is True
    db_session.expire_all()
    transfer = await db_session.get(Transfer, uuid.UUID(s["transfer_id"]))
    assert transfer.bank_txn_id == s["reading"].fields.bank_txn_id  # now it funds this ticket
    [ring] = await _bell(client, s["sme"], "INVESTMENT_TRANSFER_APPROVED")
    assert ring["entity_id"] == s["tranche"]["id"]


async def test_staff_can_approve_a_denied_receipt_after_the_hold_lapsed(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_denied(client, db_session, make_admin, make_user, make_eligible_investor)
    await _lapse(db_session, s["tranche"]["id"])
    await client.get("/admin/funding/rounds", headers=s["admin"]["headers"])  # releases the lapsed hold
    assert (await _row(client, s))["status"] == "EXPIRED"

    resp = await _decide(client, s, s["transfer_id"], "APPROVED", "Money is in the SME's statement")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ACTIVE"


async def test_staff_denying_a_receipt_the_system_denied_keeps_the_rejection(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_denied(client, db_session, make_admin, make_user, make_eligible_investor)

    resp = await _decide(client, s, s["transfer_id"], "DENIED")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["status"] == "SIGNED" and row["transfer"]["check_status"] == "REJECTED"
    check = row["transfer"]["system_check"]
    assert check["review_outcome"] == "DENIED" and check["overrides_system"] is False and check["reviewable"] is False
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "TRANSFER_NOT_RECEIVED"))).scalar_one()
    assert audit.after_state["system_verdict"] == "DENIED" and audit.after_state["reason"] == "Denied after viewing the receipt"

    again = await _decide(client, s, s["transfer_id"], "APPROVED")
    assert again.status_code == 409


async def test_an_accepted_receipt_cannot_be_denied_once_its_loan_started(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_accepted(client, db_session, make_admin, make_user, make_eligible_investor)
    advanced = await client.post(f"/admin/funding/rounds/{s['window_id']}/advance", headers=s["admin"]["headers"])
    assert advanced.status_code == 200, advanced.text
    assert (await _row(client, s))["status"] == "ACTIVE"

    denied = await _decide(client, s, s["transfer_id"], "DENIED")
    assert denied.status_code == 409 and denied.headers["X-Error-Code"] == "LOAN_STARTED"

    kept = await _decide(client, s, s["transfer_id"], "APPROVED")
    assert kept.status_code == 200 and kept.json()["transfer"]["system_check"]["review_outcome"] == "APPROVED"


async def test_a_denied_receipt_replaced_by_a_newer_one_is_reviewed_through_the_newer_one(
    client, db_session, make_admin, make_user, make_eligible_investor
):
    s = await _auto_denied(client, db_session, make_admin, make_user, make_eligible_investor)
    _use_ocr(_reading(s["tranche"], confident=False))
    newer = await _upload(client, s["investor"], s["tranche"]["id"], PNG + uuid.uuid4().bytes)
    assert newer.json()["check_status"] == "AWAITING_FIELDS"

    old = await _decide(client, s, s["transfer_id"], "APPROVED")

    assert old.status_code == 409 and old.headers["X-Error-Code"] == "SUPERSEDED"
    assert (await _row(client, s))["transfer"]["id"] == newer.json()["transfer_id"]
