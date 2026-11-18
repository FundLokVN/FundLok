"""Day-14 cut-off instants (docs/specs/direct_transfer/day14-transfer-rule.md, Loc 18 Nov 2026).

Pure: no DB, no clock. Kept apart from day14.py so windows.py can show the instants without an import cycle.

On Day 14 (disbursement day), in ICT: signing and new QRs close at 14:45 (`pay_deadline_at`), no-shows and late
money are locked at 15:00 (`no_show_at`), and receipts can be uploaded until 15:30 (`receipt_deadline_at`).
They apply only to windows that carry a `pay_deadline_at` (commit-then-accept windows). LEGACY windows have none:
their lenders pay within the reservation hold on any day (spec section 9, question 2).
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from app.core.timezone import ICT
from app.direct_transfer.config import DirectTransferParams, resolve


@dataclasses.dataclass(frozen=True)
class Day14Cutoffs:
    disbursement_date: dt.date
    pay_deadline_at: dt.datetime
    no_show_at: dt.datetime
    receipt_deadline_at: dt.datetime


def _at_ict(day: dt.date, time: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, time, tzinfo=ICT)


def cutoffs_for_date(disbursement_date: dt.date, params: DirectTransferParams | None = None) -> Day14Cutoffs:
    p = resolve(params)
    return Day14Cutoffs(
        disbursement_date=disbursement_date,
        pay_deadline_at=_at_ict(disbursement_date, p.day14_pay_cutoff),
        no_show_at=_at_ict(disbursement_date, p.day14_no_show_at),
        receipt_deadline_at=_at_ict(disbursement_date, p.day14_receipt_cutoff),
    )


def window_cutoffs(window, params: DirectTransferParams | None = None) -> Day14Cutoffs | None:
    """The window's Day-14 instants, or None for a window without a pay deadline (LEGACY)."""
    if window.pay_deadline_at is None:
        return None
    day = window.pay_deadline_at.astimezone(ICT).date()
    defaults = cutoffs_for_date(day, params)
    return Day14Cutoffs(
        disbursement_date=day,
        pay_deadline_at=window.pay_deadline_at,
        no_show_at=window.no_show_at or defaults.no_show_at,
        receipt_deadline_at=window.receipt_deadline_at or defaults.receipt_deadline_at,
    )


def set_day14_cutoffs(window, disbursement_date: dt.date, params: DirectTransferParams | None = None) -> Day14Cutoffs:
    """Stamp a window with its Day-14 instants. For the commit-then-accept build, when it fixes Day 14."""
    cutoffs = cutoffs_for_date(disbursement_date, params)
    window.pay_deadline_at = cutoffs.pay_deadline_at
    window.no_show_at = cutoffs.no_show_at
    window.receipt_deadline_at = cutoffs.receipt_deadline_at
    return cutoffs
