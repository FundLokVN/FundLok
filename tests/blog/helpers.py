"""Document builders, constants and the media-settings fixture for the blog tests.

Each test module imports `public_media` (autouse) from here rather than from a
tests/blog/conftest.py: a nested conftest without __init__.py is imported as
the module "conftest" and shadows the root one that other tests import from.
"""
import pytest

from app.core.config import settings

MEDIA_BASE = "https://media.example.com"
IMAGE_URL = f"{MEDIA_BASE}/blog/7a0c3c1e.png"


@pytest.fixture(autouse=True)
def public_media(monkeypatch):
    """A configured public bucket; individual tests unset it to test the 503."""
    monkeypatch.setattr(settings, "R2_PUBLIC_BUCKET", "fundlok-public-test")
    monkeypatch.setattr(settings, "R2_PUBLIC_BASE_URL", MEDIA_BASE)


def text(value: str, marks: list | None = None) -> dict:
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def paragraph(*children: dict) -> dict:
    return {"type": "paragraph", "content": list(children)}


def heading(level: int, value: str) -> dict:
    return {"type": "heading", "attrs": {"level": level}, "content": [text(value)]}


def image(src: str = IMAGE_URL, alt: str | None = "Biểu đồ lãi suất") -> dict:
    attrs = {"src": src}
    if alt is not None:
        attrs["alt"] = alt
    return {"type": "image", "attrs": attrs}


def link(href: str) -> list:
    return [{"type": "link", "attrs": {"href": href}}]


def document(*blocks: dict) -> dict:
    return {"type": "doc", "content": list(blocks)}


def article() -> dict:
    return document(
        heading(2, "Bảng tóm tắt"),
        paragraph(text("Lãi suất vay vốn cho doanh nghiệp nhỏ "), text("tham khảo", link("https://www.fundlok.com/rate"))),
        image(),
    )


def update_body(post: dict, **overrides) -> dict:
    """A full PUT body built from a post as the API returned it."""
    body = {
        "title": post["title"],
        "slug": post["slug"],
        "seo_title": post["seo_title"],
        "excerpt": post["excerpt"],
        "content": post["content"],
        "cover_image_url": post["cover_image_url"],
        "cover_alt": post["cover_alt"],
        "focus_keyword": post["focus_keyword"],
        "secondary_keywords": post["secondary_keywords"],
        "version": post["version"],
    }
    body.update(overrides)
    return body
