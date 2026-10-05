"""Idempotency-Key handling for money endpoints (app/payments/).

Spec: docs/specs/payments/payout-idempotency.md, section 6.
Semantics follow the IETF draft "The Idempotency-Key HTTP Header Field":

- The header is REQUIRED on every endpoint that moves or commits money
  (400 if missing or malformed).
- Same actor + endpoint + key + same request  -> the stored response is
  returned again, with `Idempotent-Replayed: true`. Nothing runs twice.
- Same actor + endpoint + key + different request -> 422.
- Same key while the first request is still running -> the second waits on
  the unique index (bounded by MONEY_LOCK_TIMEOUT_MS) and then replays, or
  gets 409 with Retry-After if the first is still holding it.

How: the claim row is INSERTed ... ON CONFLICT DO NOTHING at the start of the
request and completed with the response before COMMIT, inside the same
transaction as the money write. PostgreSQL makes a second inserter of the
same unique key wait for the first transaction to finish, so "claim visible"
and "effect committed" are the same event. If the request fails, the
rollback removes the claim and a retry with the same key runs normally.

Keys are scoped per actor, so one user's key can never replay another
user's response.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.payments.models import IdempotencyRecord

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
REPLAY_HEADER = "Idempotent-Replayed"

# PostgreSQL SQLSTATEs for "gave up waiting for a lock".
_LOCK_TIMEOUT_SQLSTATES = {"55P03", "57014"}


def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required for this request")
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must be 8-128 characters: letters, digits, '-', '_', ':' or '.'",
        )
    return idempotency_key


def request_fingerprint(*, scope: str, path_params: dict[str, Any] | None = None, body: Any = None) -> str:
    """sha256 over a canonical JSON rendering of what the request asks for.
    Header noise, key order and whitespace do not change it."""
    canonical = json.dumps(
        {"scope": scope, "path": path_params or {}, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def set_money_lock_timeout(db: AsyncSession) -> None:
    """Bound every lock wait in this transaction, so a stuck request turns
    into a fast 409 instead of a pile-up of held connections."""
    await db.execute(text(f"SET LOCAL lock_timeout = '{int(settings.MONEY_LOCK_TIMEOUT_MS)}ms'"))


def is_lock_timeout(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code in _LOCK_TIMEOUT_SQLSTATES:
            return True
    return False


def busy_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="Another request for the same operation is still being processed; retry shortly",
        headers={"Retry-After": "2"},
    )


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: Any


@dataclass
class IdempotencyClaim:
    db: AsyncSession
    record_id: UUID | None = None
    # Plain values, not the ORM row: the session is rolled back on replay,
    # which expires every loaded instance.
    replay: StoredResponse | None = None
    _completed: bool = field(default=False, repr=False)

    def replay_response(self) -> JSONResponse:
        assert self.replay is not None
        return JSONResponse(
            status_code=self.replay.status_code,
            content=self.replay.body,
            headers={REPLAY_HEADER: "true"},
        )

    async def complete(self, *, status_code: int, body: dict[str, Any]) -> None:
        """Store the response on the claim. Call right before COMMIT, in the
        same transaction as the effect."""
        assert self.record_id is not None, "cannot complete a replayed claim"
        await self.db.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == self.record_id)
            .values(response_status=status_code, response_body=body)
        )
        self._completed = True


async def claim_idempotency_key(
    db: AsyncSession, *, actor_id: UUID, scope: str, key: str, request_hash: str
) -> IdempotencyClaim:
    await set_money_lock_timeout(db)
    stmt = (
        pg_insert(IdempotencyRecord)
        .values(actor_id=actor_id, scope=scope, idempotency_key=key, request_hash=request_hash)
        .on_conflict_do_nothing(constraint="uq_idempotency_records_actor_scope_key")
        .returning(IdempotencyRecord.id)
    )
    try:
        record_id = (await db.execute(stmt)).scalar_one_or_none()
    except DBAPIError as exc:
        if is_lock_timeout(exc):
            raise busy_conflict() from exc
        raise
    if record_id is not None:
        return IdempotencyClaim(db=db, record_id=record_id)

    # Conflict: a committed claim exists (we waited for it on the unique
    # index). Read it in a fresh statement snapshot.
    existing = (
        await db.execute(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == key,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if existing.request_hash != request_hash:
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key was already used with a different request",
        )
    if existing.response_body is None or existing.response_status is None:
        raise busy_conflict()
    return IdempotencyClaim(
        db=db, replay=StoredResponse(status_code=existing.response_status, body=existing.response_body)
    )


@asynccontextmanager
async def idempotent_request(
    db: AsyncSession,
    *,
    actor_id: UUID,
    scope: str,
    key: str,
    path_params: dict[str, Any] | None = None,
    body: Any = None,
) -> AsyncIterator[IdempotencyClaim]:
    """Claim `key` for this request; roll everything back if the body of the
    `async with` raises, so a failed attempt leaves no claim behind and a
    retry with the same key runs fresh.

        async with idempotent_request(db, actor_id=..., scope=..., key=..., body=...) as claim:
            if claim.replay:
                return claim.replay_response()
            ...money write...
            await claim.complete(status_code=202, body=out)
            await db.commit()
    """
    try:
        claim = await claim_idempotency_key(
            db,
            actor_id=actor_id,
            scope=scope,
            key=key,
            request_hash=request_fingerprint(scope=scope, path_params=path_params, body=body),
        )
        if claim.replay is not None:
            await db.rollback()
        yield claim
    except BaseException:
        await db.rollback()
        raise
