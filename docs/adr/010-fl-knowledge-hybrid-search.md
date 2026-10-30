# ADR-010: fl-knowledge Moves to Hybrid Search (Semantic + BM25)

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | October 2026 |
| **Deciders** | Edward Wong (CTO) |
| **Builds on** | ADR-005 (BM25), ADR-006 (Pinecone + local embeddings), ADR-007 (PDFs) |

## Context

ADR-006 replaced BM25 with semantic search because keyword matching cannot
connect "how lenders get paid back" to "pro-rata distributions". That fixed
paraphrase queries but left the opposite gap: FundLok's docs are dense with
exact identifiers (`UNDER_REVIEW`, `Q-023`, `D-027`, `PROVISIONAL(Q-D5)`,
migration hashes, file names), and a small embedding model (MiniLM, 384-dim)
blurs them. Measured on the October 2026 corpus, `UNDER_REVIEW` returned
month-end-review sections and `Q-023` returned two regulation PDFs, while a
BM25 pass put the right documents first.

A separate defect surfaced while measuring: vector IDs were numbered across
the whole corpus, so adding one document shifted every later ID and each
reindex left the old vectors behind. The index held 4,880 vectors for 2,341
chunks, and results contained duplicate and out-of-date text.

## Decision

1. **Hybrid retrieval.** `search_fl_docs` runs both engines and merges them
   with Reciprocal Rank Fusion (k = 60):
   - *Semantic*: unchanged (MiniLM embeddings, Pinecone, cosine).
   - *Keyword*: an in-memory BM25 index (`lexical.py`, pure Python, no new
     dependency) built from the same chunks (`chunking.build_corpus`). The
     tokenizer keeps whole identifiers and also their parts
     (`UNDER_REVIEW` -> `under_review`, `under`, `review`) and indexes
     Vietnamese both with and without diacritics. Source path and heading
     are searchable.
2. **Query-aware weighting.** Keyword weight is always 1.0. Semantic weight is
   1.0 for plain-language queries, 0.4 when identifiers are mixed with words,
   0.1 when every token is an identifier (an exact lookup). Equal weights let
   semantic noise outrank exact matches on identifier queries.
3. **Graceful degradation.** If Pinecone is unreachable or unconfigured, the
   keyword engine still answers and the response says so.
4. **`SEARCH_MODE=hybrid|semantic|keyword`** (default `hybrid`) for A/B checks.
5. **Stable vector IDs and stale cleanup.** IDs are numbered per source file
   (`<source>::<n>`), and `build_index.py` deletes every vector not produced by
   the current run, after the upsert.

Results are fused on a content key (source + heading + text prefix), not on
Pinecone IDs, so keyword and semantic hits on the same chunk merge even if
the on-disk docs have drifted since the last reindex, and duplicate vectors
collapse.

## Consequences

- Keyword results always reflect the docs on disk; semantic results reflect
  the last reindex. After editing docs, run `reindex_fl_docs`.
- The keyword index is rebuilt in memory on first search after server start
  (a fraction of a second for ~2,300 chunks) and after each reindex.
- No new dependency, no extra service, no cost.
- Not addressed here: superseded documents (e.g. `docs/specs/banking/vpbank-proposal/_superseded/`)
  are still indexed and can outrank current ones on concept queries. Excluding
  them is a separate, small change.
- Tests: `tests/fl_knowledge/` (offline; no Pinecone, model or network).

## Alternatives considered

- **Pinecone sparse-dense vectors**: keeps everything in one query, but needs a
  dotproduct index (a migration of the existing cosine index) and a sparse
  encoder; more moving parts for the same result at this corpus size.
- **A larger embedding model**: helps paraphrase queries, not exact
  identifiers, and costs download size and latency.
- **A reranker**: worthwhile only if hybrid ranking proves insufficient.
