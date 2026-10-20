"""One rule for "what is this caller's IP", applied before any route runs.

About 45 audit-log call sites read `request.client.host` directly. Fixing the
trust rule in each would be 45 chances to get it wrong, so this middleware
rewrites the ASGI `client` once and every existing read inherits the answer.

WHY NOT UVICORN'S OWN ProxyHeadersMiddleware
It is on by default and trusts X-Forwarded-For from whatever FORWARDED_ALLOW_IPS
lists. With "*" it trusts every hop and so reads the caller-supplied part; with
the default "127.0.0.1" its behaviour depends on how the platform happens to
connect to the container. Neither is a number anyone configured on purpose.
Uvicorn is therefore started with --no-proxy-headers (docker/entrypoint.sh,
package.json) and this is the only place the header is interpreted.

THE RULE
Two sources, tried in order.

1. X-Client-IP, vouched for by X-Backend-Secret. The Next.js frontend is the
   API's real caller: the browser talks to Vercel, and Vercel calls us. It
   puts the visitor's address, as its platform saw it, in X-Client-IP on
   every call it makes, including the server-side session checks that carry no
   visitor address anywhere else. The header is believed only when the same
   request carries the shared secret (BACKEND_SECRET_KEY, compared in constant
   time), because without it the header is just text any caller can type.

2. X-Forwarded-For, counted from the right. Each proxy APPENDS the peer it
   saw, so the rightmost `TRUSTED_PROXY_HOPS` entries were written by
   infrastructure we run and everything left of them was written by the
   caller. The entry `hops` from the right is the first one the caller could
   not have forged. With 0 (the default) the header is ignored and the socket
   peer stands — never forgeable, merely a proxy address when deployed behind
   one.

A request that satisfies neither keeps its socket peer.
"""
from __future__ import annotations

import hmac
import ipaddress

# The MODULE, not `settings` itself: resolved per request so a reloaded config
# (tests/scheduler/test_worker_scaffold.py reloads it) is seen rather than a
# stale object captured at import time.
from app.core import config

# Lower-case, as ASGI delivers them. The frontend spells them in lib/
# backend-secret.ts and lib/client-ip.ts; header names are case-insensitive.
BACKEND_SECRET_HEADER = b"x-backend-secret"
CLIENT_IP_HEADER = b"x-client-ip"
FORWARDED_FOR_HEADER = b"x-forwarded-for"


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def vouched_client_ip(
    client_ip: str | None, presented_secret: str | None, expected_secret: str | None
) -> str | None:
    """The address in X-Client-IP if the request proved it came from our
    frontend, or None to fall through to the hop rule."""
    if not expected_secret or not presented_secret or not client_ip:
        return None
    # compare_digest on bytes: on str it refuses anything non-ASCII, and the
    # presented value is whatever the caller sent.
    if not hmac.compare_digest(
        presented_secret.encode("utf-8"), expected_secret.encode("utf-8")
    ):
        return None
    return _valid_ip(client_ip)


def trusted_client_ip(forwarded_for: str | None, hops: int) -> str | None:
    """The client address from an X-Forwarded-For value, or None to keep the peer."""
    if hops <= 0 or not forwarded_for:
        return None
    entries = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if len(entries) < hops:
        # Fewer entries than proxies we run means the request did not come
        # through the expected chain, so none of it can be vouched for.
        return None
    return _valid_ip(entries[-hops])


def _single(values: list[str]) -> str | None:
    # Two copies of a header that carries one value is not something our
    # frontend sends; refuse to pick one rather than guess which is ours.
    return values[0] if len(values) == 1 else None


class TrustedProxyMiddleware:
    """Pure ASGI, so it runs before FastAPI builds a Request and covers
    every route, websockets included."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            settings = config.settings
            headers: dict[bytes, list[str]] = {}
            for name, value in scope.get("headers", []):
                if name in (BACKEND_SECRET_HEADER, CLIENT_IP_HEADER, FORWARDED_FOR_HEADER):
                    headers.setdefault(name, []).append(value.decode("latin-1"))

            ip = vouched_client_ip(
                _single(headers.get(CLIENT_IP_HEADER, [])),
                _single(headers.get(BACKEND_SECRET_HEADER, [])),
                settings.BACKEND_SECRET_KEY,
            )
            if ip is None and settings.TRUSTED_PROXY_HOPS > 0:
                # A header may legally be repeated; RFC 7230 says the values
                # join with commas in order, which is exactly the list we walk.
                ip = trusted_client_ip(
                    ",".join(headers.get(FORWARDED_FOR_HEADER, [])),
                    settings.TRUSTED_PROXY_HOPS,
                )
            if ip is not None:
                port = scope["client"][1] if scope.get("client") else 0
                scope = dict(scope)
                scope["client"] = (ip, port)
        await self.app(scope, receive, send)
