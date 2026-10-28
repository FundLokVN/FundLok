"""
Shared chunking logic for fl-knowledge, used by both the one-time indexer
(build_index.py) and the MCP server (server.py) so they can never disagree
about what a "chunk" is.

Supports two source types:
- Markdown (.md): chunked on headings (see chunk_markdown).
- PDF (.pdf): chunked per-page, then split into overlapping character
  windows sized for the embedding model's context limit (see chunk_pdf).
  Text extraction is via pypdf -- it's a plain-text extractor, so PDFs that
  are scans/images with no text layer will yield nothing (OCR is out of
  scope here; convert those separately if needed). Extraction quality also
  depends on the PDF itself -- a browser "print to PDF" of a webpage, for
  example, may carry over navigation/menu text alongside the real content.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

# Repo root is four levels up from this file: tools/mcp-servers/fl-knowledge/chunking.py
REPO_ROOT = Path(__file__).resolve().parents[3]

# Where the knowledge base lives. Add paths here as new doc locations appear.
# Both .md and .pdf files under these paths are indexed.
INDEXED_PATHS = [
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs",
]

# Left out of the index (ADR-010 follow-up). The VPBank proposal tree alone was
# 48% of all chunks, mostly the same text several times over, and it describes
# an escrow model FundLok does not use (ADR-009), so on custody questions it
# crowded out the current docs.
#  - any directory named in EXCLUDED_DIR_NAMES, anywhere under the indexed paths
#  - the VPBank folders whose content is already inside the consolidated
#    documents (section sources are the parts they were built from; diagrams
#    and generators are build artefacts)
# Also skipped: a .pdf with a same-named .md next to it (see is_render_duplicate).
EXCLUDED_DIR_NAMES = {"_superseded"}
_VPBANK = REPO_ROOT / "docs" / "specs" / "banking" / "vpbank-proposal"
EXCLUDED_PATHS = [_VPBANK / "03-section-sources", _VPBANK / "04-diagrams", _VPBANK / "05-generators"]

# all-MiniLM-L6-v2 (the embedding model used in build_index.py/server.py) has
# a 256-token context window (~1000-1500 characters). PDF pages routinely
# exceed that, so long pages are split into overlapping windows -- the
# overlap keeps a sentence that straddles a window boundary from being cut
# with no surrounding context in either chunk.
PDF_WINDOW_CHARS = 1000
PDF_WINDOW_OVERLAP = 150
PDF_MIN_CHUNK_CHARS = 40  # drop near-empty windows (e.g. a page that's just a header/footer)


@dataclass
class Chunk:
    source: str      # file path relative to repo root
    heading: str      # nearest markdown heading, or "(top of file)"
    text: str         # chunk body


def chunk_markdown(path: Path) -> list[Chunk]:
    """Split a markdown file into chunks on '#'-prefixed headings (any level).
    Falls back to treating the whole file as one chunk if it has no headings."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    rel = str(path.relative_to(REPO_ROOT))
    lines = raw.splitlines()

    chunks: list[Chunk] = []
    current_heading = "(top of file)"
    current_lines: list[str] = []

    def flush():
        body = "\n".join(current_lines).strip()
        if body:
            chunks.append(Chunk(source=rel, heading=current_heading, text=body))

    for line in lines:
        if line.startswith("#"):
            flush()
            current_heading = line.lstrip("#").strip() or current_heading
            current_lines = [line]
        else:
            current_lines.append(line)
    flush()

    return chunks or [Chunk(source=rel, heading="(whole file)", text=raw)]


def _windows(text: str, size: int, overlap: int) -> list[str]:
    """Split text into overlapping fixed-size windows on whitespace-normalized text."""
    if len(text) <= size:
        return [text] if text.strip() else []
    windows = []
    step = size - overlap
    for start in range(0, len(text), step):
        window = text[start:start + size].strip()
        if window:
            windows.append(window)
        if start + size >= len(text):
            break
    return windows


def chunk_pdf(path: Path) -> list[Chunk]:
    """Extract text per page with pypdf, then split each page into
    overlapping character windows sized for the embedding model's context
    limit. One Chunk per window; heading identifies the page (and window,
    if a page needed more than one)."""
    rel = str(path.relative_to(REPO_ROOT))
    try:
        reader = PdfReader(str(path))
    except Exception:
        return []

    chunks: list[Chunk] = []
    for page_num, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        normalized = re.sub(r"\s+", " ", raw).strip()
        if len(normalized) < PDF_MIN_CHUNK_CHARS:
            continue
        windows = _windows(normalized, PDF_WINDOW_CHARS, PDF_WINDOW_OVERLAP)
        for i, window in enumerate(windows):
            heading = f"Page {page_num}" if len(windows) == 1 else f"Page {page_num} (part {i + 1})"
            chunks.append(Chunk(source=rel, heading=heading, text=window))
    return chunks


def is_excluded(path: Path) -> bool:
    """True for files under an excluded directory name or excluded path."""
    if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
        return True
    return any(path.is_relative_to(excluded) for excluded in EXCLUDED_PATHS)


def is_render_duplicate(path: Path) -> bool:
    """A .pdf sitting next to a same-named .md is a rendering of that file;
    indexing both doubles every chunk."""
    return path.suffix.lower() == ".pdf" and path.with_suffix(".md").exists()


def build_corpus() -> list[Chunk]:
    md_files: list[Path] = []
    pdf_files: list[Path] = []
    for p in INDEXED_PATHS:
        if p.is_dir():
            md_files.extend(f for f in sorted(p.rglob("*.md")) if not is_excluded(f))
            pdf_files.extend(
                f for f in sorted(p.rglob("*.pdf"))
                if not is_excluded(f) and not is_render_duplicate(f)
            )
        elif p.is_file():
            (pdf_files if p.suffix.lower() == ".pdf" else md_files).append(p)

    corpus: list[Chunk] = []
    for f in md_files:
        corpus.extend(chunk_markdown(f))
    for f in pdf_files:
        corpus.extend(chunk_pdf(f))
    return corpus


def chunk_id(chunk: Chunk, index: int) -> str:
    """Deterministic Pinecone vector ID: stable across re-indexing runs as
    long as file paths and chunk order don't change, so re-running
    build_index.py updates existing vectors in place instead of duplicating."""
    safe_source = chunk.source.replace("/", "__")
    return f"{safe_source}::{index}"


def assign_ids(chunks: list[Chunk]) -> list[str]:
    """Vector IDs numbered per source file (`<source>::<n>`), not across the
    whole corpus. With corpus-wide numbering, adding one document early in the
    walk shifted every later ID, so each reindex left the old vectors behind
    as stale duplicates (the index held 4,880 vectors for 2,341 chunks). A
    chunk's ID now changes only when its own file's chunk order changes."""
    seen: dict[str, int] = {}
    ids: list[str] = []
    for chunk in chunks:
        n = seen.get(chunk.source, 0)
        seen[chunk.source] = n + 1
        ids.append(chunk_id(chunk, n))
    return ids
