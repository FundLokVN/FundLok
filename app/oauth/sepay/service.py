"""SePay OAuth2 connection: authorize URL, code exchange, token refresh,
bank-account listing and disconnect (docs/specs/oauth/sepay-connect.md).

Tokens are only ever used here, server-side. They are never returned to the
browser or logged.
"""
from __future__ import annotations

import datetime as dt
import secrets
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import jwt
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.oauth.sepay import client
from app.oauth.sepay.client import SepayError
from app.oauth.sepay.crypto import EncryptionUnavailable, decrypt_token, encrypt_token
from app.oauth.sepay.models import STATUS_ACTIVE, STATUS_NEEDS_RECONNECT, SepayConnection

STATE_TYPE = "sepay_oauth_state"
STATE_TTL = dt.timedelta(minutes=10)
# SePay: access tokens last 1 hour, refresh tokens 1 month.
DEFAULT_ACCESS_TTL_SECONDS = 3600
REFRESH_TTL = dt.timedelta(days=30)
# Refresh a token this close to expiry instead of sending it.
REFRESH_MARGIN = dt.timedelta(seconds=60)
MAX_ACCOUNT_PAGES = 10


class SepayServiceError(Exception):
    def __init__(self, status_code: int, detail: str, *, persist: bool = False):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        # True when the session holds a change (e.g. NEEDS_RECONNECT) that
        # must be committed even though the request fails.
        self.persist = persist


NOT_CONFIGURED = "SePay connection is not available."
BAD_STATE = "This SePay link has expired. Please try connecting again."
CODE_REJECTED = "SePay did not accept the sign-in. Please try connecting again."
UNREACHABLE = "SePay is not responding. Please try again later."
NOT_CONNECTED = "No SePay account is connected."
NEEDS_RECONNECT = "Your SePay connection has expired. Please reconnect."


def _require_configured() -> None:
    if not settings.sepay_configured:
        raise SepayServiceError(503, NOT_CONFIGURED)


# --- state -----------------------------------------------------------------


def create_state(user_id: UUID, now: dt.datetime) -> str:
    payload = {
        "typ": STATE_TYPE,
        "sub": str(user_id),
        "nonce": secrets.token_urlsafe(16),
        "iat": int(now.timestamp()),
        "exp": int((now + STATE_TTL).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_state(state: str, user_id: UUID) -> None:
    """The state must be ours, unexpired, and issued to this same user, so a
    code obtained in someone else's browser cannot be attached to this account."""
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except InvalidTokenError as exc:
        raise SepayServiceError(400, BAD_STATE) from exc
    if payload.get("typ") != STATE_TYPE or payload.get("sub") != str(user_id):
        raise SepayServiceError(400, BAD_STATE)


def build_authorize_url(user_id: UUID, now: dt.datetime) -> str:
    _require_configured()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.SEPAY_CLIENT_ID,
            "redirect_uri": settings.sepay_redirect_uri,
            "scope": settings.SEPAY_SCOPES,
            "state": create_state(user_id, now),
        }
    )
    return f"{settings.SEPAY_BASE_URL.rstrip('/')}/oauth/authorize?{query}"


# --- tokens ----------------------------------------------------------------


def _apply_tokens(conn: SepayConnection, tokens: dict[str, Any], now: dt.datetime) -> None:
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not access or not refresh:
        raise SepayServiceError(502, UNREACHABLE)
    try:
        conn.access_token = encrypt_token(access)
        conn.refresh_token = encrypt_token(refresh)
    except EncryptionUnavailable as exc:
        raise SepayServiceError(503, NOT_CONFIGURED) from exc
    try:
        expires_in = int(tokens.get("expires_in") or DEFAULT_ACCESS_TTL_SECONDS)
    except (TypeError, ValueError):
        expires_in = DEFAULT_ACCESS_TTL_SECONDS
    conn.access_expires_at = now + dt.timedelta(seconds=expires_in)
    conn.refresh_expires_at = now + REFRESH_TTL
    conn.status = STATUS_ACTIVE


async def get_connection(db: AsyncSession, user_id: UUID) -> SepayConnection | None:
    return (
        await db.execute(select(SepayConnection).where(SepayConnection.user_id == user_id))
    ).scalar_one_or_none()


async def connect(db: AsyncSession, *, user_id: UUID, code: str, state: str, now: dt.datetime) -> SepayConnection:
    _require_configured()
    verify_state(state, user_id)
    try:
        tokens = await client.exchange_code(code)
    except SepayError as exc:
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            raise SepayServiceError(400, CODE_REJECTED) from exc
        raise SepayServiceError(502, UNREACHABLE) from exc

    conn = await get_connection(db, user_id)
    if conn is None:
        conn = SepayConnection(user_id=user_id)
        db.add(conn)
    _apply_tokens(conn, tokens, now)
    granted = tokens.get("scope")
    conn.scopes = granted if isinstance(granted, str) and granted.strip() else settings.SEPAY_SCOPES
    conn.connected_at = now
    conn.last_refreshed_at = None
    await db.flush()
    return conn


async def _refresh(db: AsyncSession, conn: SepayConnection, now: dt.datetime) -> str:
    refresh_token = decrypt_token(conn.refresh_token)
    if not refresh_token:
        conn.status = STATUS_NEEDS_RECONNECT
        raise SepayServiceError(409, NEEDS_RECONNECT, persist=True)
    try:
        tokens = await client.refresh(refresh_token)
    except SepayError as exc:
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            conn.status = STATUS_NEEDS_RECONNECT
            raise SepayServiceError(409, NEEDS_RECONNECT, persist=True) from exc
        raise SepayServiceError(502, UNREACHABLE) from exc
    # SePay rotates the refresh token: both new tokens must be saved.
    _apply_tokens(conn, tokens, now)
    conn.last_refreshed_at = now
    await db.flush()
    return tokens["access_token"]


async def _access_token(db: AsyncSession, conn: SepayConnection, now: dt.datetime) -> str:
    if conn.status == STATUS_NEEDS_RECONNECT:
        raise SepayServiceError(409, NEEDS_RECONNECT)
    token = decrypt_token(conn.access_token)
    if token and conn.access_expires_at - REFRESH_MARGIN > now:
        return token
    return await _refresh(db, conn, now)


# --- bank accounts ---------------------------------------------------------


def _bank_account_out(raw: dict[str, Any]) -> dict[str, Any]:
    bank = raw.get("bank") or {}
    # `accumulated` (the balance) is dropped on purpose.
    return {
        "id": raw.get("id"),
        "label": raw.get("label"),
        "account_number": raw.get("account_number"),
        "account_holder_name": raw.get("account_holder_name"),
        "bank_short_name": bank.get("short_name"),
        "bank_full_name": bank.get("full_name"),
        "bank_bin": bank.get("bin"),
        "bank_logo_url": bank.get("logo_url"),
        "active": bool(raw.get("active", True)),
    }


async def _fetch_accounts(token: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_ACCOUNT_PAGES:
        body = await client.list_bank_accounts(token, page=page)
        out.extend(_bank_account_out(item) for item in body.get("data") or [])
        last_page = ((body.get("meta") or {}).get("pagination") or {}).get("last_page") or 1
        if page >= int(last_page):
            break
        page += 1
    return out


async def list_bank_accounts(db: AsyncSession, *, user_id: UUID, now: dt.datetime) -> list[dict[str, Any]]:
    _require_configured()
    conn = await get_connection(db, user_id)
    if conn is None:
        raise SepayServiceError(404, NOT_CONNECTED)
    token = await _access_token(db, conn, now)
    try:
        return await _fetch_accounts(token)
    except SepayError as exc:
        if exc.status_code != 401:
            raise SepayServiceError(502, UNREACHABLE) from exc
    # The token was refused before its expiry: refresh once and retry.
    token = await _refresh(db, conn, now)
    try:
        return await _fetch_accounts(token)
    except SepayError as exc:
        raise SepayServiceError(502, UNREACHABLE) from exc


async def disconnect(db: AsyncSession, *, user_id: UUID) -> SepayConnection | None:
    conn = await get_connection(db, user_id)
    if conn is not None:
        await db.delete(conn)
        await db.flush()
    return conn
