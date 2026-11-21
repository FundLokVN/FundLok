"""The TipTap allowlist and slug helpers (docs/specs/blog/blog-posts.md §6)."""
import pytest

from app.blog.validation import (
    ContentError,
    reading_minutes,
    slugify,
    validate_cover,
    validate_document,
)
from tests.blog.helpers import (  # noqa: F401 (public_media is an autouse fixture)
    public_media,
    IMAGE_URL,
    MEDIA_BASE,
    article,
    document,
    heading,
    image,
    link,
    paragraph,
    text,
)


def test_valid_document_passes_and_yields_plain_text():
    cleaned, plain = validate_document(article())
    assert cleaned == article()
    assert plain == "Bảng tóm tắt Lãi suất vay vốn cho doanh nghiệp nhỏ tham khảo"


@pytest.mark.parametrize(
    "node",
    [
        {"type": "codeBlock", "content": [text("x")]},
        {"type": "iframe", "attrs": {"src": "https://evil.example"}},
        {"type": "script"},
        {"type": "doc", "content": []},
    ],
)
def test_rejects_unknown_node_and_mark_types(node):
    with pytest.raises(ContentError, match="not allowed"):
        validate_document(document(node))


def test_rejects_unknown_mark_type():
    with pytest.raises(ContentError, match="mark type 'code'"):
        validate_document(document(paragraph(text("x", [{"type": "code"}]))))


@pytest.mark.parametrize("level", [1, 4, 5, "2", None])
def test_rejects_h1_and_h4(level):
    with pytest.raises(ContentError, match="heading level"):
        validate_document(document({"type": "heading", "attrs": {"level": level}, "content": [text("x")]}))


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "java\tscript:alert(1)",
        " javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "//evil.example/path",
        "/\\evil.example",
        "https://",
        "",
    ],
)
def test_rejects_unsafe_link_hrefs(href):
    with pytest.raises(ContentError):
        validate_document(document(paragraph(text("x", link(href)))))


@pytest.mark.parametrize(
    "href",
    ["https://www.fundlok.com/rate", "http://example.com", "mailto:hi@fundlok.com", "tel:+84901234567", "/faq", "#bang-tom-tat"],
)
def test_accepts_safe_link_hrefs(href):
    validate_document(document(paragraph(text("x", link(href)))))


@pytest.mark.parametrize(
    "src",
    [
        "https://evil.example/blog/x.png",
        f"{MEDIA_BASE}/other/x.png",
        f"{MEDIA_BASE}/blog/../secret.png",
        "data:image/png;base64,iVBORw0KGgo=",
        None,
    ],
)
def test_rejects_foreign_or_data_image_src(src):
    with pytest.raises(ContentError, match="image"):
        validate_document(document(image(src=src)))


@pytest.mark.parametrize("alt", [None, "", "   ", "x" * 201])
def test_rejects_missing_or_long_alt(alt):
    with pytest.raises(ContentError, match="alt"):
        validate_document(document(image(alt=alt)))


def test_rejects_images_when_media_host_is_unset(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", None)
    with pytest.raises(ContentError, match="R2_PUBLIC_BASE_URL"):
        validate_document(document(image()))


def test_drops_unknown_attributes():
    raw = document(
        {"type": "paragraph", "attrs": {"class": "x", "style": "color:red"}, "content": [text("a")]},
        {"type": "image", "attrs": {"src": IMAGE_URL, "alt": "A", "onerror": "alert(1)", "title": None}},
        paragraph(text("b", [{"type": "link", "attrs": {"href": "/faq", "class": None, "target": "_top"}}])),
    )
    cleaned, _ = validate_document(raw)
    assert cleaned["content"][0] == {"type": "paragraph", "content": [text("a")]}
    assert cleaned["content"][1] == {"type": "image", "attrs": {"src": IMAGE_URL, "alt": "A"}}
    assert cleaned["content"][2]["content"][0]["marks"] == [{"type": "link", "attrs": {"href": "/faq"}}]


def test_rejects_documents_that_are_too_deep_or_too_large():
    node = paragraph(text("x"))
    for _ in range(25):
        node = {"type": "blockquote", "content": [node]}
    with pytest.raises(ContentError, match="levels deep"):
        validate_document(document(node))

    with pytest.raises(ContentError, match="KB"):
        validate_document(document(paragraph(text("x" * (600 * 1024)))))


def test_rejects_a_non_document_root():
    with pytest.raises(ContentError, match="type 'doc'"):
        validate_document({"type": "paragraph"})
    with pytest.raises(ContentError, match="no text"):
        validate_document(document(paragraph({"type": "text", "text": ""})))


def test_cover_needs_our_host_and_alt():
    assert validate_cover(None, None) == (None, None)
    assert validate_cover(IMAGE_URL, " Ảnh bìa ") == (IMAGE_URL, "Ảnh bìa")
    with pytest.raises(ContentError, match="cover image"):
        validate_cover(IMAGE_URL, None)
    with pytest.raises(ContentError, match="cover image"):
        validate_cover("https://evil.example/blog/x.png", "A")


@pytest.mark.parametrize(
    "title, slug",
    [
        ("Lãi suất vay vốn doanh nghiệp 2026", "lai-suat-vay-von-doanh-nghiep-2026"),
        ("Đồng Nai: hướng dẫn ĐĂNG KÝ", "dong-nai-huong-dan-dang-ky"),
        ("Next 16.1 — có gì mới?", "next-16-1-co-gi-moi"),
        ("!!!", ""),
    ],
)
def test_slugify_strips_vietnamese_diacritics(title, slug):
    assert slugify(title) == slug


def test_slugify_cuts_on_a_word_boundary():
    slug = slugify("doanh nghiệp " * 20)
    assert len(slug) <= 80
    assert not slug.endswith("-")
    assert slug.endswith("nghiep")


def test_reading_minutes_rounds_up():
    assert reading_minutes("") == 1
    assert reading_minutes("từ " * 200) == 1
    assert reading_minutes("từ " * 201) == 2
