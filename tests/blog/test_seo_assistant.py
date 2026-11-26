"""The editor's "Suggest SEO" button (docs/specs/blog/seo-assistant.md).

Claude is never called: the SDK runs against an httpx2 MockTransport, so these
tests cover the real request the SDK builds and the real errors it raises.
"""
import asyncio
import json

import anthropic
import httpx2
import pytest

from app.blog import seo_assistant
from app.core.config import settings
from tests.blog.helpers import public_media  # noqa: F401 (autouse fixture)

STATUS = "/admin/blog/seo-assistant"
SUGGEST = "/admin/blog/seo-assistant/suggestions"
POSTS = "/admin/blog/posts"

ARTICLE_TITLE = "Vay vốn trả góp hằng ngày cho cửa hàng"
ARTICLE_TEXT = (
    "Vay vốn trả góp hằng ngày giúp cửa hàng nhập thêm hàng trước mùa cao điểm.\n\n"
    "Mỗi ngày làm việc, doanh nghiệp trả một khoản cố định cho từng nhà đầu tư."
)
DRAFT = {"title": ARTICLE_TITLE, "locale": "vi", "text": ARTICLE_TEXT}


def good_answer(**overrides) -> dict:
    answer = {
        "titles": [
            {"title": "Vay vốn trả góp hằng ngày là gì?", "intent": "Người mới tìm hiểu cách khoản vay vận hành."},
            {"title": "Trả góp hằng ngày hay trả theo tháng?", "intent": "Người đang so sánh các cách trả nợ."},
            {"title": "Vay vốn nhập hàng mùa cao điểm", "intent": "Chủ cửa hàng sẵn sàng tìm nguồn vốn."},
        ],
        "meta_description": "Cách vay vốn trả góp hằng ngày giúp cửa hàng nhập hàng trước mùa cao điểm.",
        "focus_keyword": "vay vốn trả góp hằng ngày",
        "related_terms": ["vốn lưu động", "nhập hàng"],
    }
    answer.update(overrides)
    return answer


def message(answer, *, stop_reason="end_turn", text=None) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": seo_assistant.MODEL,
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


@pytest.fixture
def claude(monkeypatch):
    """A configured assistant whose Anthropic API is a stub. Set `.reply` to
    an httpx2.Response, a message dict, or an exception; `.requests` keeps
    what was sent."""

    class Stub:
        reply: object = message(good_answer())
        requests: list[dict] = []
        headers: list[dict] = []

    stub = Stub()
    stub.requests = []
    stub.headers = []

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
    monkeypatch.setattr(seo_assistant, "_client_for", lambda api_key: client)
    return stub


async def _suggest(client, user, **overrides):
    return await client.post(SUGGEST, json={**DRAFT, **overrides}, headers=user["headers"])


# --------------------------------------------------------------------------- #
# Who sees it
# --------------------------------------------------------------------------- #


async def test_off_without_an_api_key(client, make_system_admin, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    admin = await make_system_admin()

    status = await client.get(STATUS, headers=admin["headers"])
    assert status.status_code == 200
    assert status.json() == {"enabled": False}

    resp = await _suggest(client, admin)
    assert resp.status_code == 503
    assert resp.headers["x-error-code"] == "BLOG_SEO_UNAVAILABLE"


async def test_starts_with_system_admins_only(client, claude, make_admin, make_system_admin):
    admin = await make_admin()
    system_admin = await make_system_admin()

    assert (await client.get(STATUS, headers=admin["headers"])).json() == {"enabled": False}
    assert (await client.get(STATUS, headers=system_admin["headers"])).json() == {"enabled": True}

    refused = await _suggest(client, admin)
    assert refused.status_code == 403
    assert refused.headers["x-error-code"] == "BLOG_SEO_UNAVAILABLE"
    assert claude.requests == []


async def test_all_admins_audience_includes_admins(client, claude, make_admin, monkeypatch):
    monkeypatch.setattr(settings, "BLOG_SEO_ASSISTANT_AUDIENCE", "all_admins")
    admin = await make_admin()

    assert (await client.get(STATUS, headers=admin["headers"])).json() == {"enabled": True}
    assert (await _suggest(client, admin)).status_code == 200


async def test_non_admins_cannot_ask(client, claude, make_user):
    investor = await make_user(role="INVESTOR")
    assert (await client.get(STATUS, headers=investor["headers"])).status_code == 403
    assert (await _suggest(client, investor)).status_code == 403
    assert claude.requests == []


# --------------------------------------------------------------------------- #
# What is sent and what comes back
# --------------------------------------------------------------------------- #


async def test_sends_the_draft_title_language_and_text(client, claude, make_system_admin):
    admin = await make_system_admin()

    resp = await _suggest(client, admin)
    assert resp.status_code == 200, resp.text
    assert resp.json() == good_answer()

    [sent] = claude.requests
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert set(sent["output_config"]["format"]["schema"]["required"]) == {
        "titles",
        "meta_description",
        "focus_keyword",
        "related_terms",
    }
    assert sent["fallbacks"] == "default"
    assert claude.headers[0]["anthropic-beta"] == seo_assistant.FALLBACK_BETA
    assert "rules for financial claims" in sent["system"]
    [user_turn] = sent["messages"]
    assert "filed under Vietnamese" in user_turn["content"]
    assert ARTICLE_TITLE in user_turn["content"]
    assert ARTICLE_TEXT in user_turn["content"]


async def test_an_english_draft_is_sent_as_english(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = message(
        good_answer(
            titles=[{"title": "Daily repayment loans for shops", "intent": "Owners comparing ways to repay."}],
            meta_description="How daily repayment loans help a shop stock up before its busy season.",
            focus_keyword="daily repayment",
            related_terms=["working capital"],
        )
    )

    resp = await _suggest(
        client,
        admin,
        title="Daily repayment, explained",
        locale="en",
        text="Daily repayment lets a shop stock up before its busy season.",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["focus_keyword"] == "daily repayment"
    assert "filed under English" in claude.requests[0]["messages"][0]["content"]


async def test_uses_the_draft_sent_and_stores_nothing(client, claude, make_system_admin):
    """The editor sends its unsaved draft; the saved post is never read or changed."""
    admin = await make_system_admin()
    post = (await client.post(POSTS, json={"title": "Bản nháp đã lưu"}, headers=admin["headers"])).json()

    resp = await _suggest(client, admin)
    assert resp.status_code == 200, resp.text
    assert ARTICLE_TEXT in claude.requests[0]["messages"][0]["content"]

    after = (await client.get(f"{POSTS}/{post['id']}", headers=admin["headers"])).json()
    assert after["title"] == "Bản nháp đã lưu"
    assert after["version"] == post["version"]
    assert after["seo_title"] is None
    assert after["focus_keyword"] is None


async def test_blank_or_oversized_drafts_are_refused(client, claude, make_system_admin):
    admin = await make_system_admin()
    assert (await _suggest(client, admin, text="   ")).status_code == 422
    assert (await _suggest(client, admin, title="")).status_code == 422
    assert (await _suggest(client, admin, text="x" * 100_001)).status_code == 422
    assert (await _suggest(client, admin, locale="fr")).status_code == 422
    assert claude.requests == []


# --------------------------------------------------------------------------- #
# Checks on Claude's answer
# --------------------------------------------------------------------------- #


async def test_title_options_over_70_characters_repeats_or_promises_are_dropped(
    client, claude, make_system_admin
):
    admin = await make_system_admin()
    claude.reply = message(
        good_answer(
            titles=[
                {"title": "Vay vốn trả góp hằng ngày   là gì?", "intent": " Người mới tìm hiểu. "},
                {"title": "VAY VỐN TRẢ GÓP HẰNG NGÀY LÀ GÌ?", "intent": "Lặp lại."},
                {"title": "x" * 71, "intent": "Quá dài."},
                {"title": "Lợi nhuận đảm bảo khi đầu tư", "intent": "Hứa hẹn lợi nhuận."},
                {"title": "Guaranteed returns from shops", "intent": "Promise."},
                {"title": "Trả góp hằng ngày hay trả theo tháng?", "intent": ""},
                {"title": "Vốn nhập hàng mùa cao điểm", "intent": "Chủ cửa hàng sẵn sàng vay."},
            ]
        )
    )

    resp = await _suggest(client, admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["titles"] == [
        {"title": "Vay vốn trả góp hằng ngày là gì?", "intent": "Người mới tìm hiểu."},
        {"title": "Vốn nhập hàng mùa cao điểm", "intent": "Chủ cửa hàng sẵn sàng vay."},
    ]


async def test_related_terms_are_tidied_deduplicated_and_capped_at_four(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = message(
        good_answer(
            related_terms=[
                " vốn  lưu động ",
                "Vốn lưu động",
                "VAY VỐN TRẢ GÓP HẰNG NGÀY",
                "",
                "credit scoring",
                "nhập hàng",
                "y" * 101,
                "trả góp",
                "cửa hàng",
                "mùa cao điểm",
            ]
        )
    )

    resp = await _suggest(client, admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["related_terms"] == ["vốn lưu động", "nhập hàng", "trả góp", "cửa hàng"]


async def test_focus_keyword_must_appear_in_the_article(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = message(good_answer(focus_keyword="vay tiêu dùng"))

    resp = await _suggest(client, admin)
    assert resp.status_code == 502
    assert resp.headers["x-error-code"] == "BLOG_SEO_FAILED"


async def test_focus_keyword_matches_whatever_the_case(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = message(good_answer(focus_keyword="  VAY VỐN trả góp  hằng ngày "))

    resp = await _suggest(client, admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["focus_keyword"] == "VAY VỐN trả góp hằng ngày"


@pytest.mark.parametrize(
    "description",
    [
        "Đầu tư không rủi ro vào cửa hàng.",
        "A risk-free way to fund shops.",
        "x" * 301,
        "   ",
    ],
)
async def test_unusable_meta_description_asks_to_try_again(client, claude, make_system_admin, description):
    admin = await make_system_admin()
    claude.reply = message(good_answer(meta_description=description))

    resp = await _suggest(client, admin)
    assert resp.status_code == 502
    assert resp.headers["x-error-code"] == "BLOG_SEO_FAILED"


async def test_refusal_or_cut_off_answer_is_a_failure(client, claude, make_system_admin):
    admin = await make_system_admin()

    claude.reply = {
        **message(None, stop_reason="refusal"),
        "content": [],
        "stop_details": {"type": "refusal", "category": None, "explanation": None},
    }
    refused = await _suggest(client, admin)
    assert refused.status_code == 502
    assert refused.headers["x-error-code"] == "BLOG_SEO_FAILED"

    claude.reply = message(None, stop_reason="max_tokens", text='{"titles": [{"title": "Vay')
    cut_off = await _suggest(client, admin)
    assert cut_off.status_code == 502
    assert cut_off.headers["x-error-code"] == "BLOG_SEO_FAILED"


# --------------------------------------------------------------------------- #
# Claude slow, unavailable or out of credit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reply",
    [
        api_error(529, "overloaded_error", "Overloaded"),
        api_error(500, "api_error", "Internal server error"),
        api_error(429, "rate_limit_error", "Too many requests"),
        httpx2.ReadTimeout("timed out"),
        httpx2.ConnectError("connection refused"),
    ],
    ids=["overloaded", "server-error", "rate-limited", "read-timeout", "unreachable"],
)
async def test_slow_or_unavailable_claude_asks_to_retry(client, claude, make_system_admin, reply):
    admin = await make_system_admin()
    claude.reply = reply

    resp = await _suggest(client, admin)
    assert resp.status_code == 503
    assert resp.headers["x-error-code"] == "BLOG_SEO_BUSY"


async def test_no_answer_within_the_deadline_asks_to_retry(client, claude, make_system_admin, monkeypatch):
    monkeypatch.setattr(seo_assistant, "DEADLINE_SECONDS", 0.05)
    admin = await make_system_admin()

    async def slow():
        await asyncio.sleep(1)
        return message(good_answer())

    claude.reply = slow
    resp = await _suggest(client, admin)
    assert resp.status_code == 503
    assert resp.headers["x-error-code"] == "BLOG_SEO_BUSY"


@pytest.mark.parametrize(
    "reply",
    [
        api_error(429, "rate_limit_error", "Workspace spend limit reached", "enforced_spend_limit_reached"),
        api_error(402, "billing_error", "Payment required"),
        api_error(400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API."),
    ],
    ids=["workspace-limit", "payment-required", "credit-balance"],
)
async def test_out_of_credit_pauses_suggestions(client, claude, make_system_admin, reply):
    admin = await make_system_admin()
    claude.reply = reply

    resp = await _suggest(client, admin)
    assert resp.status_code == 503
    assert resp.headers["x-error-code"] == "BLOG_SEO_NO_CREDIT"


async def test_rejected_api_key_is_a_failure_not_a_retry(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = api_error(401, "authentication_error", "invalid x-api-key")

    resp = await _suggest(client, admin)
    assert resp.status_code == 502
    assert resp.headers["x-error-code"] == "BLOG_SEO_FAILED"
    assert "x-api-key" not in resp.text
