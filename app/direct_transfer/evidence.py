"""Append-only evidence log writer and evidence file storage (HANDOFF-04 B1/B4).

Spec: docs/specs/direct_transfer/transfer-and-evidence.md. The only write path to `evidence_log` is
`log_event` (INSERT); there is deliberately no update or delete function here or anywhere in the module.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.direct_transfer.config import DirectTransferParams, resolve
from app.direct_transfer.errors import DirectTransferError, conflict
from app.direct_transfer.models import EvidenceFile, EvidenceLog
from app.direct_transfer.storage import EvidenceStorage

EVIDENCE_PURPOSES = (
    "RECEIPT",
    "BANK_STATEMENT",
    "SME_STATEMENT",
    "INVESTOR_STATEMENT",
    "SIGNED_DOCUMENT",
    "BANK_CONFIRMATION",
    "DECLARATION",
    "INVOICE",
    "OTHER",
)


async def log_event(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID,
    event_type: str,
    now: dt.datetime,
    actor_id: UUID | None = None,
    actor_role: str | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    file_sha256: str | None = None,
) -> EvidenceLog:
    """Append one evidence row. `now` is injected (no wall-clock reads)."""
    row = EvidenceLog(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_role=actor_role,
        reason=reason,
        payload=payload or {},
        file_sha256=file_sha256,
        occurred_at=now,
    )
    db.add(row)
    await db.flush()
    return row


async def store_evidence_file(
    db: AsyncSession,
    storage: EvidenceStorage,
    *,
    owner_user_id: UUID,
    purpose: str,
    content: bytes,
    content_type: str,
    filename: str | None = None,
    params: DirectTransferParams | None = None,
) -> EvidenceFile:
    """Validate, hash and store a customer-uploaded document.

    A RECEIPT whose SHA-256 already exists is rejected (409 DUPLICATE_FILE): one file funds at most one
    tranche. The DB partial unique index is the authoritative control; the pre-check gives a clean error.
    """
    p = resolve(params)
    if purpose not in EVIDENCE_PURPOSES:
        raise DirectTransferError(400, "Invalid evidence purpose", "INVALID_PURPOSE")
    if content_type not in p.allowed_evidence_types:
        raise DirectTransferError(415, "Unsupported file type", "UNSUPPORTED_TYPE")
    if len(content) == 0:
        raise DirectTransferError(400, "Empty file", "EMPTY_FILE")
    if len(content) > p.max_evidence_bytes:
        raise DirectTransferError(413, "File too large", "FILE_TOO_LARGE")

    digest = hashlib.sha256(content).hexdigest()
    if purpose == "RECEIPT":
        existing = (
            await db.execute(select(EvidenceFile.id).where(EvidenceFile.purpose == "RECEIPT", EvidenceFile.sha256 == digest))
        ).first()
        if existing is not None:
            raise conflict("DUPLICATE_FILE", "This receipt was already used")

    file_id = uuid.uuid4()
    key = f"evidence/{purpose}/{digest[:2]}/{file_id}"
    try:
        await storage.put(key, content, content_type)
    except DirectTransferError:
        raise
    except Exception as exc:  # storage backend failure: do not leak internals
        raise DirectTransferError(503, "Storage unavailable", "STORAGE_UNAVAILABLE") from exc

    record = EvidenceFile(
        id=file_id,
        owner_user_id=owner_user_id,
        purpose=purpose,
        storage_key=key,
        sha256=digest,
        size_bytes=len(content),
        content_type=content_type,
        original_filename=filename,
    )
    db.add(record)
    await db.flush()
    return record


async def timeline(db: AsyncSession, *, entity_type: str, entity_id: UUID) -> list[EvidenceLog]:
    result = await db.execute(
        select(EvidenceLog)
        .where(EvidenceLog.entity_type == entity_type, EvidenceLog.entity_id == entity_id)
        .order_by(EvidenceLog.occurred_at, EvidenceLog.created_at)
    )
    return list(result.scalars())
