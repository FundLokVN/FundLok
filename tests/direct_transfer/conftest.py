import datetime as dt

import pytest

from .helpers import at, close_now, fund_window, make_world


@pytest.fixture
async def world(db_session):
    return await make_world(db_session)


@pytest.fixture
async def loan(world):
    """A fully funded, closed 100M loan: 10 investors x 10M, repayment starts Wed 4 Nov 2026."""
    tr = await fund_window(world, [10_000_000] * 10)
    window = await close_now(world, now=at(days=1))
    assert window.repayment_start_date == dt.date(2026, 11, 4)
    return world, window, tr
