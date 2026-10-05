"""Bank-agnostic payout gateway seam (app/banking/).

Spec: docs/specs/payments/payout-idempotency.md

The payout state machine (app/payments/payouts.py) talks only to the
`PayoutGateway` protocol below. The adapter for the custodial bank partner --
request signing, mTLS, the static-egress proxy -- implements it in a later
change; tests use a fake. Per ADR-002/003 the bank's name stays out of this
interface and out of the database.

The one rule every adapter must follow: classify each failure by whether the
request could have reached the bank.

    NOT_SENT  -- provably never left: DNS failure, TCP connect refused or
                 timed out, TLS handshake or proxy CONNECT failure. Safe to
                 send again (with the same client_request_id).
    UNKNOWN   -- anything after the request bytes may have been written: read
                 timeout, connection reset, 5xx, unparseable body. NEVER
                 resend blind; the dispatcher asks the bank by
                 client_request_id instead.

When in doubt, an adapter must return UNKNOWN. Misclassifying UNKNOWN as
NOT_SENT is the one mistake that can pay twice.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from app.core.config import settings


class SubmitOutcome(str, enum.Enum):
    ACCEPTED = "ACCEPTED"  # bank took the instruction; final status comes later
    SUCCEEDED = "SUCCEEDED"  # bank confirmed the transfer in the response
    REJECTED = "REJECTED"  # bank definitively refused it; nothing moved
    NOT_SENT = "NOT_SENT"  # the request provably never reached the bank
    UNKNOWN = "UNKNOWN"  # it may have reached the bank; outcome not known


class InquiryOutcome(str, enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PENDING = "PENDING"
    NOT_FOUND = "NOT_FOUND"  # bank has no record of this client_request_id
    ERROR = "ERROR"  # the inquiry itself failed; try again later


@dataclass(frozen=True)
class TransferRequest:
    client_request_id: str
    amount: int  # integer VND
    beneficiary_bank_bin: str
    beneficiary_account_number: str
    beneficiary_account_name: str
    description: str


@dataclass(frozen=True)
class SubmitResult:
    outcome: SubmitOutcome
    bank_transaction_ref: str | None = None
    bank_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class InquiryResult:
    outcome: InquiryOutcome
    bank_transaction_ref: str | None = None
    bank_code: str | None = None
    message: str | None = None


class PayoutGateway(Protocol):
    async def submit_transfer(self, request: TransferRequest) -> SubmitResult: ...

    async def inquire_transfer(self, client_request_id: str) -> InquiryResult: ...


def get_payout_gateway() -> PayoutGateway | None:
    """The configured adapter, or None when payouts are not wired to a bank
    (the default). Background jobs do nothing while this returns None."""
    name = settings.PAYOUT_GATEWAY
    if not name:
        return None
    raise RuntimeError(f"No payout gateway adapter is registered for PAYOUT_GATEWAY={name!r}")
