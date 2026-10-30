"""The typed response models accept what the services return (no raw dicts at the API boundary)."""
import dataclasses

from sqlalchemy import select

from app.direct_transfer import repayment, tranches, windows
from app.direct_transfer.models import Dispute, FeeInvoice
from app.direct_transfer.schemas import (
    DisputeOut,
    FeeInvoiceOut,
    FundingWindowOut,
    PaymentOut,
    ReceiptResultOut,
    TodaysPaymentsOut,
    TrancheOut,
)

from .helpers import at, attested_tranche, make_investor, open_window, upload_receipt, accept, reserve

M = 1_000_000


async def test_schemas_validate_service_outputs(loan):
    world, window, tr = loan
    progress = await windows.get_window_progress(world.db, listing_id=world.listing.id, now=at(days=1))
    out = FundingWindowOut.model_validate(dataclasses.asdict(progress))
    assert out.status == "REPAYING" and out.confirmed_vnd == 100 * M and out.annual_rate_pct == "16.000"
    assert TrancheOut.model_validate(tr[0]).amount_vnd == 10 * M
    today = await repayment.todays_payments(world.db, sme_user_id=world.sme.id, on_date=window.repayment_start_date)
    parsed = TodaysPaymentsOut.model_validate(today)
    assert len(parsed.rows) == 20 and parsed.totals.net_vnd == parsed.totals.gross_vnd - parsed.totals.tax_withheld_vnd
    invoice = (await world.db.execute(select(FeeInvoice))).scalar_one()
    assert FeeInvoiceOut.model_validate(invoice).total_vnd == 3_300_000


async def test_schemas_validate_receipt_payment_and_dispute(world):
    await open_window(world)
    inv = await make_investor(world)
    t = await reserve(world, inv, 10 * M)
    await accept(world, inv, t, now=at(minutes=1))
    pay = await tranches.payment_instructions(world.db, tranche_id=t.id, investor_id=inv.user.id, now=at(minutes=2))
    assert PaymentOut.model_validate(dataclasses.asdict(pay)).reference_code == t.reference_code
    res = await upload_receipt(world, inv, t, now=at(minutes=5))
    assert ReceiptResultOut.model_validate(dataclasses.asdict(res)).tranche_status == "ATTESTED"
    await tranches.sme_dispute_tranche(world.db, tranche_id=t.id, sme_user_id=world.sme.id, statement="x", file_id=None, now=at(minutes=6), params=world.params)
    from sqlalchemy.orm import selectinload

    d = (await world.db.execute(select(Dispute).options(selectinload(Dispute.evidence)))).scalar_one()
    out = DisputeOut.model_validate(d)
    assert out.required_next_party == "INVESTOR" and out.evidence[0].kind == "CLAIM"
