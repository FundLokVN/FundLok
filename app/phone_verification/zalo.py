"""Zalo ZBS Template Message (formerly ZNS) client for one-time codes.

Spec: docs/specs/users/phone-verification.md

    Send   POST https://business.openapi.zalo.me/message/template
           header  access_token: <OA access token>
           body    {"phone": "84…", "template_id": "…", "template_data": {"otp": "…"}, "tracking_id": "…"}
           reply   {"error": 0, "message": "Success", "data": {"msg_id": …}}
    Token  POST https://oauth.zaloapp.com/v4/oa/access_token   (form-encoded)
           header  secret_key: <app secret>
           body    refresh_token, app_id, grant_type=refresh_token
           reply   {"access_token": …, "refresh_token": …, "expires_in": "90000"}

Zalo renamed ZNS to ZBS Template Message; the endpoint and body are unchanged.
Request shape and `error` codes from Zalo's own pages:
developers.zalo.me/docs/zbs-template-message/gui-tin-template-qua-sdt/api-gui-tin-qua-sdt/api-gui-tin
developers.zalo.me/docs/zbs-template-message/bang-ma-loi/
With ZALO_ZBS_DEVELOPMENT_MODE the body also carries "mode": "development":
Zalo then delivers only to admins of the app or the OA, for trying a template
out before it is approved.
Zalo answers 200 for refusals too, so success is `error == 0`, not the status.

The OA access token lives about 25 hours, and every refresh returns a NEW
refresh token and retires the old one. Lose the new one and the OA has to be
re-authorised by hand. So the pair lives in zalo_oa_tokens where every
instance can see it, encrypted with ZALO_TOKEN_ENCRYPTION_KEY; a refresh is
saved in its own transaction, never rolled back with the send that needed it;
and refreshes are serialised across instances by an advisory lock.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.phone_verification.models import ZaloOaToken
from app.utils.locks import advisory_xact_lock

logger = logging.getLogger(__name__)

SEND_URL = "https://business.openapi.zalo.me/message/template"
TOKEN_URL = "https://oauth.zaloapp.com/v4/oa/access_token"

_TIMEOUT = 10.0
_PREFIX = "v1:"
# Refresh this long before Zalo's stated expiry, so a token never dies mid-send.
_EXPIRY_MARGIN = timedelta(minutes=5)
# If a refresh reply omits expires_in, assume an hour rather than Zalo's 25.
_DEFAULT_TTL_SECONDS = 3600

TOKEN_INVALID = -124


class ZaloError(RuntimeError):
    """Zalo did not deliver. `code` is Zalo's `error`, when it gave one."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class ZaloNotConfigured(ZaloError):
    """A ZALO_* setting is missing or unusable."""


def _cipher() -> Fernet:
    key = settings.ZALO_TOKEN_ENCRYPTION_KEY
    if not key:
        raise ZaloNotConfigured("ZALO_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise ZaloNotConfigured("ZALO_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def _encrypt(value: str) -> str:
    return _PREFIX + _cipher().encrypt(value.encode()).decode()


def _decrypt(stored: str | None) -> str | None:
    """None for anything unreadable: another format, tampering, a rotated key."""
    if not stored or not stored.startswith(_PREFIX):
        return None
    try:
        return _cipher().decrypt(stored[len(_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _require(*names: str) -> None:
    missing = [name for name in names if not getattr(settings, name)]
    if missing:
        raise ZaloNotConfigured(f"{', '.join(missing)} not configured")


async def _post(url: str, *, headers: dict[str, str], **kwargs: Any) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        logger.warning("Zalo %s unreachable: %s", url, exc)
        raise ZaloError(f"Zalo {url} unreachable") from exc
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code != 200 or not isinstance(data, dict):
        logger.warning("Zalo %s failed (%s): %s", url, resp.status_code, resp.text[:500])
        raise ZaloError(f"Zalo {url} returned HTTP {resp.status_code}")
    return data


async def _refresh(refresh_token: str) -> tuple[str, str, datetime]:
    data = await _post(
        TOKEN_URL,
        headers={"secret_key": settings.ZALO_APP_SECRET},
        data={"refresh_token": refresh_token, "app_id": settings.ZALO_APP_ID, "grant_type": "refresh_token"},
    )
    access, new_refresh = data.get("access_token"), data.get("refresh_token")
    if not access or not new_refresh:
        # Logged by name only: the reply never carries a usable token here.
        logger.error(
            "Zalo token refresh refused: error=%s %s",
            data.get("error"),
            data.get("error_name") or data.get("message"),
        )
        raise ZaloError("Zalo token refresh refused", code=data.get("error"))
    try:
        ttl = int(data.get("expires_in") or _DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL_SECONDS
    return access, new_refresh, datetime.now(timezone.utc) + timedelta(seconds=ttl)


async def _load(db, app_id: str) -> ZaloOaToken | None:
    result = await db.execute(
        select(ZaloOaToken)
        .where(ZaloOaToken.app_id == app_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _live_token(row: ZaloOaToken | None, seed: str | None, stale: str | None) -> str | None:
    """The stored access token, if it is still good to use."""
    if row is None:
        return None
    if seed and row.seeded_from != _fingerprint(seed):
        return None  # re-authorised since: the environment's token wins
    if row.access_expires_at - _EXPIRY_MARGIN <= datetime.now(timezone.utc):
        return None
    token = _decrypt(row.access_token)
    return None if token is None or token == stale else token


async def access_token(*, stale: str | None = None) -> str:
    """A live OA access token, refreshing it when it is due.

    `stale` is a token Zalo has just rejected: refresh unless another instance
    already has. Read without the lock first, because nearly every call finds a
    good token and should not queue behind the others.
    """
    _require("ZALO_APP_ID", "ZALO_APP_SECRET")
    # Before any refresh: Zalo retires the old refresh token the moment it
    # answers, so a key that cannot store the new one would lose the chain.
    _cipher()
    app_id, seed = settings.ZALO_APP_ID, settings.ZALO_OA_REFRESH_TOKEN
    # Its own session: a refresh must be kept even when the send that asked
    # for it is rolled back, or the rotated refresh token is lost.
    async with SessionLocal() as db:
        token = _live_token(await _load(db, app_id), seed, stale)
        if token:
            return token

        await advisory_xact_lock(db, f"zalo-oa-token:{app_id}")
        row = await _load(db, app_id)
        token = _live_token(row, seed, stale)
        if token:  # another instance refreshed while this one waited
            await db.commit()
            return token

        continuing = row is not None and (not seed or row.seeded_from == _fingerprint(seed))
        refresh_token = _decrypt(row.refresh_token) if continuing else seed
        if not refresh_token:
            await db.commit()
            raise ZaloNotConfigured("no usable Zalo refresh token; set ZALO_OA_REFRESH_TOKEN")

        access, new_refresh, expires_at = await _refresh(refresh_token)
        if row is None:
            row = ZaloOaToken(app_id=app_id)
            db.add(row)
        row.access_token = _encrypt(access)
        row.refresh_token = _encrypt(new_refresh)
        row.access_expires_at = expires_at
        if not continuing:
            row.seeded_from = _fingerprint(seed)
        await db.commit()
        return access


async def send_otp(to: str, code: str, *, tracking_id: str) -> None:
    """Send `code` to `to` (84xxxxxxxxx) through the OTP template."""
    _require("ZALO_APP_ID", "ZALO_APP_SECRET", "ZALO_ZBS_OTP_TEMPLATE_ID", "ZALO_TOKEN_ENCRYPTION_KEY")
    body = {
        "phone": to,
        "template_id": settings.ZALO_ZBS_OTP_TEMPLATE_ID,
        "template_data": {settings.ZALO_ZBS_OTP_PARAM: code},
        "tracking_id": tracking_id,
    }
    if settings.ZALO_ZBS_DEVELOPMENT_MODE:
        body["mode"] = "development"
    token = await access_token()
    data = await _post(SEND_URL, headers={"access_token": token}, json=body)
    if data.get("error") == TOKEN_INVALID:
        # Revoked or expired early. One refresh and one retry; no loop.
        token = await access_token(stale=token)
        data = await _post(SEND_URL, headers={"access_token": token}, json=body)
    if data.get("error") != 0:
        logger.warning("Zalo ZBS refused: error=%s message=%s", data.get("error"), data.get("message"))
        raise ZaloError(f"Zalo ZBS refused (error={data.get('error')})", code=data.get("error"))
