"""Configurable parameters for the direct-transfer module.

Every value that is a policy placeholder is tagged PROVISIONAL(Q-Dn); see docs/outstanding-issues.md
(the Q-D series) and docs/specs/direct_transfer/*. Nothing here is hard-coded in service logic: services
take `params: DirectTransferParams | None` and fall back to `DEFAULT_PARAMS`, so tests can override any
value without monkeypatching.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os


@dataclasses.dataclass(frozen=True)
class DirectTransferParams:
    # --- funding window (docs/specs/direct_transfer/funding-window.md) ---
    hold_hours: int = 2
    window_days: int = 14
    # Which flow new windows use: "LEGACY" (reserve, sign and pay within 2 hours) or "COMMIT_THEN_ACCEPT" (ADR-011,
    # Money flow FINAL v1.0). Set by FUNDING_WINDOW_FLOW in the deployment; LEGACY until it is switched on there.
    default_window_flow: str = dataclasses.field(
        default_factory=lambda: os.getenv("FUNDING_WINDOW_FLOW", "LEGACY").strip().upper() or "LEGACY"
    )
    min_raise_pct: int = 50
    min_ticket_vnd: int = 10_000_000  # Q-021 (config, Lok may revisit)
    max_target_vnd: int = 100_000_000
    cross_platform_cap_vnd: int = 100_000_000  # D-054: per legal entity, target plus declared outside debt
    # Deadlines count calendar time, never working days (AM-E20, Loc 15 Nov 2026; holidays ignored, D-046).
    sme_confirm_hours: int = 24  # SME checks a receipt within 24 hours (was dispute_working_days = 2, D-021)
    confirmed_dispute_days: int = 30  # A5: after an automatic confirmation the SME may dispute with a bank statement
    close_wait_days: int = 2  # PROVISIONAL(Q-D4)
    refund_days: int = 2  # Q-020
    working_days_per_month: int = 21  # Q-011 resolved A
    # Automatic extension (docs/specs/repayment_schedule/automatic-extension.md, D-042)
    extension_filing_last_day: int = 5  # the SME files month m by the 5th of month m+1 (ICT)
    extension_days_per_month: int = 21  # D: Δ = ceil(U / due x 21), then up to a multiple of 5
    extension_day_step: int = 5
    extension_basis_days: int = 252  # extra interest P x r x Δ / 252
    stub_basis_days: int = 252  # PROVISIONAL(Q-D5): business days per year
    rate_cap_pct: int = 20  # INV-5 / Dieu 468
    # --- repayment (docs/specs/direct_transfer/repayment-collection.md) ---
    withholding_bps: int = 500  # PROVISIONAL(Q-D6): 5% on interest, individuals only
    # Each investment starts repaying as soon as staff approve it, as its own loan (Phat, 2026-11-02), instead of
    # one pooled loan when the round closes. The round's minimum raise then no longer refunds anyone.
    repay_from_approval: bool = True
    lender_report_hours: int = 24  # Q-026: lender reports a missed day within 24 hours of the due date ending
    late_report_cap_days: int = 30  # PROVISIONAL(Q-D14): the 30-day dispute window after report_deadline
    # --- disputes ---
    dispute_response_days: int = 2  # placeholder
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
    # --- Day-14 transfer rule (docs/specs/direct_transfer/day14-transfer-rule.md, Loc 18 Nov 2026) ---
    qr_validity_minutes: int = 15  # each QR, and its code, is valid this long
    qr_min_interval_seconds: int = 60  # a new QR at most once a minute (QR_TOO_SOON)
    day14_pay_cutoff: dt.time = dt.time(14, 45)  # Day 14, ICT: signing and new QRs close
    day14_no_show_at: dt.time = dt.time(15, 0)  # Day 14, ICT: no-shows and late money are locked
    day14_receipt_cutoff: dt.time = dt.time(15, 30)  # Day 14, ICT: last receipt upload


DEFAULT_PARAMS = DirectTransferParams()


def resolve(params: DirectTransferParams | None) -> DirectTransferParams:
    return params or DEFAULT_PARAMS
