"""Configurable parameters for the direct-transfer module.

Every value that is a policy placeholder is tagged PROVISIONAL(Q-Dn); see docs/outstanding-issues.md
(the Q-D series) and docs/specs/direct_transfer/*. Nothing here is hard-coded in service logic: services
take `params: DirectTransferParams | None` and fall back to `DEFAULT_PARAMS`, so tests can override any
value without monkeypatching.
"""
from __future__ import annotations

import dataclasses
import os


@dataclasses.dataclass(frozen=True)
class DirectTransferParams:
    # --- funding window (docs/specs/direct_transfer/funding-window.md) ---
    hold_hours: int = 2
    window_days: int = 14
    min_raise_pct: int = 50
    min_ticket_vnd: int = 10_000_000  # Q-021 (config, Lok may revisit)
    max_target_vnd: int = 100_000_000
    cross_platform_cap_vnd: int = 400_000_000  # PROVISIONAL(Q-D12)
    dispute_working_days: int = 2  # SME dispute window on disbursement (placeholder, D-021)
    close_wait_working_days: int = 2  # PROVISIONAL(Q-D4)
    refund_working_days: int = 2  # Q-020
    working_days_per_month: int = 21  # Q-011 resolved A
    stub_basis_days: int = 252  # PROVISIONAL(Q-D5): business days per year
    rate_cap_pct: int = 20  # INV-5 / Dieu 468
    # --- repayment (docs/specs/direct_transfer/repayment-collection.md) ---
    withholding_bps: int = 500  # PROVISIONAL(Q-D6): 5% on interest, individuals only
    missed_day_report_working_days: int = 2  # Q-026
    late_report_cap_working_days: int = 5  # PROVISIONAL(Q-D14)
    # --- disputes ---
    dispute_response_working_days: int = 2  # placeholder
    # --- fees (docs/specs/direct_transfer/fees.md); LIVE by default, E1 / D-023 ---
    fees_enabled: bool = True
    sme_fee_bps: int = 300
    investor_fee_bps_per_year: int = 250
    vat_bps: int = 1000  # PROVISIONAL(Q-D10)
    sme_invoice_due_days: int = 7
    investor_invoice_due_days: int = 7  # PROVISIONAL(Q-D15)
    late_fee_bps_per_day: int = 0  # PROVISIONAL(Q-D11)
    # FundLok's OWN revenue account for invoice QR codes. Not custody: it only ever receives FundLok fees.
    fee_account_bin: str | None = dataclasses.field(default_factory=lambda: os.getenv("FUNDLOK_FEE_BANK_BIN"))
    fee_account_number: str | None = dataclasses.field(default_factory=lambda: os.getenv("FUNDLOK_FEE_ACCOUNT_NUMBER"))
    fee_account_name: str | None = dataclasses.field(default_factory=lambda: os.getenv("FUNDLOK_FEE_ACCOUNT_NAME"))
    # --- evidence ---
    max_evidence_bytes: int = 10 * 1024 * 1024
    allowed_evidence_types: tuple[str, ...] = ("application/pdf", "image/png", "image/jpeg", "image/webp")
    receipt_future_skew_minutes: int = 5


DEFAULT_PARAMS = DirectTransferParams()


def resolve(params: DirectTransferParams | None) -> DirectTransferParams:
    return params or DEFAULT_PARAMS
