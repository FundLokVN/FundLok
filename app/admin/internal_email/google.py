"""Google OAuth 2.0 and the Gmail API, for each admin's own Gmail connection.

THE FLOW
1. GET /admin/email/oauth/authorize (signed-in admin) answers with Google's
   consent URL and sets a short-lived browser-binding cookie.
2. Google sends the browser to GET /admin/email/oauth/callback with a code.
3. The callback trades the code for tokens (exchange_code), keeps only the
   refresh token, encrypted (crypto.py), and redirects to the console.
4. Each send trades the refresh token for a one-hour access token
   (refresh_access_token) and posts each message to the Gmail API.

STATE AND BROWSER BINDING
`state` is a JWT signed with SECRET_KEY: the admin's id, a `purpose` claim
(so app/utils/jwt.py never accepts it as a session) and the PKCE challenge.
The PKCE verifier lives only in an httpOnly cookie on the browser that started
the flow. The callback needs both: a state lifted from a URL is useless without
that browser's cookie, and a code intercepted on the way back is useless
without the verifier. Neither the callback nor the cookie relies on the session
cookie, so an access token that expired while the admin was on Google's
consent screen does not fail the connect.

FIXED HOSTS
Every Google URL is a constant, not a setting, for the reason transport.py
gave for the SMTP host: nothing on the settings screen can point the server at
another host.

All HTTP goes through `_client()`, the seam the tests replace with a fake
Google (conftest makes an unpatched one fail).
"""

import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = ("openid", "email", "profile", GMAIL_SEND_SCOPE)

# Issuers Google signs ID tokens with (both spellings are in use).
_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

TIMEOUT_SECONDS = 20

STATE_PURPOSE = "gmail_oauth_state"
STATE_TTL = timedelta(minutes=10)

# The PKCE verifier, on the browser that started the flow. No Path attribute:
# the browser then scopes it to the directory of the URL that set it,
# /api/admin/email/oauth behind the frontend proxy (or /admin/email/oauth
# called directly), so it travels to the callback and nowhere else.
BINDING_COOKIE = "gmail_oauth_binding"


class OAuthNotConfigured(Exception):
    """No client id (GOOGLE_OAUTH_CLIENT_ID, else GOOGLE_CLIENT_ID) or no
    GOOGLE_OAUTH_CLIENT_SECRET."""


class GoogleTokenRevoked(Exception):
    """Google answered invalid_grant: the admin revoked access, changed their
    password, the token expired unused, or it was revoked by a disconnect.
    Only a new connect can fix it."""


class GoogleUnavailable(Exception):
    """Google could not be reached or answered with an error that is not the
    token's fault. Worth trying again later."""


@dataclass(frozen=True)
class TokenGrant:
    """What the callback keeps from Google's code exchange."""

    refresh_token: str | None
    access_token: str
    scopes: list[str]
    email: str
    email_verified: bool
    name: str | None


def is_configured() -> bool:
    return bool(settings.gmail_oauth_client_id and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def _credentials() -> tuple[str, str]:
    """(client id, client secret). The client id falls back to the sign-in
    client (GOOGLE_CLIENT_ID), so reusing it needs only its secret."""
    if not is_configured():
        logger.error(
            "No Google OAuth client for Admin > Email: set GOOGLE_OAUTH_CLIENT_SECRET "
            "(and GOOGLE_OAUTH_CLIENT_ID, unless reusing GOOGLE_CLIENT_ID)."
        )
        raise OAuthNotConfigured
    return settings.gmail_oauth_client_id, settings.GOOGLE_OAUTH_CLIENT_SECRET.strip()


def _client() -> httpx.AsyncClient:
    # A seam: tests replace this with a fake Google.
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS)


# --------------------------------------------------------------------------- #
# Authorize
# --------------------------------------------------------------------------- #


def new_code_verifier() -> str:
    # 43 characters of [A-Za-z0-9_-]: inside RFC 7636's 43-128 unreserved.
    return secrets.token_urlsafe(32)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def create_state(user_id: uuid.UUID, challenge: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "purpose": STATE_PURPOSE,
            "cc": challenge,
            "iat": now,
            "exp": now + STATE_TTL,
            "jti": uuid.uuid4().hex,
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def read_state(state: str) -> tuple[uuid.UUID, str] | None:
    """(admin id, PKCE challenge) from a state this server issued in the last
    STATE_TTL, else None."""
    try:
        payload = jwt.decode(
            state,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"require": ["exp", "sub", "purpose", "cc"]},
        )
        if payload.get("purpose") != STATE_PURPOSE:
            return None
        return uuid.UUID(payload["sub"]), str(payload["cc"])
    except (jwt.PyJWTError, ValueError, TypeError):
        return None


def authorization_url(state: str, challenge: str) -> str:
    client_id, _ = _credentials()
    params = {
        "client_id": client_id,
        "redirect_uri": settings.gmail_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent: Google returns a refresh token on EVERY connect,
        # including a reconnect of an account that granted access before.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


def _id_token_claims(id_token: str, client_id: str) -> dict:
    """The ID token's claims. Received straight from Google's token endpoint
    over verified TLS, so OpenID Connect Core 3.1.3.7 allows the TLS server
    check in place of the signature; issuer, audience and expiry are still
    checked."""
    claims = jwt.decode(
        id_token,
        options={"verify_signature": False, "verify_exp": True},
        algorithms=["RS256"],
    )
    if claims.get("iss") not in _ISSUERS:
        raise GoogleUnavailable("unexpected ID token issuer")
    aud = claims.get("aud")
    if aud != client_id and not (isinstance(aud, list) and client_id in aud):
        raise GoogleUnavailable("ID token is for another client")
    return claims


async def exchange_code(code: str, code_verifier: str) -> TokenGrant:
    client_id, client_secret = _credentials()
    try:
        async with _client() as http:
            resp = await http.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": code_verifier,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": settings.gmail_oauth_redirect_uri,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Google code exchange failed: %s", type(exc).__name__)
        raise GoogleUnavailable from exc
    if resp.status_code != 200:
        # Google's error code only ("invalid_grant", "redirect_uri_mismatch"),
        # never the body, which could echo the code back.
        logger.warning(
            "Google code exchange refused (HTTP %s, %s)", resp.status_code, _oauth_error(resp)
        )
        raise GoogleUnavailable
    body = resp.json()
    try:
        claims = _id_token_claims(body["id_token"], client_id)
    except (KeyError, jwt.PyJWTError) as exc:
        logger.warning("Google code exchange returned no usable ID token")
        raise GoogleUnavailable from exc
    return TokenGrant(
        refresh_token=body.get("refresh_token"),
        access_token=body["access_token"],
        scopes=str(body.get("scope", "")).split(),
        email=str(claims.get("email") or ""),
        email_verified=claims.get("email_verified") in (True, "true"),
        name=claims.get("name"),
    )


async def refresh_access_token(refresh_token: str, http: httpx.AsyncClient) -> str:
    """A fresh access token (one hour) for the stored refresh token."""
    client_id, client_secret = _credentials()
    try:
        resp = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    except httpx.HTTPError as exc:
        logger.warning("Google token refresh failed: %s", type(exc).__name__)
        raise GoogleUnavailable from exc
    if resp.status_code == 200:
        return resp.json()["access_token"]
    error = _oauth_error(resp)
    logger.warning("Google token refresh refused (HTTP %s, %s)", resp.status_code, error)
    if error in ("invalid_grant", "unauthorized_client"):
        raise GoogleTokenRevoked
    raise GoogleUnavailable


async def revoke(token: str) -> None:
    """Best effort: tell Google to drop the grant. A failure is logged and
    ignored; the token is deleted on our side either way.

    Revoking removes the whole grant for this account and OAuth client, so it
    is only called for a token being given up for good, never for the
    previous token of an account that just reconnected."""
    try:
        async with _client() as http:
            resp = await http.post(REVOKE_URL, data={"token": token})
        if resp.status_code != 200:
            logger.info("Google token revocation answered HTTP %s", resp.status_code)
    except httpx.HTTPError as exc:
        logger.info("Google token revocation failed: %s", type(exc).__name__)


def _oauth_error(resp: httpx.Response) -> str:
    try:
        error = resp.json().get("error")
    except ValueError:
        return "no JSON"
    # Google's token endpoint uses a string; other APIs nest an object.
    return error if isinstance(error, str) else "unknown"


# --------------------------------------------------------------------------- #
# Gmail API
# --------------------------------------------------------------------------- #


def gmail_error_reasons(resp: httpx.Response) -> set[str]:
    """The `reason` values of a Gmail API error body, e.g.
    {"insufficientPermissions"} or {"userRateLimitExceeded"}."""
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        return set()
    if not isinstance(error, dict):
        return set()
    reasons = {e.get("reason") for e in error.get("errors") or [] if isinstance(e, dict)}
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and detail.get("reason"):
            reasons.add(detail["reason"])
    if error.get("status"):
        reasons.add(error["status"])
    return {r for r in reasons if isinstance(r, str)}


async def send_raw(http: httpx.AsyncClient, access_token: str, raw: bytes) -> httpx.Response:
    """POST one RFC 2822 message, base64url-encoded, to users.messages.send.
    Returns Google's response for the caller to judge."""
    return await http.post(
        GMAIL_SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"raw": base64.urlsafe_b64encode(raw).decode("ascii")},
    )
