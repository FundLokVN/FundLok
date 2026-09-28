"""The bank reference rate every public estimate is priced on.

One source: the `bank_rate_config` row in force, the same row admin score runs
read (app/underwriting/service.py::get_current_bank_rate). The rate is changed
by adding a row (POST /underwriting/bank-rate), never in code: the
`GradingInput.bank_rate_pct` dataclass default is a convenience for
hand-built test inputs only, and no production path may price on it.

No fallback. A missing row is a configuration error; quoting a visitor on a
rate that is not the one they would be scored on is worse than a brief outage,
so the calculator answers 503 and the error is logged.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.underwriting.service import get_current_bank_rate

logger = logging.getLogger(__name__)


async def bank_rate_in_force(db: AsyncSession) -> float:
    try:
        row = await get_current_bank_rate(db)
    except HTTPException as exc:
        logger.error("No bank_rate_config row in force; refusing to price an estimate: %s", exc.detail)
        raise HTTPException(
            status_code=503,
            detail="The rate calculator is temporarily unavailable",
            headers={"X-Error-Code": "BANK_RATE_UNAVAILABLE"},
        ) from exc
    return float(row.rate_pct)
