"""Phase 2 slice S1: investor_agreement_codes can belong to a window (no tranche yet) and carry a purpose.
Spec: docs/specs/direct_transfer/commit-then-accept-flow.md section 3."""
import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from app.market.investor_agreement.models import AgreementSigningCode
from tests.direct_transfer.helpers import T0, make_investor, make_world, open_window, reserve


def _code(investor, **kw):
    base = dict(
        investor_id=investor.user.id, version="v1", phone="+84900000000", code_hash="h", channel="log",
        expires_at=T0 + dt.timedelta(minutes=10),
    )
    base.update(kw)
    return AgreementSigningCode(**base)


async def test_agreement_code_purpose_check_and_loan_contract_requires_tranche(db_session):
    world = await make_world(db_session)
    window = await open_window(world)
    investor = await make_investor(world)
    tranche = await reserve(world, investor, 10_000_000)

    # Existing shape: a tranche code defaults to SERVICE_AGREEMENT.
    existing = _code(investor, tranche_id=tranche.id)
    db_session.add(existing)
    await db_session.flush()
    await db_session.refresh(existing)
    assert existing.purpose == "SERVICE_AGREEMENT" and existing.window_id is None

    # New: a service-agreement code for a window with no tranche yet, and a loan-contract code with a tranche.
    db_session.add(_code(investor, window_id=window.id))
    db_session.add(_code(investor, tranche_id=tranche.id, window_id=window.id, purpose="LOAN_CONTRACT"))
    await db_session.flush()

    for bad in (dict(purpose="OTHER", tranche_id=tranche.id), dict(purpose="LOAN_CONTRACT", window_id=window.id)):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(_code(investor, **bad))
                await db_session.flush()
