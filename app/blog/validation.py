"""Allowlist check for TipTap documents, plus slug and reading-time helpers.

The public site renders `content` from a fixed schema and never inserts raw
HTML, so this is the one gate between what an admin pastes into the editor and
what visitors see. Unknown node or mark types are refused (the admin should see
why); unknown attributes on allowed types are dropped silently, because TipTap
adds harmless ones (`class: null`, ...) as extensions evolve.

See docs/specs/blog/blog-posts.md §6.
"""
import json
import math
import re
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from app.core.config import settings

MAX_DEPTH = 20
MAX_BYTES = 512 * 1024
MAX_ALT = 200
MAX_HREF = 2000
MAX_IMAGE_URL = 500
SLUG_MAX_LENGTH = 80
WORDS_PER_MINUTE = 200

LINK_SCHEMES = {"http", "https", "mailto", "tel"}
ORDERED_LIST_TYPES = {"1", "a", "A", "i", "I"}

# A new post starts as one empty paragraph: ProseMirror's doc needs at least one block.
EMPTY_DOCUMENT: dict[str, Any] = {"type": "doc", "content": [{"type": "paragraph"}]}


class ContentError(ValueError):
    """The document breaks the allowlist; the message says where."""


# --------------------------------------------------------------------------- #
# Attribute checks: each takes the raw value (None when absent) and returns the
# value to keep, None to drop it, or raises ContentError.
# --------------------------------------------------------------------------- #


def _unsafe_characters(value: str) -> bool:
    # Whitespace and control characters let "java\tscript:" past a naive scheme
    # check; a backslash makes "/\evil.com" protocol-relative in browsers.
    return any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in value)


def _heading_level(value: Any) -> int:
    if value not in (2, 3) or isinstance(value, bool):
        raise ContentError("heading level must be 2 or 3")
    return value


def _ordered_list_start(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10_000:
        raise ContentError("ordered list start must be a positive integer")
    return value


def _ordered_list_type(value: Any) -> str | None:
    return value if value in ORDERED_LIST_TYPES else None


def _dimension(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10_000:
        return value
    return None


def _optional_title(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:MAX_ALT]
    return None


def check_href(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_HREF:
        raise ContentError("link href is missing or too long")
    if _unsafe_characters(value):
        raise ContentError("link href contains whitespace or control characters")
    if value.startswith("#") or (value.startswith("/") and not value.startswith("//")):
        return value
    scheme, sep, _ = value.partition(":")
    if not sep or scheme.lower() not in LINK_SCHEMES:
        raise ContentError("link must be http, https, mailto, tel, a /path or a #anchor")
    if scheme.lower() in {"http", "https"} and not urlsplit(value).netloc:
        raise ContentError("link has no host")
    return value


def check_image_url(value: Any) -> str:
    base = (settings.R2_PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        raise ContentError("images are not available: R2_PUBLIC_BASE_URL is not set")
    if not isinstance(value, str) or len(value) > MAX_IMAGE_URL:
        raise ContentError("image src is missing or too long")
    if _unsafe_characters(value) or ".." in value:
        raise ContentError("image src contains characters that are not allowed")
    if not value.startswith(f"{base}/blog/"):
        raise ContentError("image must be uploaded through the editor (FundLok media host)")
    return value


def check_alt(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentError("image alt text is required")
    value = value.strip()
    if len(value) > MAX_ALT:
        raise ContentError(f"image alt text is longer than {MAX_ALT} characters")
    return value


def _link_target(value: Any) -> str | None:
    return "_blank" if value == "_blank" else None


NODE_ATTRS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "doc": {},
    "paragraph": {},
    "heading": {"level": _heading_level},
    "text": {},
    "bulletList": {},
    "orderedList": {"start": _ordered_list_start, "type": _ordered_list_type},
    "listItem": {},
    "blockquote": {},
    "horizontalRule": {},
    "hardBreak": {},
    "image": {
        "src": check_image_url,
        "alt": check_alt,
        "title": _optional_title,
        "width": _dimension,
        "height": _dimension,
    },
}

MARK_ATTRS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "bold": {},
    "italic": {},
    "underline": {},
    "strike": {},
    "link": {"href": check_href, "target": _link_target},
}

# Nodes that end a run of text, so the plain-text form keeps words apart.
_BLOCK_NODES = {"paragraph", "heading", "listItem", "blockquote", "horizontalRule", "image"}


def _clean_attrs(allowed: dict[str, Callable[[Any], Any]], attrs: Any, where: str) -> dict[str, Any]:
    if attrs is not None and not isinstance(attrs, dict):
        raise ContentError(f"{where}: attrs must be an object")
    attrs = attrs or {}
    cleaned: dict[str, Any] = {}
    for name, check in allowed.items():
        try:
            value = check(attrs.get(name))
        except ContentError as exc:
            raise ContentError(f"{where}: {exc}") from None
        if value is not None:
            cleaned[name] = value
    return cleaned


def _clean_marks(marks: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(marks, list):
        raise ContentError(f"{where}: marks must be a list")
    cleaned = []
    for i, mark in enumerate(marks):
        here = f"{where}.marks[{i}]"
        mark_type = mark.get("type") if isinstance(mark, dict) else None
        if mark_type not in MARK_ATTRS:
            raise ContentError(f"{here}: mark type {mark_type!r} is not allowed")
        out: dict[str, Any] = {"type": mark_type}
        attrs = _clean_attrs(MARK_ATTRS[mark_type], mark.get("attrs"), here)
        if attrs:
            out["attrs"] = attrs
        cleaned.append(out)
    return cleaned


def _clean_node(node: Any, where: str, depth: int, text: list[str]) -> dict[str, Any]:
    if depth > MAX_DEPTH:
        raise ContentError(f"{where}: nested more than {MAX_DEPTH} levels deep")
    node_type = node.get("type") if isinstance(node, dict) else None
    if node_type not in NODE_ATTRS or (node_type == "doc" and depth > 0):
        raise ContentError(f"{where}: node type {node_type!r} is not allowed")

    out: dict[str, Any] = {"type": node_type}
    attrs = _clean_attrs(NODE_ATTRS[node_type], node.get("attrs"), where)
    if attrs:
        out["attrs"] = attrs

    if node_type == "text":
        value = node.get("text")
        if not isinstance(value, str) or not value:
            raise ContentError(f"{where}: text node has no text")
        out["text"] = value
        text.append(value)
        if node.get("marks"):
            out["marks"] = _clean_marks(node["marks"], where)
        return out

    if node_type == "hardBreak":
        text.append(" ")
    children = node.get("content")
    if children is not None:
        if not isinstance(children, list):
            raise ContentError(f"{where}: content must be a list")
        out["content"] = [
            _clean_node(child, f"{where}.content[{i}]", depth + 1, text) for i, child in enumerate(children)
        ]
    if node_type in _BLOCK_NODES:
        text.append(" ")
    return out


def validate_document(document: Any) -> tuple[dict[str, Any], str]:
    """Return (cleaned document, plain text) or raise ContentError."""
    if not isinstance(document, dict) or document.get("type") != "doc":
        raise ContentError("content must be a TipTap document of type 'doc'")
    if len(json.dumps(document, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise ContentError(f"content is larger than {MAX_BYTES // 1024} KB")
    text: list[str] = []
    cleaned = _clean_node(document, "doc", 0, text)
    return cleaned, re.sub(r"\s+", " ", "".join(text)).strip()


def validate_cover(url: str | None, alt: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    try:
        return check_image_url(url), check_alt(alt)
    except ContentError as exc:
        raise ContentError(f"cover image: {exc}") from None


def reading_minutes(plain_text: str) -> int:
    return max(1, math.ceil(len(plain_text.split()) / WORDS_PER_MINUTE))


def slugify(text: str, max_length: int = SLUG_MAX_LENGTH) -> str:
    """'Lãi suất vay vốn 2026' -> 'lai-suat-vay-von-2026'. May return ''."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > max_length:
        cut = slug[:max_length]
        # Prefer ending on a whole word.
        if slug[max_length] != "-" and "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        slug = cut.strip("-")
    return slug
