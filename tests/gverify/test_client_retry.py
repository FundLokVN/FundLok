"""GVerify client: connection failures are retried, and never leak to users.

Observed 2026-09-29: GVerify's nameservers (tpv.vn) intermittently time out,
so a lookup made while the local DNS cache is cold failed the whole KYC/KYB
request with "[Errno 8] nodename nor servname provided", and that text reached
the browser in the 502. Connection failures (nothing sent, nothing billed) are
now retried; anything after the request was sent is not, because the provider
may already have run and billed the call.

httpx.AsyncClient is replaced by a scripted fake, so no network is used. These
tests call the client functions directly and do not request the app `client`
fixture, which is why patching httpx here is safe.
"""
import httpx
import pytest

from app.core.config import settings
from app.gverify import client as gv
from app.gverify.client import UNREACHABLE_MESSAGE, GVerifyError

OK_ENVELOPE = {"success": True, "data": {"person_number": "001099009999"}}


@pytest.fixture
def fake_http(monkeypatch):
    """Script what each POST does: an exception to raise, or a JSON body."""
    monkeypatch.setattr(settings, "GVERIFY_BASE_URL", "https://gverify.test")
    monkeypatch.setattr(settings, "GVERIFY_API_KEY", "test-key")
    monkeypatch.setattr(settings, "GVERIFY_PARTNER_CODE", "TEST")
    monkeypatch.setattr(gv, "_RETRY_BACKOFF_SECONDS", 0)

    state = {"script": [], "calls": 0}

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            state["calls"] += 1
            step = state["script"].pop(0)
            if isinstance(step, Exception):
                raise step
            return httpx.Response(200, json=step, request=httpx.Request("POST", url))

    monkeypatch.setattr(gv.httpx, "AsyncClient", FakeAsyncClient)
    return state


def _dns_error():
    return httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")


async def test_a_transient_dns_failure_is_retried_and_succeeds(fake_http):
    fake_http["script"] = [_dns_error(), OK_ENVELOPE]

    data = await gv.verify_ocr_id(img_front_b64="a", img_back_b64="b")

    assert data == OK_ENVELOPE["data"]
    assert fake_http["calls"] == 2


async def test_connect_timeouts_are_retried_too(fake_http):
    fake_http["script"] = [httpx.ConnectTimeout("timed out"), httpx.ConnectTimeout("timed out"), OK_ENVELOPE]

    assert await gv.face_match(img1_b64="a", img2_b64="b") == OK_ENVELOPE["data"]
    assert fake_http["calls"] == 3


async def test_it_gives_up_after_the_retries_with_a_generic_message(fake_http):
    fake_http["script"] = [_dns_error(), _dns_error(), _dns_error()]

    with pytest.raises(GVerifyError) as exc:
        await gv.verify_ocr_id(img_front_b64="a", img_back_b64="b")

    assert fake_http["calls"] == 1 + gv._CONNECT_RETRIES
    assert str(exc.value) == UNREACHABLE_MESSAGE
    assert "Errno" not in str(exc.value)


async def test_a_read_timeout_is_not_retried(fake_http):
    # The request was sent: the provider may have run (and billed) the call.
    fake_http["script"] = [httpx.ReadTimeout("read timed out"), OK_ENVELOPE]

    with pytest.raises(GVerifyError) as exc:
        await gv.face_match(img1_b64="a", img2_b64="b")

    assert fake_http["calls"] == 1
    assert str(exc.value) == UNREACHABLE_MESSAGE


async def test_the_multipart_kyb_upload_is_retried_as_well(fake_http):
    fake_http["script"] = [_dns_error(), OK_ENVELOPE]

    data = await gv.ocrx_decode(
        file_bytes=b"%PDF-1.4", filename="c.pdf", content_type="application/pdf", document_type="COMPANY"
    )

    assert data == OK_ENVELOPE["data"]
    assert fake_http["calls"] == 2


async def test_a_provider_error_response_is_not_retried(fake_http):
    fake_http["script"] = [{"success": False, "error": {"code": 1, "message": "bad image"}}]

    with pytest.raises(GVerifyError):
        await gv.verify_ocr_id(img_front_b64="a", img_back_b64="b")

    assert fake_http["calls"] == 1
