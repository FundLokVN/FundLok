"""ADR-010: hybrid search for fl-knowledge. Offline; fusion and keyword
ranking are tested directly, with fake semantic results."""
import pytest

from chunking import Chunk, assign_ids
from hybrid import Hit, chunk_key, fuse, hit_from_keyword, hit_from_semantic
from lexical import BM25Index, fold_diacritics, tokenize


# --- tokenizer ---------------------------------------------------------------

def test_tokenize_keeps_identifier_and_its_parts():
    toks = tokenize("UNDER_REVIEW Q-023 ac9f152fa24e")
    assert {"under_review", "under", "review", "q-023", "q", "023", "ac9f152fa24e"} <= set(toks)


def test_tokenize_folds_vietnamese_diacritics_and_keeps_original():
    toks = tokenize("Phụ lục quyết định")
    assert "phu" in toks and "luc" in toks and "quyet" in toks
    assert "phụ" in toks
    assert fold_diacritics("Đồng") == "dong"


# --- BM25 --------------------------------------------------------------------

DOCS = [
    "RESERVED -> SIGNED when the investor signs the acceptance",
    "UNDER_REVIEW means staff decides after a receipt mismatch or SME dispute",
    "Stub interest is a separate dated obligation outside T0",
    "Pro-rata distributions are paid to lenders after repayment",
]


def test_bm25_exact_identifier_ranks_first():
    idx = BM25Index(DOCS)
    assert idx.search("UNDER_REVIEW")[0].doc_index == 1


def test_bm25_identifier_part_matches_spaced_query():
    idx = BM25Index(DOCS)
    assert idx.search("under review staff")[0].doc_index == 1


def test_bm25_no_match_returns_empty():
    assert BM25Index(DOCS).search("zzzzqqqq") == []


def test_bm25_empty_index_is_safe():
    assert BM25Index([]).search("anything") == []


def test_bm25_repeated_query_word_does_not_double_count():
    idx = BM25Index(DOCS)
    assert idx.search("stub")[0].score == pytest.approx(idx.search("stub stub stub")[0].score)


# --- fusion ------------------------------------------------------------------

def sem(source, heading, text, rank):
    return hit_from_semantic({"metadata": {"source": source, "heading": heading, "text": text}, "score": 0.5}, rank)


def kw(source, heading, text, rank):
    return hit_from_keyword(source, heading, text, 1.0, rank)


def test_chunk_found_by_both_engines_beats_chunk_found_by_one():
    semantic = [sem("a.md", "A", "alpha", 1), sem("b.md", "B", "beta", 2)]
    keyword = [kw("c.md", "C", "gamma", 1), kw("b.md", "B", "beta", 2)]
    fused = fuse(semantic, keyword, top_k=3)
    assert fused[0].source == "b.md"
    assert fused[0].semantic_rank == 2 and fused[0].keyword_rank == 2


def test_keyword_only_hit_survives_fusion():
    fused = fuse([sem("a.md", "A", "alpha", 1)], [kw("id.md", "ID", "UNDER_REVIEW", 1)], top_k=5)
    assert {h.source for h in fused} == {"a.md", "id.md"}


def test_duplicate_semantic_vectors_collapse_to_one_result():
    dup = [sem("a.md", "A", "same text", 1), sem("a.md", "A", "same text", 2), sem("b.md", "B", "other", 3)]
    fused = fuse(dup, [], top_k=5)
    assert [h.source for h in fused] == ["a.md", "b.md"]


def test_top_k_is_respected_and_order_is_deterministic():
    semantic = [sem(f"{i}.md", "H", f"t{i}", i) for i in range(1, 8)]
    assert fuse(semantic, [], top_k=3) == fuse(semantic, [], top_k=3)
    assert len(fuse(semantic, [], top_k=3)) == 3


def test_semantic_only_and_keyword_only_modes():
    semantic, keyword = [sem("s.md", "S", "x", 1)], [kw("k.md", "K", "y", 1)]
    assert [h.source for h in fuse(semantic, keyword, 5, mode="semantic")] == ["s.md"]
    assert [h.source for h in fuse(semantic, keyword, 5, mode="keyword")] == ["k.md"]
    with pytest.raises(ValueError):
        fuse(semantic, keyword, 5, mode="nonsense")


def test_chunk_key_ignores_text_beyond_300_chars_but_not_source():
    base = "x" * 300
    assert chunk_key("a.md", "H", base + "1") == chunk_key("a.md", "H", base + "2")
    assert chunk_key("a.md", "H", base) != chunk_key("b.md", "H", base)


# --- stable vector ids (stale-vector bug) -------------------------------------

def test_vector_ids_are_stable_when_an_earlier_file_is_added():
    a = [Chunk("docs/b.md", "h1", "t"), Chunk("docs/b.md", "h2", "t")]
    before = assign_ids(a)
    after = assign_ids([Chunk("docs/a.md", "h", "t")] + a)
    assert after[1:] == before
    assert before == ["docs__b.md::0", "docs__b.md::1"]


# --- real corpus: the identifiers that failed in semantic-only search ---------

def test_keyword_half_finds_new_specs_on_the_real_corpus():
    from chunking import build_corpus

    chunks = build_corpus()
    idx = BM25Index([f"{c.source}\n{c.heading}\n{c.text}" for c in chunks])

    def top_sources(q, n=5):
        return [chunks[h.doc_index].source for h in idx.search(q, n)]

    assert any("outstanding-issues" in s for s in top_sources("Q-023 stub interest"))
    assert any("funding-window" in s or "HANDOFF-04" in s for s in top_sources("UNDER_REVIEW tranche"))


# --- query-aware weighting ------------------------------------------------------

@pytest.mark.parametrize("query", ["UNDER_REVIEW", "Q-023", "PROVISIONAL(Q-D5)", "ac9f152fa24e merge", "T0 total"])
def test_identifier_queries_are_detected(query):
    from hybrid import has_identifier
    assert has_identifier(query)


@pytest.mark.parametrize("query", ["who holds the money", "84 working days", "16% flat rate", "tranche states and transitions"])
def test_plain_language_queries_are_not_identifier_queries(query):
    from hybrid import has_identifier
    assert not has_identifier(query)


def test_damped_semantic_weight_lets_keyword_winner_come_first():
    semantic = [sem("noise.md", "N", "n", 1)]
    keyword = [kw("exact.md", "E", "e", 1)]
    # Equal weights: a rank-1 tie, broken by the semantic rank.
    assert fuse(semantic, keyword, 2)[0].source == "noise.md"
    # Identifier query: semantic damped, the exact keyword match wins.
    assert fuse(semantic, keyword, 2, semantic_weight=0.4)[0].source == "exact.md"


def test_semantic_weight_by_query_type():
    from hybrid import LOOKUP_SEMANTIC_WEIGHT, MIXED_SEMANTIC_WEIGHT, semantic_weight_for
    assert semantic_weight_for("who holds the money") == 1.0
    assert semantic_weight_for("84 working days") == 1.0
    assert semantic_weight_for("Q-023") == LOOKUP_SEMANTIC_WEIGHT
    assert semantic_weight_for("UNDER_REVIEW Q-023") == LOOKUP_SEMANTIC_WEIGHT
    assert semantic_weight_for("stub interest Q-023") == MIXED_SEMANTIC_WEIGHT
