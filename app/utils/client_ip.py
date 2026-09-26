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
Each proxy APPENDS the peer it saw, so the rightmost `TRUSTED_PROXY_HOPS`
entries were written by infrastructure we run and everything left of them was
written by the caller. The entry `hops` from the right is the first one the
caller could not have forged. With 0 (the default) the header is ignored and the
socket peer stands — never forgeable, merely a proxy address when deployed
behind one.
"""
from __future__ import annotations

import ipaddress

from app.core.config import settings


def trusted_client_ip(forwarded_for: str | None, hops: int) -> str | None:
    """The client address from an X-Forwarded-For value, or None to keep the peer."""
    if hops <= 0 or not forwarded_for:
        return None
    entries = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if len(entries) < hops:
        # Fewer entries than proxies we run means the request did not come
        # through the expected chain, so none of it can be vouched for.
        return None
    candidate = entries[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


class TrustedProxyMiddleware:
    """Pure ASGI, so it runs before FastAPI builds a Request and covers
    every route, websockets included."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            hops = settings.TRUSTED_PROXY_HOPS
            if hops > 0:
                # A header may legally be repeated; RFC 7230 says the values
                # join with commas in order, which is exactly the list we walk.
                values = [
                    value.decode("latin-1")
                    for name, value in scope.get("headers", [])
                    if name == b"x-forwarded-for"
                ]
                ip = trusted_client_ip(",".join(values), hops)
                if ip is not None:
                    port = scope["client"][1] if scope.get("client") else 0
                    scope = dict(scope)
                    scope["client"] = (ip, port)
        await self.app(scope, receive, send)
