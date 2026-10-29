"""Typed Pydantic response models for Phat's routers (CLAUDE.md: no raw dicts). Shapes are the contract in
docs/specs/direct_transfer/*.md and docs/handoffs/HANDOFF-04-phat-tickets.md. Money is an integer VND everywhere."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

TrancheStatus = Literal["RESERVED", "SIGNED", "ATTESTED", "CONFIRMED", "ACTIVE", "EXPIRED", "UNDER_REVIEW", "VOID"]
WindowStatus = Literal["OPEN", "FULL", "CLOSING", "REPAYING", "FAILED", "REPAID", "CANCELLED"]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorOut(BaseModel):
    detail: str


class FundingWindowOut(_Out):
    id: UUID
    listing_id: UUID
    status: WindowStatus
    target_vnd: int
    min_raise_vnd: int
    min_ticket_vnd: int
    confirmed_vnd: int
    reserved_vnd: int
    capacity_vnd: int
    annual_rate_pct: str
    term_months: int
    window_start: dt.datetime
    window_end: dt.datetime
    days_left: int
    tranche_count: int
    closing_started_at: dt.datetime | None = None
    close_wait_until: dt.datetime | None = None
    repayment_start_date: dt.date | None = None


class TrancheOut(_Out):
    id: UUID
    window_id: UUID
    status: TrancheStatus
    amount_vnd: int
    reference_code: str
    hold_expires_at: dt.datetime
    signed_at: dt.datetime | None = None
    attested_at: dt.datetime | None = None
    confirm_deadline: dt.datetime | None = None
    review_cause: str | None = None
    void_reason: str | None = None


class BankOut(_Out):
    """One entry of the bank dropdown. `bin` is what the account is saved and the VietQR is built with."""
    bin: str
    code: str
    short_name: str
    name: str
    kind: Literal["BANK", "WALLET"]
    popular: bool
    vietqr: bool  # False: the account can be saved, but no VietQR can be made for it


class PaymentAccountOut(_Out):
    id: UUID
    holder_role: Literal["SME", "INVESTOR"]
    holder_type: Literal["INDIVIDUAL", "ENTITY"]
    bank_bin: str
    bank_name: str
    account_number: str
    account_name: str
    status: Literal["PENDING", "APPROVED", "REJECTED"]
    rejection_reason: str | None = None
    created_at: dt.datetime
    approved_at: dt.datetime | None = None


class PaymentOut(_Out):
    bank_name: str
    bank_bin: str
    account_number: str
    account_name: str
    amount_vnd: int
    reference_code: str
    vietqr_payload: str
    hold_expires_at: dt.datetime
    warning: str


class ReceiptResultOut(_Out):
    transfer_id: UUID
    tranche_status: TrancheStatus
    check_status: Literal["AWAITING_FIELDS", "MATCHED", "MISMATCH", "REJECTED", "DUE"]
    failure_codes: list[str]
    confirmation_level: Literal["REPORTED", "ATTESTED", "CONFIRMED", "VERIFIED"]


class PayeeOut(BaseModel):
    bank_name: str
    bank_bin: str
    account_number: str
    account_name: str


class RepaymentRowOut(BaseModel):
    repayment_day_id: UUID
    tranche_id: UUID
    kind: Literal["INSTALMENT", "STUB"]
    investor_display: str
    due_date: dt.date
    principal_vnd: int
    interest_vnd: int
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int
    status: Literal["DUE", "PRESUMED_PAID", "REPORTED_MISSED", "MATCHED"]
    report_deadline: dt.datetime
    payee: PayeeOut | None
    reference_code: str
    vietqr_payload: str | None


class TotalsOut(BaseModel):
    gross_vnd: int
    tax_withheld_vnd: int
    net_vnd: int


class TodaysPaymentsOut(BaseModel):
    date: dt.date
    rows: list[RepaymentRowOut]
    totals: TotalsOut


class DisputeEvidenceOut(_Out):
    party: Literal["INVESTOR", "SME", "STAFF"]
    kind: Literal["CLAIM", "PROOF", "COUNTER_PROOF", "NOTE"]
    statement: str | None = None
    file_id: UUID | None = None
    submitted_at: dt.datetime


class DisputeOut(_Out):
    id: UUID
    subject_type: Literal["TRANCHE", "REPAYMENT_DAY"]
    cause: str
    status: Literal["OPEN", "BANK_TRACE", "DECIDED"]
    required_next_party: Literal["INVESTOR", "SME", "STAFF"]
    response_deadline: dt.datetime | None = None
    outcome: str | None = None
    evidence: list[DisputeEvidenceOut] = []


class FeeInvoiceOut(_Out):
    id: UUID
    kind: Literal["SME_ACTIVATION", "INVESTOR_SERVICE"]
    base_vnd: int
    fee_vnd: int
    vat_vnd: int
    late_fee_vnd: int
    total_vnd: int
    status: Literal["ISSUED", "PAID", "OVERDUE", "VOID"]
    issued_at: dt.datetime
    due_at: dt.datetime
    paid_at: dt.datetime | None = None
    invoice_number: str
    reference_code: str
