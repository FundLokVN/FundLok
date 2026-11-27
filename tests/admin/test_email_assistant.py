"""Admin > Email "Write with Claude" (docs/specs/admin/email-assistant.md).

Claude is never called: the SDK runs against tests/assistants/stub.py.
"""
import pytest

from app.core.config import settings
from tests.assistants.stub import api_error, claude_stub, message  # noqa: F401 (fixture)

STATUS = "/admin/email/assistant"
DRAFT = "/admin/email/assistant/draft"

INSTRUCTION = "Nhắc các doanh nghiệp nộp tờ khai thuế GTGT tháng 11/2026 trước ngày 5 để được gia hạn tự động."
REQUEST = {"instruction": INSTRUCTION, "language": "vi", "template": "action_required"}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "text": text, "items": []}


def bullets(*items: str) -> dict:
    return {"type": "bullets", "text": "", "items": list(items)}


def good_draft(**overrides) -> dict:
    draft = {
        "subject": "Nhắc nộp tờ khai thuế GTGT tháng 11/2026",
        "heading": "Nộp tờ khai trước ngày 5",
        "body": [
            paragraph("Kính gửi Quý khách,"),
            paragraph("Vui lòng nộp tờ khai thuế GTGT tháng 11/2026 trước ngày 5 để được xem xét gia hạn tự động."),
            bullets("Tải tờ khai bản điện tử gốc", "Kiểm tra doanh thu đã khai"),
            paragraph("Trân trọng,"),
        ],
    }
    draft.update(overrides)
    return draft


@pytest.fixture
def claude(claude_stub):
    claude_stub.reply = message(good_draft())
    return claude_stub


async def _draft(client, user, **overrides):
    return await client.post(DRAFT, json={**REQUEST, **overrides}, headers=user["headers"])


# --------------------------------------------------------------------------- #
# Who sees it
# --------------------------------------------------------------------------- #


async def test_off_without_an_api_key(client, make_system_admin, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    admin = await make_system_admin()

    assert (await client.get(STATUS, headers=admin["headers"])).json() == {"enabled": False}
    resp = await _draft(client, admin)
    assert resp.status_code == 503
    assert resp.headers["x-error-code"] == "EMAIL_ASSIST_UNAVAILABLE"


async def test_starts_with_system_admins_only(client, claude, make_admin, make_system_admin):
    admin = await make_admin()
    system_admin = await make_system_admin()

    assert (await client.get(STATUS, headers=admin["headers"])).json() == {"enabled": False}
    assert (await client.get(STATUS, headers=system_admin["headers"])).json() == {"enabled": True}
    refused = await _draft(client, admin)
    assert refused.status_code == 403
    assert refused.headers["x-error-code"] == "EMAIL_ASSIST_UNAVAILABLE"
    assert claude.requests == []


async def test_all_admins_audience_includes_admins(client, claude, make_admin, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_ASSISTANT_AUDIENCE", "all_admins")
    admin = await make_admin()

    assert (await client.get(STATUS, headers=admin["headers"])).json() == {"enabled": True}
    assert (await _draft(client, admin)).status_code == 200


async def test_non_admins_cannot_ask(client, claude, make_user):
    sme = await make_user(role="SME")
    assert (await client.get(STATUS, headers=sme["headers"])).status_code == 403
    assert (await _draft(client, sme)).status_code == 403
    assert claude.requests == []


# --------------------------------------------------------------------------- #
# What is sent
# --------------------------------------------------------------------------- #


async def test_sends_the_instruction_language_template_and_current_draft(client, claude, make_system_admin):
    admin = await make_system_admin()

    resp = await _draft(
        client,
        admin,
        current={
            "subject": "Nhắc nộp tờ khai",
            "heading": "",
            "body": "Xin chào, vui lòng nộp tờ khai.",
            "button_label": "Nộp tờ khai",
        },
    )
    assert resp.status_code == 200, resp.text

    [sent] = claude.requests
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["fallbacks"] == "default"
    assert "rules for financial claims" in sent["system"]
    prompt = sent["messages"][0]["content"]
    assert "Write the email in Vietnamese." in prompt
    assert "a request for the reader to do something" in prompt
    assert INSTRUCTION in prompt
    assert "Subject: Nhắc nộp tờ khai" in prompt
    assert 'a button labelled "Nộp tờ khai"' in prompt


async def test_recipients_are_not_accepted(client, claude, make_system_admin):
    admin = await make_system_admin()
    resp = await _draft(client, admin, to=["owner@example.com"])
    assert resp.status_code == 422
    assert claude.requests == []


@pytest.mark.parametrize(
    "instruction",
    [
        "Gửi cho khách hàng CCCD 012345678901 về khoản vay",
        "Remind the investor with account 1903456789012 to confirm",
        "Gọi lại số 0912345678",
        "Write to nguyen.van.a@gmail.com about the delay",
    ],
    ids=["cccd", "bank-account", "phone", "outside-email"],
)
async def test_personal_data_never_reaches_claude(client, claude, make_system_admin, instruction):
    admin = await make_system_admin()
    resp = await _draft(client, admin, instruction=instruction)
    assert resp.status_code == 422
    assert resp.headers["x-error-code"] == "EMAIL_ASSIST_PERSONAL_DATA"
    assert claude.requests == []


async def test_fundlok_addresses_and_formatted_amounts_are_fine(client, claude, make_system_admin):
    admin = await make_system_admin()
    resp = await _draft(
        client,
        admin,
        instruction="Báo khoản phí kích hoạt 3.000.000 đồng; hỏi đáp qua support@fundlok.com.",
    )
    assert resp.status_code == 200, resp.text


async def test_personal_data_in_the_current_draft_is_refused_too(client, claude, make_system_admin):
    admin = await make_system_admin()
    resp = await _draft(client, admin, current={"body": "Số tài khoản: 1903456789012"})
    assert resp.status_code == 422
    assert resp.headers["x-error-code"] == "EMAIL_ASSIST_PERSONAL_DATA"


async def test_blank_or_oversized_instructions_are_refused(client, claude, make_system_admin):
    admin = await make_system_admin()
    assert (await _draft(client, admin, instruction="   ")).status_code == 422
    assert (await _draft(client, admin, instruction="x" * 2001)).status_code == 422
    assert (await _draft(client, admin, language="fr")).status_code == 422
    assert claude.requests == []


# --------------------------------------------------------------------------- #
# What comes back
# --------------------------------------------------------------------------- #


async def test_body_is_escaped_html_in_the_composers_paragraphs(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = message(
        good_draft(
            subject="  Nhắc   nộp tờ khai  ",
            body=[
                paragraph("Xin chào <b>Quý khách</b> & đối tác,"),
                bullets("Mục <script>1</script>", "  "),
                paragraph("   "),
                paragraph("Trân trọng,"),
            ],
        )
    )

    resp = await _draft(client, admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject"] == "Nhắc nộp tờ khai"
    assert body["body_html"] == (
        "<div>Xin chào &lt;b&gt;Quý khách&lt;/b&gt; &amp; đối tác,</div>"
        "<ul><li>Mục &lt;script&gt;1&lt;/script&gt;</li></ul>"
        "<div>Trân trọng,</div>"
    )
    assert body["body_text"] == "Xin chào <b>Quý khách</b> & đối tác,\n\n• Mục <script>1</script>\n\nTrân trọng,"


async def test_placeholders_and_figures_not_given_are_flagged(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = message(
        good_draft(
            body=[
                paragraph("Kính gửi Quý khách,"),
                paragraph("Hạn chót là [hạn chót]. Bước 1: tải tờ khai tháng 11/2026."),
                paragraph("Lãi suất tham khảo khoảng 15% một năm; xem [hướng dẫn](https://www.fundlok.com/blog)."),
                paragraph("Trân trọng,"),
            ]
        )
    )

    resp = await _draft(client, admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["placeholders"] == ["[hạn chót]"]
    # 11 and 2026 are in the instruction; 1 is a step number; 15 was invented.
    assert body["unverified_numbers"] == ["15"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject": "Lợi nhuận đảm bảo cho nhà đầu tư"},
        {"body": [paragraph("Đầu tư không rủi ro.")]},
        {"heading": "A risk-free way to invest"},
        {"subject": "   "},
        {"subject": "x" * 201},
        {"body": [paragraph("   ")]},
        {"body": [paragraph("x" * 10_001)]},
        {"body": [paragraph("Liên hệ chị Lan qua lan.nguyen@gmail.com")]},
    ],
    ids=[
        "promised-return",
        "no-risk",
        "risk-free",
        "blank-subject",
        "long-subject",
        "empty-body",
        "long-body",
        "outside-email",
    ],
)
async def test_unusable_drafts_ask_to_try_again(client, claude, make_system_admin, overrides):
    admin = await make_system_admin()
    claude.reply = message(good_draft(**overrides))

    resp = await _draft(client, admin)
    assert resp.status_code == 502
    assert resp.headers["x-error-code"] == "EMAIL_ASSIST_FAILED"


async def test_slow_or_out_of_credit_claude_uses_the_email_codes(client, claude, make_system_admin):
    admin = await make_system_admin()

    claude.reply = api_error(529, "overloaded_error", "Overloaded")
    busy = await _draft(client, admin)
    assert busy.status_code == 503
    assert busy.headers["x-error-code"] == "EMAIL_ASSIST_BUSY"

    claude.reply = api_error(429, "rate_limit_error", "Spend limit", "enforced_spend_limit_reached")
    no_credit = await _draft(client, admin)
    assert no_credit.status_code == 503
    assert no_credit.headers["x-error-code"] == "EMAIL_ASSIST_NO_CREDIT"


async def test_paragraphs_are_a_blank_line_apart(client, claude, make_system_admin):
    admin = await make_system_admin()
    claude.reply = message(good_draft(body=[paragraph("Kính gửi Quý khách,"), paragraph("Trân trọng,")]))

    resp = await _draft(client, admin)
    assert resp.json()["body_html"] == "<div>Kính gửi Quý khách,</div><div><br></div><div>Trân trọng,</div>"
