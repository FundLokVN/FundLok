"""App-wide request handling: validation error bodies and client-IP resolution.

Both live in app/main.py and apply to every route, so they are tested against
routes owned by different modules, not only the rate calculator where they were
found.
"""
import pytest

from app.utils.client_ip import trusted_client_ip, vouched_client_ip

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


@pytest.fixture
def probe_client_ip():
    """A throwaway route that reports the address the middleware settled on."""
    from fastapi import Request

    from app.main import app

    seen = {}

    @app.get("/__probe_client_ip")
    async def _probe(request: Request):
        seen["ip"] = request.client.host if request.client else None
        return {}

    try:
        yield seen
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__probe_client_ip"
        ]


async def test_middleware_applies_to_routes_outside_rates(client, monkeypatch, probe_client_ip):
    """The point of moving the rule into middleware: ~45 audit-log sites read
    request.client.host, and they must now agree with the rate calculator."""
    from app.core.config import settings

    seen = probe_client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    await client.get("/__probe_client_ip", headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.4"})
    assert seen["ip"] == "198.51.100.4"

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
    await client.get("/__probe_client_ip", headers={"X-Forwarded-For": "9.9.9.9"})
    assert seen["ip"] != "9.9.9.9"


# --------------------------------------------------------------------------- #
# Client IP from the frontend (X-Client-IP + X-Backend-Secret)
# --------------------------------------------------------------------------- #

FRONTEND_SECRET = "a3f1c9e07b5d42e8a6c1f0d9b8e7a6c5"
VISITOR = "203.0.113.50"


@pytest.mark.parametrize(
    "client_ip,presented,expected_secret,expected",
    [
        (VISITOR, FRONTEND_SECRET, FRONTEND_SECRET, VISITOR),
        ("2001:DB8::1", FRONTEND_SECRET, FRONTEND_SECRET, "2001:db8::1"),  # normalised
        (VISITOR, "wrong", FRONTEND_SECRET, None),
        (VISITOR, FRONTEND_SECRET[:-1], FRONTEND_SECRET, None),  # a prefix is not the secret
        (VISITOR, None, FRONTEND_SECRET, None),
        (VISITOR, "", FRONTEND_SECRET, None),
        (VISITOR, "anything", None, None),  # backend unconfigured: header disabled
        (VISITOR, "", "", None),  # an empty secret must not match an empty header
        (VISITOR, "sécret", FRONTEND_SECRET, None),  # non-ASCII: rejected, not a crash
        ("not-an-ip", FRONTEND_SECRET, FRONTEND_SECRET, None),
        ("9.9.9.9, 203.0.113.50", FRONTEND_SECRET, FRONTEND_SECRET, None),  # one address only
        (None, FRONTEND_SECRET, FRONTEND_SECRET, None),
    ],
)
def test_vouched_client_ip(client_ip, presented, expected_secret, expected):
    assert vouched_client_ip(client_ip, presented, expected_secret) == expected


async def test_client_ip_header_is_trusted_with_the_right_secret(
    client, monkeypatch, probe_client_ip
):
    """Every Next → FastAPI call carries the visitor's address in X-Client-IP,
    the server-side session checks included. With the secret it is the
    answer, whatever X-Forwarded-For says and however many hops are set."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "BACKEND_SECRET_KEY", FRONTEND_SECRET)
    for hops in (0, 1, 2):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", hops)
        probe_client_ip.clear()
        await client.get(
            "/__probe_client_ip",
            headers={
                "X-Backend-Secret": FRONTEND_SECRET,
                "X-Client-IP": VISITOR,
                "X-Forwarded-For": "9.9.9.9, 198.51.100.4, 10.0.0.1",
            },
        )
        assert probe_client_ip["ip"] == VISITOR, f"hops={hops}"


@pytest.mark.parametrize(
    "secret_headers",
    [
        {"X-Backend-Secret": "guessed"},
        {},
    ],
    ids=["wrong-secret", "missing-secret"],
)
async def test_client_ip_header_is_ignored_without_the_secret(
    client, monkeypatch, probe_client_ip, secret_headers
):
    """Without the secret X-Client-IP is a header any caller can type, so
    it is ignored and the existing rule decides."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "BACKEND_SECRET_KEY", FRONTEND_SECRET)

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
    await client.get(
        "/__probe_client_ip", headers={**secret_headers, "X-Client-IP": VISITOR}
    )
    assert probe_client_ip["ip"] != VISITOR

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    await client.get(
        "/__probe_client_ip",
        headers={
            **secret_headers,
            "X-Client-IP": VISITOR,
            "X-Forwarded-For": "9.9.9.9, 198.51.100.4",
        },
    )
    assert probe_client_ip["ip"] == "198.51.100.4"


async def test_client_ip_header_is_ignored_when_the_backend_has_no_secret(
    client, monkeypatch, probe_client_ip
):
    """Unset BACKEND_SECRET_KEY turns the header off; no presented value,
    the empty string included, can stand in for a secret we do not have."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "BACKEND_SECRET_KEY", None)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
    for presented in ("anything", ""):
        await client.get(
            "/__probe_client_ip",
            headers={"X-Backend-Secret": presented, "X-Client-IP": VISITOR},
        )
        assert probe_client_ip["ip"] != VISITOR


async def test_hops_rule_still_applies_when_the_secret_carries_no_usable_address(
    client, monkeypatch, probe_client_ip
):
    """The right secret with no X-Client-IP, a garbage one, or two of them
    falls back to TRUSTED_PROXY_HOPS exactly as before."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "BACKEND_SECRET_KEY", FRONTEND_SECRET)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    base = {"X-Backend-Secret": FRONTEND_SECRET, "X-Forwarded-For": "9.9.9.9, 198.51.100.4"}

    for extra in ([], [("X-Client-IP", "not-an-ip")], [("X-Client-IP", VISITOR), ("X-Client-IP", "1.2.3.4")]):
        probe_client_ip.clear()
        headers = list(base.items()) + extra
        await client.get("/__probe_client_ip", headers=headers)
        assert probe_client_ip["ip"] == "198.51.100.4", extra
