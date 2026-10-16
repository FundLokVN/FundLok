from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def advisory_xact_lock(db: AsyncSession, key: str) -> None:
    """Serialise callers sharing `key` until the current transaction ends.

    For count-then-act limits (attempt counters read from audit_logs): without
    it, concurrent requests all read the count before any of them writes, so a
    burst gets N tries instead of the limit. Released on commit or rollback.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )
