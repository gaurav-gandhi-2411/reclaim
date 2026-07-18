from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

# Generates the REALISTIC document near-dup distribution Feature 1b targets — ordinary
# consumer/prosumer document accumulation (whitespace/formatting cleanup, paragraph-level
# revisions, copy-paste-into-another-app flattening) — applied to REAL public-domain text
# (Project Gutenberg, via fetch_gutenberg_texts.py), not fabricated sentences. Mirrors
# build_realistic_recompression_tiers.py's role for Feature 1a exactly: real content,
# deterministic named transform profiles, disclosed as covering only those profiles (ADR-0017).
#
# Every transform is DETERMINISTIC given a chunk's own text and, where randomness is used
# (paragraph shuffling), a seeded RNG — reproducible without needing to commit any generated
# text files.

_SEED = 42
_MIN_CHUNK_WORDS = 300
_CHUNKS_PER_BOOK = 15

_MILD = "mild"
_MODERATE = "moderate"
_COLLAB_PASTE = "collab_paste"


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    chunk_id: str
    text: str


@dataclass(frozen=True, slots=True)
class RealisticDocumentVariant:
    chunk_id: str
    tier: str
    profile: str
    text: str


def chunk_book(text: str, book_id: str, *, n_chunks: int = _CHUNKS_PER_BOOK) -> list[DocumentChunk]:
    """Splits `text` into paragraph-aligned chunks of at least `_MIN_CHUNK_WORDS` words each —
    real book text, not synthetic sentences, chunked to resemble realistic document lengths
    (a report/memo/essay, not a whole novel)."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[DocumentChunk] = []
    current: list[str] = []
    current_words = 0
    for paragraph in paragraphs:
        current.append(paragraph)
        current_words += len(paragraph.split())
        if current_words >= _MIN_CHUNK_WORDS:
            chunks.append(DocumentChunk(f"{book_id}_{len(chunks):03d}", "\n\n".join(current)))
            current = []
            current_words = 0
        if len(chunks) >= n_chunks:
            break
    return chunks


def _mild_whitespace_cleanup(text: str) -> str:
    """The lightest touch: whitespace/formatting normalization only, content unchanged — the
    "re-saved through a different text editor" case."""
    normalized = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def _moderate_paragraph_trim_and_reorder(text: str, rng: random.Random) -> str:
    """A meaningfully revised copy: drop the last paragraph (trimmed), swap the order of two
    others — the "restructured but still recognizably the same document" case."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) > 1:
        paragraphs = paragraphs[:-1]
    if len(paragraphs) > 2:
        i, j = rng.sample(range(len(paragraphs)), 2)
        paragraphs[i], paragraphs[j] = paragraphs[j], paragraphs[i]
    return "\n\n".join(paragraphs)


def _collab_tool_paste(text: str) -> str:
    """Simulates the single most common real-world lossy re-transmission of document text:
    copy-pasting into a chat/email/collab-tool strips paragraph structure (everything
    collapses to a flat run of whitespace-separated text) and often truncates to whatever was
    selected — modeled here as flattening plus a 70% truncation of the flattened word stream."""
    flattened = re.sub(r"\s+", " ", text).strip()
    words = flattened.split()
    truncate_at = max(1, int(len(words) * 0.7))
    return " ".join(words[:truncate_at])


_PROFILES: tuple[tuple[str, str], ...] = (
    ("mild_whitespace_cleanup", _MILD),
    ("moderate_paragraph_trim_and_reorder", _MODERATE),
    ("collab_tool_paste", _COLLAB_PASTE),
)


def build_realistic_document_variants(
    chunks: list[DocumentChunk],
) -> list[RealisticDocumentVariant]:
    """One variant per (chunk, profile) — 3 variants per base chunk, deterministic given
    `_SEED` (only the moderate profile's paragraph swap uses randomness)."""
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic synthetic transform, not security
    variants: list[RealisticDocumentVariant] = []
    for chunk in chunks:
        for profile, tier in _PROFILES:
            if profile == "mild_whitespace_cleanup":
                text = _mild_whitespace_cleanup(chunk.text)
            elif profile == "moderate_paragraph_trim_and_reorder":
                text = _moderate_paragraph_trim_and_reorder(chunk.text, rng)
            elif profile == "collab_tool_paste":
                text = _collab_tool_paste(chunk.text)
            else:  # pragma: no cover - exhaustive over _PROFILES above
                raise AssertionError(f"unhandled profile {profile}")
            variants.append(
                RealisticDocumentVariant(
                    chunk_id=chunk.chunk_id, tier=tier, profile=profile, text=text
                )
            )
    return variants


def build_all_chunks(
    cleaned_texts_dir: Path, *, chunks_per_book: int = _CHUNKS_PER_BOOK
) -> list[DocumentChunk]:
    all_chunks: list[DocumentChunk] = []
    for text_path in sorted(cleaned_texts_dir.glob("*.txt")):
        book_id = text_path.stem
        text = text_path.read_text(encoding="utf-8")
        all_chunks.extend(chunk_book(text, book_id, n_chunks=chunks_per_book))
    return all_chunks
