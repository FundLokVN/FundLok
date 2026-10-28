"""Spec: docs/specs/direct_transfer/transfer-and-evidence.md section 8 (B4 pure checks)."""
import dataclasses
import datetime as dt

import pytest

from app.core.timezone import ICT
from app.direct_transfer.receipts import (
    AMOUNT_MISMATCH,
    PAYEE_MISMATCH,
    PAYER_NAME_MISMATCH,
    REFERENCE_MISSING,
    TIME_OUT_OF_RANGE,
    ExpectedTransfer,
    ManualEntryExtractor,
    ReceiptFields,
    check_receipt,
    normalize_name,
)

NOW = dt.datetime(2026, 11, 2, 10, 0, tzinfo=ICT)


def expected():
    return ExpectedTransfer(
        amount_vnd=10_000_000,
        reference_code="FLAB12CD34",
        payee_account_number="19036789012",
        payer_kyc_name="Nguyễn Văn Đạt",
        reserved_at=NOW - dt.timedelta(minutes=30),
    )


def good():
    return ReceiptFields(
        amount_vnd=10_000_000,
        transferred_at=NOW - dt.timedelta(minutes=10),
        payer_name="NGUYEN VAN DAT",
        payer_account="0011223344",
        payee_account="19036789012",
        memo="chuyen tien FLAB12CD34 cho vay",
        bank_txn_id="FT26306000123",
    )


def test_receipt_check_happy_path_matches():
    assert check_receipt(good(), expected(), now=NOW) == []


def test_receipt_check_amount_must_be_exact():
    f = dataclasses.replace(good(), amount_vnd=10_000_001)
    assert check_receipt(f, expected(), now=NOW) == [AMOUNT_MISMATCH]


def test_receipt_check_memo_must_carry_reference_ignoring_case_and_spaces():
    ok = dataclasses.replace(good(), memo="fl ab12-cd34 loan")
    assert check_receipt(ok, expected(), now=NOW) == []
    bad = dataclasses.replace(good(), memo="no reference here")
    assert check_receipt(bad, expected(), now=NOW) == [REFERENCE_MISSING]


def test_receipt_check_payee_must_be_sme_registered_account():
    f = dataclasses.replace(good(), payee_account="99999999")
    assert check_receipt(f, expected(), now=NOW) == [PAYEE_MISMATCH]


def test_receipt_check_payer_name_diacritic_folded_case_insensitive():
    for name in ("Nguyễn Văn Đạt", "nguyen van dat", "NGUYEN  VAN  ĐẠT", "Nguyen-Van Dat"):
        f = dataclasses.replace(good(), payer_name=name)
        assert check_receipt(f, expected(), now=NOW) == [], name
    assert normalize_name("Đặng Thị Ánh") == "DANG THI ANH"


def test_receipt_check_payer_name_mismatch_flags():
    f = dataclasses.replace(good(), payer_name="TRAN THI B")
    assert check_receipt(f, expected(), now=NOW) == [PAYER_NAME_MISMATCH]


@pytest.mark.parametrize(
    "field", ["amount_vnd", "transferred_at", "payer_name", "payer_account", "payee_account", "memo", "bank_txn_id"]
)
def test_receipt_check_missing_required_fields_rejected(field):
    f = dataclasses.replace(good(), **{field: None})
    assert check_receipt(f, expected(), now=NOW) == [f"MISSING_FIELD:{field}"]
    blank = dataclasses.replace(good(), **{field: ""}) if field not in ("amount_vnd", "transferred_at") else f
    assert check_receipt(blank, expected(), now=NOW)[0].startswith("MISSING_FIELD")


def test_receipt_check_time_before_reservation_flagged():
    early = dataclasses.replace(good(), transferred_at=NOW - dt.timedelta(hours=3))
    assert check_receipt(early, expected(), now=NOW) == [TIME_OUT_OF_RANGE]
    future = dataclasses.replace(good(), transferred_at=NOW + dt.timedelta(hours=1))
    assert check_receipt(future, expected(), now=NOW) == [TIME_OUT_OF_RANGE]


async def test_manual_entry_extractor_returns_none():
    assert await ManualEntryExtractor().extract(content=b"%PDF", content_type="application/pdf") is None


def test_fields_round_trip_json():
    f = good()
    assert ReceiptFields.from_json(f.to_json()) == f
