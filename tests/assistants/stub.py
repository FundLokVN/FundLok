"""A stand-in Anthropic API for the console assistants' tests.

The real SDK runs against an httpx2 MockTransport, so the tests cover the
request the SDK builds and the typed errors it raises, and never reach the
network. Test modules import the fixture by name, as the blog tests do with
`public_media`:

    from tests.assistants.stub import claude_stub  # noqa: F401
"""
import json

import anthropic
import httpx2
import pytest

from app.assistants import claude
from app.core.config import settings


def message(answer, *, stop_reason: str = "end_turn", text: str | None = None) -> dict:
    """A Messages API response whose text is `answer` as JSON."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": claude.MODEL,
        "content": [{"type": "text", "text": text if text is not None else json.dumps(answer, ensure_ascii=False)}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    }


def api_error(status: int, error_type: str, text: str, error_code: str | None = None) -> httpx2.Response:
    error = {"type": error_type, "message": text}
    if error_code:
        error["details"] = {"error_code": error_code}
    return httpx2.Response(status, json={"type": "error", "error": error})


class Stub:
    """Set `.reply` to an httpx2.Response, a message dict, an exception, or an
    async callable returning one; `.requests` and `.headers` keep what was sent."""

    def __init__(self) -> None:
        self.reply: object = None
        self.requests: list[dict] = []
        self.headers: list[dict] = []


@pytest.fixture
def claude_stub(monkeypatch) -> Stub:
    """Both assistants switched on for SYSTEM_ADMIN, answering from the stub."""
    stub = Stub()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        stub.requests.append(json.loads(request.content))
        stub.headers.append(dict(request.headers))
        reply = stub.reply
        if callable(reply):
            reply = await reply()
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        return httpx2.Response(200, json=reply, headers={"request-id": "req_test"})

    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(settings, "BLOG_SEO_ASSISTANT_AUDIENCE", "system_admin")
    monkeypatch.setattr(settings, "EMAIL_ASSISTANT_AUDIENCE", "system_admin")
    monkeypatch.setattr(claude, "_client_for", lambda api_key: client)
    return stub
