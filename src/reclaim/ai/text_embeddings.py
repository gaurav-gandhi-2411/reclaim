from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require

# Feature 1b Stage 2 (spec §2): sentence embeddings for the RESIDUAL only — clusters
# MinHash/LSH (minhash_lsh.py) couldn't cleanly resolve. `all-MiniLM-L6-v2` (Apache-2.0, tiny,
# CPU-fast) per spec, matching spec §0.6: raw cosine similarity is the reported number, never
# manufactured into a probability. Mirrors phash.py -> image_similarity.py's two-stage role,
# just for the text pipeline's second stage instead of a whole-set prefilter.

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Lazily constructed once per process — loading the model is the expensive part (disk read +
# weight materialization), computing an embedding from an already-loaded model is fast. Not
# thread-safe by construction (module-level mutable cache); this codebase's AI-layer code has
# no concurrent-access requirement today (see ADR-0011 — "no UI wiring" posture).
_model_cache: object | None = None


def _model() -> object:
    global _model_cache
    if _model_cache is None:
        sentence_transformers = require(
            "sentence_transformers", feature="sentence-embedding residual resolution"
        )
        _model_cache = sentence_transformers.SentenceTransformer(_MODEL_NAME)
    return _model_cache


@dataclass(frozen=True, slots=True)
class DocumentEmbedding:
    path: Path
    vector: tuple[float, ...]  # plain tuple, trivially serializable — same reasoning as
    # DocumentMinHash.minhash_values and phash.ImageHashRecord's hex strings.


def compute_document_embedding(path: Path, text: str) -> DocumentEmbedding | None:
    """Returns `None` (not an error) for empty/whitespace-only text — nothing meaningful to
    embed, same "skip, don't abort" posture as every other AI-layer compute function."""
    if not text.strip():
        return None
    model = _model()
    vector = model.encode(text, convert_to_numpy=True, show_progress_bar=False)  # type: ignore[attr-defined]
    return DocumentEmbedding(path=path, vector=tuple(float(v) for v in vector))


def cosine_similarity(embedding_a: DocumentEmbedding, embedding_b: DocumentEmbedding) -> float:
    """Raw cosine similarity (-1.0 to 1.0, higher = more similar) — never calibrated or
    presented as a probability (spec §0.6)."""
    numpy = require("numpy", feature="cosine similarity computation")
    vector_a = numpy.asarray(embedding_a.vector)
    vector_b = numpy.asarray(embedding_b.vector)
    norm_a = numpy.linalg.norm(vector_a)
    norm_b = numpy.linalg.norm(vector_b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(numpy.dot(vector_a, vector_b) / (norm_a * norm_b))
