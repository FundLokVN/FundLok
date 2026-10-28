"""
fl-knowledge: an MCP server that indexes FundLok's own markdown knowledge
base (specs, ADRs, handoffs, CLAUDE.md, AGENTS.md, README) and exposes a
semantic-search tool over it, backed by a Pinecone vector index.

Architecture:
- Embeddings are generated locally with sentence-transformers
  (all-MiniLM-L6-v2, 384-dim) -- no per-query API cost, no network
  dependency for the embedding step itself.
- Vectors + metadata (source file, heading, text) live in a Pinecone
  serverless index. Populate/refresh it with build_index.py in this folder
  -- this server only queries, it never writes to the index.
- Search is HYBRID (ADR-010): the semantic ranking above is merged with an
  in-memory BM25 keyword ranking (lexical.py) using Reciprocal Rank Fusion
  (hybrid.py). Embeddings match on meaning ("how lenders get paid back" finds
  "pro-rata distributions"); BM25 matches exact identifiers that embeddings
  blur (UNDER_REVIEW, Q-023, a migration hash). SEARCH_MODE=semantic|keyword
  switches either half off for comparison.

Setup: see README.md in this folder. Requires PINECONE_API_KEY (via .env in
this folder) and a Pinecone index already populated by build_index.py.

This is a stdio MCP server -- it's meant to be launched by an MCP client
(Claude Code, via .mcp.json at the repo root), not run standalone in a
terminal for interactive use.
"""

import asyncio
import os

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pinecone import Pinecone
from sentence_transformers import SentenceTransformer

from chunking import build_corpus
from hybrid import MODES, Hit, format_hit, fuse, hit_from_keyword, hit_from_semantic, semantic_weight_for
from lexical import BM25Index

load_dotenv()

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "fl-knowledge")
SEARCH_MODE = os.environ.get("SEARCH_MODE", "hybrid")
if SEARCH_MODE not in MODES:
    raise SystemExit(f"SEARCH_MODE must be one of {MODES}, got {SEARCH_MODE!r}")
CANDIDATES = 20  # how many results each engine contributes before fusion


class VectorSearch:
    """Lazy-loaded wrapper around the embedding model + Pinecone index.

    Loaded lazily (not at import time) so that `list_tools` still works even
    if PINECONE_API_KEY is missing or the index doesn't exist yet -- the
    error only surfaces when a search is actually attempted, with a message
    pointing at build_index.py.
    """

    def __init__(self):
        self._model: SentenceTransformer | None = None
        self._index = None

    def _ensure_ready(self):
        if self._model is None:
            self._model = SentenceTransformer(EMBEDDING_MODEL)
        if self._index is None:
            api_key = os.environ.get("PINECONE_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "PINECONE_API_KEY is not set. Copy .env.example to .env "
                    "in tools/mcp-servers/fl-knowledge/ and paste your key in."
                )
            pc = Pinecone(api_key=api_key)
            existing = {i["name"] for i in pc.list_indexes()}
            if INDEX_NAME not in existing:
                raise RuntimeError(
                    f"Pinecone index '{INDEX_NAME}' doesn't exist yet. Run "
                    f"`python3 build_index.py` in this folder first."
                )
            self._index = pc.Index(INDEX_NAME)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        self._ensure_ready()
        vector = self._model.encode(query, normalize_embeddings=True).tolist()
        result = self._index.query(vector=vector, top_k=top_k, include_metadata=True)
        return result.get("matches", [])

    def stats(self) -> dict:
        self._ensure_ready()
        return self._index.describe_index_stats()


class KeywordSearch:
    """Lazy in-memory BM25 over the current on-disk docs. Rebuilt on demand
    (see reset()) after a reindex, so keyword results always reflect the docs
    as they are now, even if Pinecone has not been reindexed yet."""

    def __init__(self):
        self._chunks = None
        self._index: BM25Index | None = None

    def reset(self):
        self._chunks = None
        self._index = None

    def _ensure_ready(self):
        if self._index is None:
            self._chunks = build_corpus()
            # Source path and heading are searchable too, so "funding-window"
            # or "Tranche state machine" finds the right file/section.
            self._index = BM25Index([f"{c.source}\n{c.heading}\n{c.text}" for c in self._chunks])

    def search(self, query: str, limit: int) -> list[Hit]:
        self._ensure_ready()
        hits = []
        for rank, h in enumerate(self._index.search(query, limit), start=1):
            c = self._chunks[h.doc_index]
            hits.append(hit_from_keyword(c.source, c.heading, c.text, h.score, rank))
        return hits


search_backend = VectorSearch()
keyword_backend = KeywordSearch()
server = Server("fl-knowledge")


def run_search(query: str, top_k: int) -> tuple[list[Hit], str]:
    """Hybrid search with graceful degradation: if the semantic side is
    unavailable (no API key, index missing, network) the keyword side still
    answers, and the note says so. Returns (hits, note)."""
    semantic: list[Hit] = []
    note = ""
    if SEARCH_MODE in ("hybrid", "semantic"):
        try:
            matches = search_backend.search(query, top_k=max(top_k * 4, CANDIDATES))
            semantic = [hit_from_semantic(m, rank) for rank, m in enumerate(matches, start=1)]
        except RuntimeError as e:
            if SEARCH_MODE == "semantic":
                raise
            note = f"(semantic search unavailable, keyword results only: {e})\n\n"
    keyword = (
        keyword_backend.search(query, max(top_k * 4, CANDIDATES))
        if SEARCH_MODE in ("hybrid", "keyword") else []
    )
    mode = SEARCH_MODE if (semantic or SEARCH_MODE != "hybrid") else "keyword"
    return fuse(semantic, keyword, top_k, mode=mode, semantic_weight=semantic_weight_for(query)), note


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_fl_docs",
            description=(
                "Searches the FundLok project knowledge base -- CLAUDE.md, AGENTS.md, "
                "README.md, and everything under docs/ (architecture decisions, ADRs, "
                "spec docs, handoffs, PR writeups, and external source documents like "
                "regulations under docs/regulatory/) -- searched with hybrid ranking: "
                "semantic embeddings (Pinecone) merged with BM25 keyword matching, so both "
                "concept questions and exact identifiers (state names, Q-/D- ids, "
                "revision hashes) work. Use this "
                "before answering any question about existing design decisions, prior "
                "spec iterations, regulatory requirements, or codebase conventions, "
                "since this information may not be in the current conversation context. "
                "Prefer this over re-reading whole files when only one section is needed. "
                "Note: for exhaustive review of a single long document (e.g. a full "
                "gap analysis against a regulation), read that file directly instead -- "
                "this tool returns only the top-k most relevant chunks, not full coverage."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A natural-language question, or exact identifiers/keywords, e.g. 'how do lenders get paid after a borrower repays', 'brankas webhook retry behavior' or 'UNDER_REVIEW Q-023'.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max number of matching sections to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="reindex_fl_docs",
            description=(
                "Re-embed and re-upsert all docs into Pinecone. Run this after "
                "editing specs/ADRs/handoffs so search reflects the latest "
                "content -- equivalent to running build_index.py."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "reindex_fl_docs":
        # Reuse build_index.py's logic rather than duplicating it.
        import build_index
        build_index.main()
        keyword_backend.reset()
        stats = search_backend.stats()
        return [TextContent(type="text", text=f"Reindexed. Index stats: {stats}")]

    if name == "search_fl_docs":
        query = arguments.get("query", "")
        top_k = int(arguments.get("top_k", 5))
        try:
            hits, note = run_search(query, top_k)
        except RuntimeError as e:
            return [TextContent(type="text", text=str(e))]

        if not hits:
            return [TextContent(type="text", text=f"{note}No matching sections found.")]

        return [TextContent(type="text", text=note + "\n\n---\n\n".join(format_hit(h) for h in hits))]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
