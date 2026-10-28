"""Evidence storage interface (HANDOFF-04 B4/I-14). Production location is open (Q-025): everything goes through
this Protocol so the adapter can move to Viettel IDC without touching callers."""
from __future__ import annotations

import asyncio
from typing import Protocol


class EvidenceStorage(Protocol):
    async def put(self, key: str, content: bytes, content_type: str) -> None: ...

    async def get(self, key: str, *, max_bytes: int) -> bytes: ...


class InMemoryStorage:
    """Test adapter; also useful for local dev without MinIO."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        self.objects[key] = (content, content_type)

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        content, _ = self.objects[key]
        if len(content) > max_bytes:
            raise ValueError("object larger than max_bytes")
        return content


class R2Storage:
    """Adapter over app/utils/r2.py (Cloudflare R2 / MinIO). boto3 is synchronous, so calls run in a thread."""

    def __init__(self, bucket: str | None = None) -> None:
        self.bucket = bucket

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        from app.utils import r2

        await asyncio.to_thread(r2.put_bytes, key, content, content_type, bucket=self.bucket)

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        from app.utils import r2

        return await asyncio.to_thread(r2.get_bytes, key, max_bytes=max_bytes, bucket=self.bucket)
