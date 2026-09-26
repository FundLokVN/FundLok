"""App-wide request handling: validation error bodies and client-IP resolution.

Both live in app/main.py and apply to every route, so they are tested against
routes owned by different modules, not only the rate calculator where they were
found.
"""
import pytest

from app.utils.client_ip import trusted_client_ip

NAN_BODY = '{"email": NaN, "password": NaN, "name": NaN, "loan_amount": NaN}'


# --------------------------------------------------------------------------- #
# Validation errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/auth/login", "/contact", "/api/v1/rates/calculate"])
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_json_literals_are_a_422_not_a_500(client, path, literal):
    """json.loads accepts NaN and Infinity. The default 422 handler echoed the
    rejected value back and could not serialise it, so every POST route —
    login included — answered these literals with a 500."""
    resp = await client.post(
        path,
        content=NAN_BODY.replace("NaN", literal),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422, resp.text


async def test_validation_errors_do_not_reflect_the_rejected_input(client):
    secret = "hunter2-" + "x" * 64
    resp = await client.post(
        "/api/v1/rates/calculate",
        json={"industry": secret},
    )
    assert resp.status_code == 422
    assert secret not in resp.text
    first = resp.json()["detail"][0]
    # The frontend reads exactly these (lib/api-client.ts extractMessage).
    assert set(first) == {"type", "loc", "msg"}


# --------------------------------------------------------------------------- #
# Client IP
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "header,hops,expected",
    [
        ("9.9.9.9, 203.0.113.7", 0, None),  # 0 = header ignored
        ("9.9.9.9, 203.0.113.7", 1, "203.0.113.7"),  # rightmost = our proxy wrote it
        ("9.9.9.9, 203.0.113.7, 10.0.0.1", 2, "203.0.113.7"),
        ("203.0.113.7", 2, None),  # shorter than the chain: vouch for nothing
        ("9.9.9.9, not-an-ip", 1, None),  # garbage in the trusted slot
        ("", 1, None),
        (None, 1, None),
        ("9.9.9.9,,203.0.113.7", 1, "203.0.113.7"),
    ],
)
def test_trusted_client_ip(header, hops, expected):
    assert trusted_client_ip(header, hops) == expected


async def test_middleware_applies_to_routes_outside_rates(client, monkeypatch):
    """The point of moving the rule into middleware: ~45 audit-log sites read
    request.client.host, and they must now agree with the rate calculator."""
    from fastapi import Request

    from app.core.config import settings
    from app.main import app

    seen = {}

    @app.get("/__probe_client_ip")
    async def _probe(request: Request):
        seen["ip"] = request.client.host if request.client else None
        return {}

    try:
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        await client.get("/__probe_client_ip", headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.4"})
        assert seen["ip"] == "198.51.100.4"

        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
        await client.get("/__probe_client_ip", headers={"X-Forwarded-For": "9.9.9.9"})
        assert seen["ip"] != "9.9.9.9"
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__probe_client_ip"
        ]
