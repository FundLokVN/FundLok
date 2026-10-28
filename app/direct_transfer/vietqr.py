"""Reference-code generator and VietQR (NAPAS 247 / EMVCo MPM) payload builder (HANDOFF-04 B3).

Spec: docs/specs/direct_transfer/transfer-and-evidence.md rules 1-2. Pure functions: no I/O, no clock.
"""
from __future__ import annotations

import secrets
import string

REFERENCE_PREFIX = "FL"
REFERENCE_LENGTH = 8  # shortest memo among target banks is still unchecked (spec section 9, Q3)
_ALPHABET = string.ascii_uppercase + string.digits

NAPAS_AID = "A000000727"
SERVICE_TO_ACCOUNT = "QRIBFTTA"
MAX_MEMO_LENGTH = 50  # conservative bound; typical bank memo fields are 50-210 chars
MAX_AMOUNT_DIGITS = 13


def generate_reference_code(rng: secrets.SystemRandom | None = None) -> str:
    """'FL' + 8 characters from A-Z0-9, drawn from a CSPRNG (never `random`)."""
    chooser = (rng or secrets).choice  # type: ignore[union-attr]
    return REFERENCE_PREFIX + "".join(chooser(_ALPHABET) for _ in range(REFERENCE_LENGTH))


def is_valid_reference_code(code: str) -> bool:
    return (
        len(code) == len(REFERENCE_PREFIX) + REFERENCE_LENGTH
        and code.startswith(REFERENCE_PREFIX)
        and all(c in _ALPHABET for c in code[len(REFERENCE_PREFIX):])
    )


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor. Check value for b'123456789' is 0x29B1."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _tlv(tag: str, value: str) -> str:
    if len(value) > 99:
        raise ValueError(f"TLV value too long for tag {tag}")
    return f"{tag}{len(value):02d}{value}"


def build_vietqr_payload(*, bank_bin: str, account_number: str, amount_vnd: int, memo: str) -> str:
    """Dynamic (amount-fixed) VietQR payload to an account. `amount_vnd` must be a positive integer."""
    if not isinstance(amount_vnd, int) or isinstance(amount_vnd, bool) or amount_vnd <= 0:
        raise ValueError("amount_vnd must be a positive integer")
    if len(str(amount_vnd)) > MAX_AMOUNT_DIGITS:
        raise ValueError("amount_vnd too large")
    if not (bank_bin.isdigit() and len(bank_bin) == 6):
        raise ValueError("bank_bin must be a 6-digit NAPAS BIN")
    if not account_number or len(account_number) > 19 or not account_number.isalnum():
        raise ValueError("invalid account number")
    if not memo or len(memo) > MAX_MEMO_LENGTH or not memo.isascii():
        raise ValueError("memo must be 1-%d ASCII characters" % MAX_MEMO_LENGTH)

    beneficiary = _tlv("00", bank_bin) + _tlv("01", account_number)
    merchant_info = _tlv("00", NAPAS_AID) + _tlv("01", beneficiary) + _tlv("02", SERVICE_TO_ACCOUNT)
    body = (
        _tlv("00", "01")
        + _tlv("01", "12")  # dynamic QR: the amount is fixed
        + _tlv("38", merchant_info)
        + _tlv("53", "704")  # VND
        + _tlv("54", str(amount_vnd))
        + _tlv("58", "VN")
        + _tlv("62", _tlv("08", memo))
        + "6304"
    )
    return body + f"{crc16_ccitt_false(body.encode('ascii')):04X}"


def parse_tlv(payload: str) -> dict[str, str]:
    """Top-level TLV parse (used by tests and by verification of our own payloads)."""
    out: dict[str, str] = {}
    i = 0
    while i < len(payload):
        tag = payload[i : i + 2]
        length = int(payload[i + 2 : i + 4])
        out[tag] = payload[i + 4 : i + 4 + length]
        i += 4 + length
    return out


def verify_crc(payload: str) -> bool:
    if len(payload) < 8 or payload[-8:-4] != "6304":
        return False
    return f"{crc16_ccitt_false(payload[:-4].encode('ascii')):04X}" == payload[-4:]
