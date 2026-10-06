"""Retention of verification documents in R2.

Every KYC/KYB attempt's submitted documents are stored under a deterministic
prefix keyed by the attempt id, so the evidence for any verdict (including
FAILED attempts) can be located without a DB column:

    {R2_VERIFICATION_BUCKET or R2_BUCKET}/
        verification/KYC/{verification_id}/id_front.{jpg|png}
        verification/KYC/{verification_id}/id_back.{jpg|png}
        verification/KYC/{verification_id}/portrait.{jpg|png}
        verification/KYB/{verification_id}/certificate.{jpg|png|pdf}

Uploads are best-effort: a storage outage must never block or fail the
verification itself (the provider payloads in Postgres remain the primary
audit record). Failures are logged and swallowed. boto3 is sync, so uploads
run in a worker thread to keep the event loop free.
"""
from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.utils import r2

logger = logging.getLogger(__name__)

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF"


def _bucket() -> str | None:
    return settings.R2_VERIFICATION_BUCKET or settings.R2_BUCKET


def _ext_and_type(raw: bytes) -> tuple[str, str]:
    if raw.startswith(_PNG_MAGIC):
        return "png", "image/png"
    if raw.startswith(_PDF_MAGIC):
        return "pdf", "application/pdf"
    # Callers validate magic bytes before we get here; JPEG is the default.
    return "jpg", "image/jpeg"


def _put(key: str, raw: bytes) -> None:
    _, content_type = _ext_and_type(raw)
    r2.put_bytes(key, raw, content_type, bucket=_bucket())


class StorageUnavailable(RuntimeError):
    """Documents could not be stored, in a mode where storing them is the
    point (manual review: the files are the only thing an admin can judge)."""


async def _store(kind: str, verification_id, documents: dict[str, bytes], *, strict: bool = False) -> None:
    """Best-effort by default (see module docstring). With strict=True a
    failure raises StorageUnavailable instead: in manual verification mode no
    provider looks at the documents, so an attempt whose files did not land
    could never be reviewed and must not be accepted."""
    if not r2.is_configured():
        if strict:
            raise StorageUnavailable("object storage is not configured")
        logger.warning(
            "R2 not configured — %s documents for %s not retained", kind, verification_id
        )
        return
    try:
        for name, raw in documents.items():
            ext, _ = _ext_and_type(raw)
            key = f"verification/{kind}/{verification_id}/{name}.{ext}"
            await asyncio.to_thread(_put, key, raw)
    except Exception as exc:  # noqa: BLE001 — retention must never break verification
        logger.exception(
            "Failed to store %s verification documents for %s", kind, verification_id
        )
        if strict:
            raise StorageUnavailable(str(exc)) from exc


async def store_kyc_documents(
    verification_id, *, id_front: bytes, id_back: bytes, portrait: bytes, strict: bool = False
) -> None:
    await _store(
        "KYC",
        verification_id,
        {"id_front": id_front, "id_back": id_back, "portrait": portrait},
        strict=strict,
    )


async def store_kyb_document(verification_id, *, certificate: bytes, strict: bool = False) -> None:
    await _store("KYB", verification_id, {"certificate": certificate}, strict=strict)


# The extensions _ext_and_type can produce. The chosen one is NOT recorded in
# Postgres — the key is derived from the bytes at upload time — so a reader has
# to probe. Three HEADs at worst, and only when someone opens the document.
_CERTIFICATE_EXTENSIONS = (("pdf", "application/pdf"), ("jpg", "image/jpeg"), ("png", "image/png"))

# Ten minutes: long enough to open and read a certificate, short enough that a
# leaked link (a pasted URL, a shared screenshot) stops working quickly. The
# object itself stays private — this is the only way to reach it.
CERTIFICATE_URL_TTL_SECONDS = 600


def find_kyb_certificate(verification_id) -> tuple[str, str] | None:
    """Locate a stored KYB certificate: (object key, content type), or None.

    None is a normal answer, not an error. Retention is best-effort by design
    (see the module docstring): an attempt can exist with no stored object if
    R2 was unconfigured or erroring at the time, and the caller has to render
    that case rather than assume the file is there.
    """
    if not r2.is_configured():
        return None
    bucket = _bucket()
    for ext, content_type in _CERTIFICATE_EXTENSIONS:
        key = f"verification/KYB/{verification_id}/certificate.{ext}"
        if r2.head_object(key, bucket=bucket) is not None:
            return key, content_type
    return None


def presign_kyb_certificate(key: str) -> str:
    """A short-lived read URL for a located certificate."""
    return r2.presign_get(
        key, bucket=_bucket(), expires_in=CERTIFICATE_URL_TTL_SECONDS
    )


# The three images a KYC attempt stores (see store_kyc_documents), in the order
# a reviewer reads them. Same probing as the certificate: the extension is
# derived from the bytes at upload time and not recorded.
KYC_DOCUMENT_NAMES = ("id_front", "id_back", "portrait")
_KYC_IMAGE_EXTENSIONS = (("jpg", "image/jpeg"), ("png", "image/png"))


def find_kyc_document(verification_id, name: str) -> tuple[str, str] | None:
    """Locate one stored KYC image: (object key, content type), or None.

    None covers an unknown name as well as a missing object, so a caller can't
    use this to probe arbitrary keys. A missing object is a normal answer:
    retention is best-effort, exactly as for certificates.
    """
    if name not in KYC_DOCUMENT_NAMES or not r2.is_configured():
        return None
    bucket = _bucket()
    for ext, content_type in _KYC_IMAGE_EXTENSIONS:
        key = f"verification/KYC/{verification_id}/{name}.{ext}"
        if r2.head_object(key, bucket=bucket) is not None:
            return key, content_type
    return None


def presign_kyc_document(key: str) -> str:
    """A short-lived read URL for a located KYC image (same TTL as certificates:
    these are ID cards and faces, so a leaked link must die quickly)."""
    return r2.presign_get(
        key, bucket=_bucket(), expires_in=CERTIFICATE_URL_TTL_SECONDS
    )
