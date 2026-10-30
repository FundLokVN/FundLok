"""Bank account-holder name lookup: the hook for automatic verification (PROVISIONAL(Q-D18), Q-029).

The standard (Edward and Loc, 2026-10-30): FundLok name = KYC/KYB name = BANK account holder name. The name a user
types when saving an account is self-declared, so it proves nothing. The holder name must come from the bank:
today a staff member looks it up (types the bank and account number into a bank app's transfer screen, which
shows the holder without moving money) and records it when approving (accounts.decide_payment_account).

A `BankNameLookup` automates that step: given a bank BIN and account number it returns the holder name from the
bank (NAPAS 247 account-name inquiry, through a provider), or None when it cannot tell. It returns a name only:
no balances, no transactions. No provider is chosen or connected yet, so the default `NoLookup` always says None
and every account waits for staff. Providers are a vendor, cost and Loc decision (Q-029).
"""
from __future__ import annotations

from typing import Protocol


class BankNameLookup(Protocol):
    async def lookup(self, *, bank_bin: str, account_number: str) -> str | None:
        """The account holder's name at the bank, or None (unsupported bank, account not found, provider down)."""
        ...


class NoLookup:
    async def lookup(self, *, bank_bin: str, account_number: str) -> str | None:
        return None
