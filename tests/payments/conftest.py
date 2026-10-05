import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import engine


@pytest.fixture
def session_factory():
    """Independent sessions, one per unit of work, as the dispatcher and the
    concurrency tests need -- the shared `db_session` cannot model two
    transactions racing each other."""
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def approved_instruction(payout_ready, create_disbursement, approve_disbursement):
    """A disbursement instruction that a second admin has approved, ready for
    the dispatcher. Returns (setup, instruction_json)."""

    async def _make(target_amount: str = "10000.00"):
        setup = await payout_ready(target_amount=target_amount)
        created = await create_disbursement(setup, "approved-fixture-create")
        assert created.status_code == 202, created.text
        approved = await approve_disbursement(created.json()["id"], setup["checker"], "approved-fixture-approve")
        assert approved.status_code == 200, approved.text
        return setup, approved.json()

    return _make
