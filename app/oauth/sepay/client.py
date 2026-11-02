"""Thin async client for SePay's OAuth2 token endpoint and API v1.

Docs: https://developer.sepay.vn/en/sepay-oauth2/bat-dau-nhanh
Every call goes through `_send`, the single seam tests replace.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings


class SepayError(RuntimeError):
    """SePay was unreachable or answered with an error."""

    def __init__(self, message: str, *, status_code: int | None = None, error: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        # The OAuth error code, e.g. "invalid_grant", when SePay sent one.
        self.error = error


async def _send(method: str, url: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.request(method, url, **kwargs)


def _base() -> str:
    return settings.SEPAY_BASE_URL.rstrip("/")


async def _call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        resp = await _send(method, f"{_base()}{path}", **kwargs)
    except httpx.RequestError as exc:
        raise SepayError(f"Could not reach SePay: {exc}") from exc

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        raise SepayError(
            f"SePay {method} {path} failed ({resp.status_code})",
            status_code=resp.status_code,
            error=error,
        )
    return body if isinstance(body, dict) else {}


async def exchange_code(code: str) -> dict[str, Any]:
    return await _call(
        "POST",
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.sepay_redirect_uri,
            "client_id": settings.SEPAY_CLIENT_ID,
            "client_secret": settings.SEPAY_CLIENT_SECRET,
        },
    )


async def refresh(refresh_token: str) -> dict[str, Any]:
    return await _call(
        "POST",
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.SEPAY_CLIENT_ID,
            "client_secret": settings.SEPAY_CLIENT_SECRET,
        },
    )


async def list_bank_accounts(access_token: str, *, page: int = 1, limit: int = 50) -> dict[str, Any]:
    return await _call(
        "GET",
        "/api/v1/bank-accounts",
        params={"page": page, "limit": limit},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
