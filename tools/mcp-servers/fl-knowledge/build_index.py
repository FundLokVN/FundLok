"""
One-time (and re-run-able) indexer for fl-knowledge's Pinecone backend.

What it does:
1. Walks the same docs (CLAUDE.md, AGENTS.md, README.md, docs/**/*.md) as
   before, split into chunks by chunking.py.
2. Embeds each chunk locally with sentence-transformers (no API cost, no
   network dependency for the embedding step itself).
3. Upserts the vectors into a Pinecone serverless index, with the chunk's
   source file, heading, and text stored as metadata so search results are
   readable without a second lookup.

Run this:
    - once, before first use, to populate the index
    - again any time docs/specs/ADRs change materially (it's idempotent --
      chunk IDs are deterministic, so re-running updates existing vectors in
      place rather than duplicating them)

Requires PINECONE_API_KEY to be set -- see .env.example in this folder.
"""

import os
import sys

from chunking import assign_ids, build_corpus
from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec
from sentence_transformers import SentenceTransformer

load_dotenv()

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = 384  # dimension of all-MiniLM-L6-v2 -- update if you change the model
INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "fl-knowledge")
PINECONE_CLOUD = os.environ.get("PINECONE_CLOUD", "aws")
PINECONE_REGION = os.environ.get("PINECONE_REGION", "us-east-1")

# Pinecone metadata has a size limit per record; keep stored text well under it.
MAX_METADATA_TEXT_CHARS = 2000

BATCH_SIZE = 64


def get_pinecone_client() -> Pinecone:
    api_key = os.environ.get("PINECONE_API_KEY")
    if not api_key:
        sys.exit(
            "PINECONE_API_KEY is not set. Copy .env.example to .env in this "
            "folder and paste your key in, or export it in your shell."
        )
    return Pinecone(api_key=api_key)


def ensure_index(pc: Pinecone):
    existing = {i["name"] for i in pc.list_indexes()}
    if INDEX_NAME not in existing:
        print(f"[build_index] Creating Pinecone index '{INDEX_NAME}' "
              f"(dim={EMBEDDING_DIM}, cloud={PINECONE_CLOUD}, region={PINECONE_REGION})...")
        pc.create_index(
            name=INDEX_NAME,
            dimension=EMBEDDING_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud=PINECONE_CLOUD, region=PINECONE_REGION),
        )
    return pc.Index(INDEX_NAME)


def delete_stale_vectors(index, current_ids: set[str]) -> int:
    """Delete every vector whose ID is not in this run's corpus (removed or
    renamed docs, and the leftovers of the old corpus-wide ID scheme). Runs
    after the upsert, so a failure part-way leaves extra vectors, never a
    missing index."""
    stale: list[str] = []
    for id_page in index.list():
        # Older Pinecone SDKs yield bare ID strings, newer ones ListItem objects.
        page_ids = [getattr(item, "id", item) for item in id_page]
        stale.extend(i for i in page_ids if i not in current_ids)
    for start in range(0, len(stale), 1000):
        index.delete(ids=stale[start:start + 1000])
    print(f"[build_index] Deleted {len(stale)} stale vectors.")
    return len(stale)


def main():
    chunks = build_corpus()
    if not chunks:
        print("[build_index] No chunks found -- check chunking.INDEXED_PATHS.")
        return

    print(f"[build_index] {len(chunks)} chunks found. Loading embedding model "
          f"'{EMBEDDING_MODEL}' (first run downloads it, ~90MB)...")
    model = SentenceTransformer(EMBEDDING_MODEL)

    pc = get_pinecone_client()
    index = ensure_index(pc)

    print("[build_index] Embedding and upserting...")
    texts = [f"{c.heading}\n{c.text}" for c in chunks]
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)

    vectors = []
    for chunk_vector_id, chunk, vec in zip(assign_ids(chunks), chunks, embeddings):
        vectors.append({
            "id": chunk_vector_id,
            "values": vec.tolist(),
            "metadata": {
                "source": chunk.source,
                "heading": chunk.heading,
                "text": chunk.text[:MAX_METADATA_TEXT_CHARS],
            },
        })

    for start in range(0, len(vectors), BATCH_SIZE):
        batch = vectors[start:start + BATCH_SIZE]
        index.upsert(vectors=batch)
        print(f"[build_index] Upserted {start + len(batch)}/{len(vectors)}")

    delete_stale_vectors(index, {v["id"] for v in vectors})

    print(f"[build_index] Done. Index '{INDEX_NAME}' now has {len(vectors)} vectors "
          f"(stats: {index.describe_index_stats()}).")


if __name__ == "__main__":
    main()
