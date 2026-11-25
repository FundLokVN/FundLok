"""The app's connection pool survives a connection the server closed while it sat idle.

Production's database (Neon) closes idle connections. The pool then hands the next request a dead one, and the first
request after a quiet spell failed with `asyncpg ... InterfaceError: connection is closed`, in the maintenance-mode
middleware every request passes through (seen on prod-api, 23 Nov 2026, on POST /internal/jobs/repayment-day).
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import engine


async def test_a_connection_the_server_closed_while_idle_is_replaced_not_served():
    async with engine.connect() as conn:  # a connection goes back to the pool, idle
        pid = (await conn.execute(text("select pg_backend_pid()"))).scalar_one()

    killer = create_async_engine(engine.url)  # another engine, so it cannot be the pooled connection itself
    try:
        async with killer.connect() as other:
            assert (await other.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})).scalar_one() is True
    finally:
        await killer.dispose()

    async with engine.connect() as conn:  # must get a working connection, not the dead one
        assert (await conn.execute(text("select 1"))).scalar_one() == 1
        assert (await conn.execute(text("select pg_backend_pid()"))).scalar_one() != pid


def test_the_pool_checks_connections_and_recycles_them_before_the_server_closes_them():
    assert engine.pool._pre_ping is True
    assert 0 < engine.pool._recycle <= 300  # Neon closes idle connections after about five minutes
