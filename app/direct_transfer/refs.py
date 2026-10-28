"""Unique reference-code allocation (shared by tranches and fee invoices so a memo is never ambiguous)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.errors import DirectTransferError
from app.direct_transfer.models import FeeInvoice, Tranche
from app.direct_transfer.vietqr import generate_reference_code


async def unique_reference_code(db: AsyncSession, *, attempts: int = 10) -> str:
    for _ in range(attempts):
        code = generate_reference_code()
        in_tranches = (await db.execute(select(Tranche.id).where(Tranche.reference_code == code))).first()
        in_invoices = (await db.execute(select(FeeInvoice.id).where(FeeInvoice.reference_code == code))).first()
        if in_tranches is None and in_invoices is None:
            return code
    raise DirectTransferError(503, "Could not allocate a reference code", "REFERENCE_EXHAUSTED")
