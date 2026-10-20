"""SEC-08: a deployment that requires captcha but has no secret refuses.

Until SEC-08 a missing CLOUDFLARE_TURNSTILE_SECRET_KEY switched captcha off,
so login, contact and the rate calculator accepted any request. conftest.py
runs the suite with CAPTCHA_REQUIRED=False; these tests turn it back on.
"""
import pytest

from app.core.config import settings
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
    # Its verifier is the async twin, so this covers both code paths.
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
