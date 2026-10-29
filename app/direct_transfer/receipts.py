"""Receipt fields, pure receipt checks, and the extractor interface (HANDOFF-04 B4).

Spec: docs/specs/direct_transfer/transfer-and-evidence.md rules 3-9. Pure: no DB, no clock, no network.
No receipt may be sent to an external OCR/AI service (I-14, Q-025): `ReceiptExtractor` is the only extraction
seam and the MVP implementation (`ManualEntryExtractor`) extracts nothing -- staff enter the fields.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
import unicodedata
from typing import Protocol

# Failure codes (stable strings; the UI maps them to plain language).
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
REFERENCE_MISSING = "REFERENCE_MISSING"
PAYEE_MISMATCH = "PAYEE_MISMATCH"
PAYER_NAME_MISMATCH = "PAYER_NAME_MISMATCH"
TIME_OUT_OF_RANGE = "TIME_OUT_OF_RANGE"
MISSING_FIELD = "MISSING_FIELD"  # reported as MISSING_FIELD:<name>

REQUIRED_FIELDS = (
    "amount_vnd",
    "transferred_at",
    "payer_name",
    "payer_account",
    "payee_account",
    "memo",
    "bank_txn_id",
)


@dataclasses.dataclass(frozen=True)
class ReceiptFields:
    amount_vnd: int | None
    transferred_at: dt.datetime | None
    payer_name: str | None
    payer_account: str | None
    payee_account: str | None
    memo: str | None
    bank_txn_id: str | None
    payee_name: str | None = None

    def to_json(self) -> dict:
        return {
            "amount_vnd": self.amount_vnd,
            "transferred_at": self.transferred_at.isoformat() if self.transferred_at else None,
            "payer_name": self.payer_name,
            "payer_account": self.payer_account,
            "payee_account": self.payee_account,
            "memo": self.memo,
            "bank_txn_id": self.bank_txn_id,
            "payee_name": self.payee_name,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ReceiptFields":
        ts = data.get("transferred_at")
        return cls(
            amount_vnd=data.get("amount_vnd"),
            transferred_at=dt.datetime.fromisoformat(ts) if ts else None,
            payer_name=data.get("payer_name"),
            payer_account=data.get("payer_account"),
            payee_account=data.get("payee_account"),
            memo=data.get("memo"),
            bank_txn_id=data.get("bank_txn_id"),
            payee_name=data.get("payee_name"),
        )


@dataclasses.dataclass(frozen=True)
class ExpectedTransfer:
    amount_vnd: int
    reference_code: str
    payee_account_number: str
    payer_kyc_name: str
    reserved_at: dt.datetime  # a transfer cannot predate the reservation that issued the reference


class ReceiptExtractor(Protocol):
    async def extract(self, *, content: bytes, content_type: str) -> ReceiptFields | None:
        """Return the fields read from the receipt, or None if they could not be read (staff will enter them)."""
        ...


class ManualEntryExtractor:
    """MVP extractor: reads nothing. The transfer waits for staff manual entry (HANDOFF-04 I-14, E5)."""

    async def extract(self, *, content: bytes, content_type: str) -> ReceiptFields | None:
        return None


def normalize_name(name: str) -> str:
    """PROVISIONAL(Q-D13): diacritic-folded, case-insensitive, whitespace-collapsed form of a person's name."""
    text = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^0-9A-Za-z]+", " ", text).strip().upper()
    return re.sub(r"\s+", " ", text)


def _normalize_memo(memo: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKD", memo).upper())


def _normalize_account(account: str) -> str:
    return re.sub(r"[\s\-.]", "", account).upper()


def missing_fields(fields: ReceiptFields) -> list[str]:
    missing = []
    for name in REQUIRED_FIELDS:
        value = getattr(fields, name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


def check_receipt(
    fields: ReceiptFields,
    expected: ExpectedTransfer,
    *,
    now: dt.datetime,
    future_skew: dt.timedelta = dt.timedelta(minutes=5),
) -> list[str]:
    """Return the list of failure codes; an empty list means the receipt matches the QR FundLok issued."""
    failures: list[str] = [f"{MISSING_FIELD}:{name}" for name in missing_fields(fields)]
    if failures:
        # A receipt missing any required field is rejected outright (Loc.md receipt checks).
        return failures

    if not isinstance(fields.amount_vnd, int) or fields.amount_vnd != expected.amount_vnd:
        failures.append(AMOUNT_MISMATCH)
    if _normalize_memo(expected.reference_code) not in _normalize_memo(fields.memo or ""):
        failures.append(REFERENCE_MISSING)
    if _normalize_account(fields.payee_account or "") != _normalize_account(expected.payee_account_number):
        failures.append(PAYEE_MISMATCH)
    if normalize_name(fields.payer_name or "") != normalize_name(expected.payer_kyc_name):
        failures.append(PAYER_NAME_MISMATCH)
    ts = fields.transferred_at
    if ts is not None:
        if ts.tzinfo is None:
            failures.append(TIME_OUT_OF_RANGE)
        elif ts < expected.reserved_at or ts > now + future_skew:
            failures.append(TIME_OUT_OF_RANGE)
    return failures
