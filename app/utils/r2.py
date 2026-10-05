"""Cloudflare R2 (S3-compatible) client helpers.

Locally the same code talks to the minio container from docker-compose.yml.
"""

from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException, status

from app.core.config import settings


def is_configured() -> bool:
    """True when the R2 credentials are present. Callers with best-effort
    storage (e.g. verification document retention) check this to no-op
    gracefully instead of surfacing the 503 that _client() raises."""
    return bool(
        settings.R2_ENDPOINT_URL and settings.R2_ACCESS_KEY_ID and settings.R2_SECRET_ACCESS_KEY
    )


@lru_cache(maxsize=1)
def _client():
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured",
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name=settings.R2_REGION,
        config=Config(signature_version="s3v4"),
    )


def put_bytes(file_key: str, data: bytes, content_type: str, *, bucket: str | None = None) -> None:
    """Server-side direct upload of an in-memory object (no presign round-trip).

    Used where the backend itself holds the bytes — e.g. retaining verification
    documents. ``bucket`` overrides the default uploads bucket.
    """
    _client().put_object(
        Bucket=bucket or settings.R2_BUCKET,
        Key=file_key,
        Body=data,
        ContentType=content_type,
    )


def get_bytes(file_key: str, *, max_bytes: int, bucket: str | None = None) -> bytes:
    """Read an object into memory, refusing anything over ``max_bytes``.

    The cap is read-side, not trusted from the object's metadata alone: the
    body stream is cut at ``max_bytes + 1`` so an object larger than it claims
    still cannot exhaust memory. Used where the backend must parse an upload
    (e-invoice listings); the caller decides the ceiling.
    """
    resp = _client().get_object(Bucket=bucket or settings.R2_BUCKET, Key=file_key)
    body = resp["Body"]
    try:
        data = body.read(max_bytes + 1)
    finally:
        body.close()
    if len(data) > max_bytes:
        raise ValueError(f"Object {file_key} exceeds {max_bytes} bytes")
    return data


def presign_put(file_key: str, content_type: str, *, content_length: int) -> str:
    """Presigned PUT URL bound to one Content-Type AND one exact size.

    Both are signed headers: storage rejects the PUT unless the client sends
    the same Content-Type and a Content-Length equal to ``content_length``.
    Signing the length is what enforces a size limit. Without it the URL
    accepted anything up to R2's 5 GB single-PUT maximum, whatever size the
    caller declared (and had checked) at presign. A presigned POST with a
    content-length-range policy would allow a range instead, but R2 does not
    support POST uploads.

    Callers pass the size they validated against their own limit. A browser
    sets Content-Length to the file's byte size itself, so declaring
    ``file.size`` matches automatically.
    """
    if content_length <= 0:
        raise ValueError("content_length must be positive")
    return _client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.R2_BUCKET,
            "Key": file_key,
            "ContentType": content_type,
            "ContentLength": content_length,
        },
        ExpiresIn=settings.R2_PRESIGN_EXPIRE_SECONDS,
    )


def presign_get(
    file_key: str,
    *,
    bucket: str | None = None,
    expires_in: int | None = None,
) -> str:
    """Presigned GET URL for reading a private object (e.g. rendering an avatar).

    `bucket` mirrors put_bytes: verification documents live in
    R2_VERIFICATION_BUCKET, not the default one. `expires_in` lets a caller
    choose a tighter window than the global default — a link to someone's
    business registration certificate should not live as long as an avatar.
    """
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket or settings.R2_BUCKET,
            "Key": file_key,
        },
        ExpiresIn=expires_in or settings.R2_PRESIGN_EXPIRE_SECONDS,
    )


def delete_object(file_key: str) -> None:
    """Delete an object; deleting a nonexistent key is a no-op in S3/R2."""
    _client().delete_object(Bucket=settings.R2_BUCKET, Key=file_key)


def head_object(file_key: str, *, bucket: str | None = None) -> dict | None:
    """Object metadata ({size, content_type}) or None if the key does not exist."""
    try:
        resp = _client().head_object(Bucket=bucket or settings.R2_BUCKET, Key=file_key)
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return None
        raise
    return {
        "size": resp.get("ContentLength"),
        "content_type": resp.get("ContentType"),
    }
