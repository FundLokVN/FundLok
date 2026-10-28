"""
Hybrid ranking for fl-knowledge (ADR-010): semantic (Pinecone) + keyword
(BM25) merged with Reciprocal Rank Fusion.

RRF scores each chunk 1/(k + rank) in every list it appears in and adds them
up. It needs no score calibration between the two engines (cosine similarity
and BM25 are on different scales) and rewards chunks both engines like.

This module has no Pinecone/model imports so it can be unit-tested offline.
"""

import hashlib
import re
from dataclasses import dataclass, field

RRF_K = 60  # standard constant from the original RRF paper; damps the top ranks
MAX_TEXT_CHARS = 2000  # same cap build_index.py applies to stored metadata text

MODES = ("hybrid", "semantic", "keyword")

# A token with a letter AND a digit or underscore: UNDER_REVIEW, Q-023, T0,
# ac9f152fa24e, PROVISIONAL(Q-D5). Such a query is looking for an exact thing,
# which embeddings blur, so the keyword engine gets full weight and the
# semantic engine is damped. Plain-language queries weigh both equally.
_IDENTIFIER_RE = re.compile(r"(?=\S*[A-Za-z])(?=\S*[\d_])\S+")
MIXED_SEMANTIC_WEIGHT = 0.4  # identifiers mixed with ordinary words
LOOKUP_SEMANTIC_WEIGHT = 0.1  # every token is an identifier: an exact lookup


def has_identifier(query: str) -> bool:
    return bool(_IDENTIFIER_RE.search(query))


def semantic_weight_for(query: str) -> float:
    """How much to trust the semantic engine for this query (keyword stays 1.0)."""
    tokens = query.split()
    identifiers = [t for t in tokens if _IDENTIFIER_RE.fullmatch(t)]
    if not identifiers:
        return 1.0
    return LOOKUP_SEMANTIC_WEIGHT if len(identifiers) == len(tokens) else MIXED_SEMANTIC_WEIGHT


@dataclass
class Hit:
    source: str
    heading: str
    text: str
    semantic_rank: int | None = None
    semantic_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    fused_score: float = 0.0
    key: str = field(default="", repr=False)


def chunk_key(source: str, heading: str, text: str) -> str:
    """Identity of a chunk that does not depend on Pinecone vector IDs, so a
    keyword hit and a semantic hit on the same chunk fuse correctly even if
    the on-disk docs have drifted since the last reindex, and so duplicate
    vectors collapse to one result."""
    raw = f"{source}\x00{heading}\x00{text[:300]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def hit_from_semantic(match: dict, rank: int) -> Hit:
    meta = match.get("metadata", {}) or {}
    source, heading, text = meta.get("source", "unknown"), meta.get("heading", ""), meta.get("text", "")
    return Hit(
        source=source, heading=heading, text=text,
        semantic_rank=rank, semantic_score=match.get("score"),
        key=chunk_key(source, heading, text),
    )


def hit_from_keyword(source: str, heading: str, text: str, score: float, rank: int) -> Hit:
    text = text[:MAX_TEXT_CHARS]
    return Hit(
        source=source, heading=heading, text=text,
        keyword_rank=rank, keyword_score=score,
        key=chunk_key(source, heading, text),
    )


def fuse(semantic: list[Hit], keyword: list[Hit], top_k: int,
         mode: str = "hybrid", rrf_k: int = RRF_K,
         semantic_weight: float = 1.0, keyword_weight: float = 1.0) -> list[Hit]:
    """Merge the two ranked lists. `semantic` and `keyword` must each be in
    rank order. In "semantic"/"keyword" mode only that list is used (kept for
    A/B comparison and as a fallback when one engine is unavailable)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    lists: list[list[Hit]] = []
    if mode in ("hybrid", "semantic"):
        lists.append(semantic)
    if mode in ("hybrid", "keyword"):
        lists.append(keyword)

    merged: dict[str, Hit] = {}
    for hits in lists:
        for rank, hit in enumerate(hits, start=1):
            current = merged.get(hit.key)
            if current is None:
                merged[hit.key] = current = Hit(
                    source=hit.source, heading=hit.heading, text=hit.text, key=hit.key,
                )
            # A duplicate within one list keeps its best (first) rank only.
            if hit.semantic_rank is not None and current.semantic_rank is None:
                current.semantic_rank, current.semantic_score = hit.semantic_rank, hit.semantic_score
                current.fused_score += semantic_weight / (rrf_k + rank)
            if hit.keyword_rank is not None and current.keyword_rank is None:
                current.keyword_rank, current.keyword_score = hit.keyword_rank, hit.keyword_score
                current.fused_score += keyword_weight / (rrf_k + rank)
            # Prefer the longer text (the keyword side is not cut by Pinecone's limit).
            if len(hit.text) > len(current.text):
                current.text = hit.text

    ranked = sorted(
        merged.values(),
        key=lambda h: (-h.fused_score, h.semantic_rank or 10**9, h.keyword_rank or 10**9),
    )
    return ranked[:top_k]


def format_hit(hit: Hit) -> str:
    parts = []
    if hit.semantic_rank is not None:
        parts.append(f"semantic #{hit.semantic_rank}")
    if hit.keyword_rank is not None:
        parts.append(f"keyword #{hit.keyword_rank}")
    return (
        f"### {hit.source} — {hit.heading}  "
        f"(score: {hit.fused_score:.4f} · {' + '.join(parts) or 'n/a'})\n{hit.text}"
    )
