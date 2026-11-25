"""SEC-08: a deployment that requires captcha but has no secret refuses.

Until SEC-08 a missing CLOUDFLARE_TURNSTILE_SECRET_KEY switched captcha off,
so login, contact and the rate calculator accepted any request. conftest.py
runs the suite with CAPTCHA_REQUIRED=False; these tests turn it back on.
"""
from functools import partial
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import settings
from app.utils import captcha
from tests.rates.test_rate_inquiries import sample_inquiry_payload


@pytest.fixture
def captcha_required_without_secret(monkeypatch):
    monkeypatch.setattr(settings, "CAPTCHA_REQUIRED", True)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "")


def _assert_refused(resp):
    assert resp.status_code == 503, resp.text
    assert resp.headers["X-Error-Code"] == "CAPTCHA_UNCONFIGURED"


async def test_login_is_refused(client, captcha_required_without_secret):
    _assert_refused(
        await client.post("/auth/login", json={"email": "someone@example.com", "password": "Wh4tever-pass!"})
    )


async def test_the_contact_form_is_refused(client, captcha_required_without_secret):
    _assert_refused(
        await client.post(
            "/contact",
            json={"name": "A Person", "email": "someone@example.com", "subject": "Hi", "message": "Hello there"},
        )
    )


async def test_the_rate_calculator_is_refused(client, captcha_required_without_secret):
    _assert_refused(await client.post("/api/v1/rates/calculate", json=sample_inquiry_payload()))


async def test_with_captcha_not_required_the_request_goes_through(client, monkeypatch):
    monkeypatch.setattr(settings, "CAPTCHA_REQUIRED", False)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "")

    resp = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "Wh4tever-pass!"})

    assert resp.status_code == 401, resp.text


async def test_with_a_secret_a_missing_token_is_still_a_400(client, monkeypatch):
    monkeypatch.setattr(settings, "CAPTCHA_REQUIRED", True)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")

    resp = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "Wh4tever-pass!"})

    assert resp.status_code == 400, resp.text
    assert "Turnstile token is missing" in resp.text


@pytest.mark.parametrize(
    ("path", "fields"),
    [
        ("/auth/login", {"password": "Wh4tever-pass!"}),
        ("/auth/register", {"password": "Wh4tever-pass!", "full_name": "A Person"}),
        ("/auth/forgot-password", {}),
        ("/contact", {"name": "A Person", "message": "Hello there"}),
    ],
)
async def test_forms_reject_invalid_tokens_using_async_siteverify(client, monkeypatch, path, fields):
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-secret")

    def blocking_client(*args, **kwargs):
        pytest.fail("Turnstile verification opened a blocking HTTP client")

    async def siteverify(request):
        assert request.method == "POST"
        assert request.url == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        assert parse_qs(request.content.decode()) == {
            "secret": ["test-secret"], "response": ["invalid-token"],
        }
        return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})

    async_client = partial(httpx.AsyncClient, transport=httpx.MockTransport(siteverify))
    monkeypatch.setattr(captcha.httpx, "Client", blocking_client)
    monkeypatch.setattr(captcha.httpx, "AsyncClient", async_client)

    resp = await client.post(path, json={"email": "someone@example.com", "turnstile_token": "invalid-token", **fields})

    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Cloudflare Turnstile verification failed"}
