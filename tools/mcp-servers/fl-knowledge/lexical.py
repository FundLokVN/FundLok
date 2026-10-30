"""
Keyword (BM25) half of fl-knowledge's hybrid search (ADR-010).

Pure Python, no dependencies, so the MCP server stays light. The index is
built in memory from the same chunks the Pinecone index is built from
(chunking.build_corpus) and holds a few thousand chunks, so a build takes a
fraction of a second.

Why a custom tokenizer: FundLok's docs are full of identifiers a plain
word-splitter mangles -- UNDER_REVIEW, Q-023, D-027, PROVISIONAL(Q-D5),
ac9f152fa24e, INV-5, app/direct_transfer. For every token we index the whole
identifier AND its parts, so a query for "UNDER_REVIEW", "under review" or
"review" all find it. Vietnamese text is indexed both as written and with
diacritics folded, so a query typed without accents still matches.
"""

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"\w[\w\-]*")
_PART_SPLIT_RE = re.compile(r"[_\-]+")


def fold_diacritics(text: str) -> str:
    """Lowercase ASCII-fold: 'Phụ lục' -> 'phu luc', 'đ' -> 'd'."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower()


def tokenize(text: str) -> list[str]:
    """Identifier-aware tokens. See the module docstring."""
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(unicodedata.normalize("NFC", text).lower()):
        raw = raw.strip("-_")
        if not raw:
            continue
        variants = {raw, fold_diacritics(raw)}
        for variant in variants:
            tokens.append(variant)
            if "_" in variant or "-" in variant:
                tokens.extend(p for p in _PART_SPLIT_RE.split(variant) if p)
    return tokens


@dataclass(frozen=True)
class LexicalHit:
    doc_index: int
    score: float


class BM25Index:
    """Okapi BM25 over a fixed list of documents (strings)."""

    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_count = len(documents)
        self._postings: dict[str, dict[int, int]] = defaultdict(dict)
        self._lengths: list[int] = []
        for i, doc in enumerate(documents):
            counts = Counter(tokenize(doc))
            self._lengths.append(sum(counts.values()))
            for term, tf in counts.items():
                self._postings[term][i] = tf
        self._avg_len = (sum(self._lengths) / self.doc_count) if self.doc_count else 0.0

    def _idf(self, term: str) -> float:
        df = len(self._postings.get(term, ()))
        return math.log(1.0 + (self.doc_count - df + 0.5) / (df + 0.5))

    def search(self, query: str, limit: int = 20) -> list[LexicalHit]:
        if not self.doc_count:
            return []
        # Query terms are de-duplicated: a repeated word shouldn't double-count.
        terms = set(tokenize(query))
        scores: dict[int, float] = defaultdict(float)
        for term in terms:
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = self._idf(term)
            for doc, tf in posting.items():
                norm = 1.0 - self.b + self.b * (self._lengths[doc] / self._avg_len)
                scores[doc] += idf * (tf * (self.k1 + 1.0)) / (tf + self.k1 * norm)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [LexicalHit(doc, score) for doc, score in ranked[:limit]]
