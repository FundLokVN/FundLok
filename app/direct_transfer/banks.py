"""Vietnamese banks and e-wallets by NAPAS BIN (HANDOFF-04: users save a payment account first).

Static reference data in data/vn_banks.json (source and cross-check recorded in the file; regenerate with
scripts/refresh_vn_banks.py). The BIN is what goes into the VietQR payload, so an unknown or mistyped BIN would
send a payer to the wrong bank: registration validates it here and takes the bank name from this list, never from
the client. E-wallets are listed but off by default (`kind == "WALLET"`): Decree 94 allows them, but the receipt and
statement evidence flow has only been designed for bank accounts. PROVISIONAL(Q-D17).
"""
from __future__ import annotations

import dataclasses
import json
from functools import lru_cache
from pathlib import Path

from app.direct_transfer.errors import bad_request

DATA_FILE = Path(__file__).parent / "data" / "vn_banks.json"


@dataclasses.dataclass(frozen=True)
class Bank:
    bin: str
    code: str
    short_name: str
    name: str
    kind: str  # "BANK" | "WALLET"
    popular: bool


@lru_cache(maxsize=1)
def _load() -> tuple[Bank, ...]:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return tuple(Bank(**row) for row in data["banks"])


def list_banks(*, include_wallets: bool = False) -> list[Bank]:
    """Popular banks first (in the file's order), then the rest A-Z."""
    banks = [b for b in _load() if include_wallets or b.kind == "BANK"]
    popular = [b for b in banks if b.popular]
    rest = sorted((b for b in banks if not b.popular), key=lambda b: b.short_name.lower())
    return popular + rest


def get_bank(bank_bin: str) -> Bank | None:
    return next((b for b in _load() if b.bin == bank_bin), None)


def require_bank(bank_bin: str, *, allow_wallets: bool = False) -> Bank:
    bank = get_bank(bank_bin)
    if bank is None or (bank.kind == "WALLET" and not allow_wallets):
        raise bad_request("Unsupported bank")
    return bank
