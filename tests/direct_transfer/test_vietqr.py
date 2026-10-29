"""Spec: docs/specs/direct_transfer/transfer-and-evidence.md section 8 (B3)."""
import re

import pytest

from app.direct_transfer.vietqr import (
    build_vietqr_payload,
    crc16_ccitt_false,
    generate_reference_code,
    is_valid_reference_code,
    parse_tlv,
    verify_crc,
)


def test_reference_code_format_and_uniqueness():
    codes = {generate_reference_code() for _ in range(500)}
    assert len(codes) == 500
    assert all(re.fullmatch(r"FL[A-Z0-9]{8}", c) for c in codes)
    assert all(is_valid_reference_code(c) for c in codes)
    assert not is_valid_reference_code("FL1234567")  # too short
    assert not is_valid_reference_code("XX12345678")


def test_reference_code_uses_csprng_not_random(monkeypatch):
    import random

    monkeypatch.setattr(random, "choice", lambda *_: (_ for _ in ()).throw(AssertionError("random used")))
    assert is_valid_reference_code(generate_reference_code())


def test_vietqr_crc16_known_vector():
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_vietqr_payload_structure_and_crc_round_trip():
    payload = build_vietqr_payload(bank_bin="970407", account_number="19036789012", amount_vnd=10_000_000, memo="FLAB12CD34")
    assert payload.startswith("000201010212")
    assert verify_crc(payload)
    top = parse_tlv(payload)
    assert top["53"] == "704" and top["54"] == "10000000" and top["58"] == "VN"
    merchant = parse_tlv(top["38"])
    assert merchant["00"] == "A000000727" and merchant["02"] == "QRIBFTTA"
    beneficiary = parse_tlv(merchant["01"])
    assert beneficiary == {"00": "970407", "01": "19036789012"}
    assert parse_tlv(top["62"])["08"] == "FLAB12CD34"
    # tampering breaks the CRC
    assert not verify_crc(payload.replace("10000000", "10000001"))


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(amount_vnd=0),
        dict(amount_vnd=-5),
        dict(amount_vnd=1.5),
        dict(memo="x" * 200),
        dict(memo=""),
        dict(bank_bin="1234"),
        dict(account_number=""),
    ],
)
def test_vietqr_payload_rejects_non_positive_amount_and_long_memo(kwargs):
    base = dict(bank_bin="970407", account_number="123456789", amount_vnd=1000, memo="FLAB12CD34")
    base.update(kwargs)
    with pytest.raises(ValueError):
        build_vietqr_payload(**base)
