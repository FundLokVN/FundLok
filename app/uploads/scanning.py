"""Malware scanning of uploads with ClamAV (compliance audit SEC-04).

Before SEC-04 nothing scanned anything: `commit_file` set SCANNING and no
code ever moved it on. Now, with UPLOAD_SCAN_ENABLED on, a confirmed upload
waits in SCANNING while the ARQ worker runs `scan_upload`, which streams the
object from R2 to clamd and records the verdict:

    clean          -> UPLOADED     reviewers can open it, the application can
                                   be submitted (both already require UPLOADED)
    malware found  -> INFECTED     the object is deleted
    no verdict     -> SCAN_FAILED  clamd unreachable after every retry, the
                                   object missing, or the file over clamd's
                                   size limits. Not called clean.

Each outcome writes an UPLOAD_SCANNED audit row. Files are streamed, never
held in memory, because a VAT zip can be 200 MB. If the job is never queued
(Redis down at confirm) or the worker dies mid-scan, `requeue_stalled_scans`
queues it again.

The scan runs after confirm, so for the three document types the backend
parses at confirm (e-invoice zip, tax filings, CIC report) parsing comes
first. The parsers are hardened for hostile input already; what the scan
protects is the person who opens the file.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from arq import Retry
from arq.connections import ArqRedis, RedisSettings, create_pool
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.lending.models import Document, LoanApplicationDocument
from app.utils.audit import append_audit
from app.utils.r2 import delete_object, iter_object

logger = logging.getLogger(__name__)

SCANNING = "SCANNING"
CLEAN = "UPLOADED"
INFECTED = "INFECTED"
SCAN_FAILED = "SCAN_FAILED"

SCAN_ACTION = "UPLOAD_SCANNED"
SCAN_JOB = "scan_upload"
# Below WorkerSettings.max_tries (ARQ's default 5), so the last try is ours
# to record as SCAN_FAILED rather than ARQ silently dropping the job.
MAX_TRIES = 4
RETRY_DELAY_SECONDS = 60
STALLED_AFTER = timedelta(minutes=15)

# What clamd reports, instead of a malware name, for a file over its
# configured limits (AlertExceedsMax yes). Unscanned, so not clean.
_LIMIT_PREFIX = "Heuristics.Limits.Exceeded"

# kind -> (model, attribute holding the storage key, audit entity type)
KINDS = {
    "application_document": (LoanApplicationDocument, "file_key", "LOAN_APPLICATION_DOCUMENT"),
    "document": (Document, "storage_key", "DOCUMENT"),
}


class ScannerUnavailable(Exception):
    """clamd gave no verdict: unreachable, timed out, or answered an error."""


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    signature: str | None = None
    limit_exceeded: bool = False


# --------------------------------------------------------------------------- #
# The clamd client: INSTREAM over TCP. Synchronous; the job runs it in a thread.
# --------------------------------------------------------------------------- #


def _read_reply(sock: socket.socket) -> str:
    reply = b""
    while not reply.endswith(b"\0") and len(reply) < 4096:
        chunk = sock.recv(4096)
        if not chunk:
            break
        reply += chunk
    return reply.rstrip(b"\0").decode("utf-8", "replace").strip()


def _verdict(reply: str) -> ScanResult:
    # "stream: OK" | "stream: <name> FOUND" | "<message> ERROR"
    if reply.endswith(" FOUND"):
        name = reply.removeprefix("stream:").removesuffix(" FOUND").strip()
        if name.startswith(_LIMIT_PREFIX):
            return ScanResult(clean=False, signature=name, limit_exceeded=True)
        return ScanResult(clean=False, signature=name)
    if reply.endswith(" OK"):
        return ScanResult(clean=True)
    if "size limit exceeded" in reply.lower():
        return ScanResult(clean=False, signature="StreamMaxLength", limit_exceeded=True)
    raise ScannerUnavailable(f"clamd answered: {reply or '<nothing>'}")


def scan_chunks(chunks: Iterable[bytes]) -> ScanResult:
    """Stream `chunks` to clamd and return its verdict."""
    try:
        with socket.create_connection(
            (settings.CLAMAV_HOST, settings.CLAMAV_PORT), timeout=settings.CLAMAV_TIMEOUT_SECONDS
        ) as sock:
            sock.sendall(b"zINSTREAM\0")
            try:
                for chunk in chunks:
                    if chunk:
                        sock.sendall(struct.pack("!I", len(chunk)) + chunk)
                sock.sendall(struct.pack("!I", 0))
            except (BrokenPipeError, ConnectionResetError):
                # clamd hangs up mid-stream once a file passes StreamMaxLength;
                # its reason is still waiting to be read. Closing with our
                # unread data in its buffer makes the kernel send a reset, so
                # on Linux (CI, Cloud Run) the send usually fails with
                # ECONNRESET rather than EPIPE.
                pass
            return _verdict(_read_reply(sock))
    except OSError as exc:
        raise ScannerUnavailable(f"{type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Queueing, from the API
# --------------------------------------------------------------------------- #

_pool: ArqRedis | None = None


async def queue_scan(kind: str, document_id: UUID) -> None:
    """Queue a scan for a document just set to SCANNING. Never fails the
    request: if Redis is down the row stays SCANNING and
    `requeue_stalled_scans` queues it later."""
    global _pool
    try:
        if _pool is None:
            _pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await _pool.enqueue_job(SCAN_JOB, kind, str(document_id))
    except Exception:
        logger.exception("Could not queue %s scan for %s; the stalled-scan sweep will retry", kind, document_id)


# --------------------------------------------------------------------------- #
# Worker jobs
# --------------------------------------------------------------------------- #


def _scan_object(key: str) -> ScanResult:
    return scan_chunks(iter_object(key))


async def scan_upload(ctx: dict, kind: str, document_id: str) -> str:
    """ARQ job: scan one SCANNING document and record the verdict."""
    model, key_attr, entity_type = KINDS[kind]
    async with SessionLocal() as db:
        doc = await db.get(model, UUID(document_id))
        if doc is None or doc.status != SCANNING:
            return "skipped"  # re-uploaded, deleted or already scanned
        key = getattr(doc, key_attr)

        try:
            result = await asyncio.to_thread(_scan_object, key)
        except ClientError as exc:
            # Most often the object is gone. Nothing to retry.
            return await _record(db, doc, entity_type, SCAN_FAILED, reason=f"storage: {exc.response.get('Error', {}).get('Code')}")
        except (ScannerUnavailable, BotoCoreError) as exc:
            job_try = ctx.get("job_try", 1)
            if job_try < MAX_TRIES:
                logger.warning("Scan of %s %s deferred (try %s): %s", kind, document_id, job_try, exc)
                raise Retry(defer=RETRY_DELAY_SECONDS * job_try) from exc
            return await _record(db, doc, entity_type, SCAN_FAILED, reason=str(exc))

        if result.clean:
            return await _record(db, doc, entity_type, CLEAN)
        if result.limit_exceeded:
            return await _record(db, doc, entity_type, SCAN_FAILED, reason=result.signature)
        await asyncio.to_thread(delete_object, key)
        return await _record(db, doc, entity_type, INFECTED, signature=result.signature)


async def _record(db, doc, entity_type: str, status: str, **detail) -> str:
    doc.status = status
    append_audit(
        db,
        entity_type=entity_type,
        entity_id=doc.id,
        action=SCAN_ACTION,
        actor_id=None,
        after_state={"status": status, **{k: v for k, v in detail.items() if v}},
    )
    await db.commit()
    if status != CLEAN:
        logger.warning("Upload %s %s: %s", entity_type, doc.id, {"status": status, **detail})
    return status.lower()


async def requeue_stalled_scans(ctx: dict) -> int:
    """ARQ cron: queue again any document SCANNING for longer than
    STALLED_AFTER. Covers a confirm that could not reach Redis and a worker
    that died mid-scan. A duplicate is harmless: a job finding its row no
    longer SCANNING does nothing."""
    cutoff = datetime.now(timezone.utc) - STALLED_AFTER
    queued = 0
    async with SessionLocal() as db:
        for kind, (model, _, _) in KINDS.items():
            ids = (
                await db.execute(select(model.id).where(model.status == SCANNING, model.updated_at < cutoff))
            ).scalars()
            for document_id in ids:
                await ctx["redis"].enqueue_job(SCAN_JOB, kind, str(document_id))
                queued += 1
    if queued:
        logger.warning("Re-queued %s stalled upload scan(s)", queued)
    return queued
