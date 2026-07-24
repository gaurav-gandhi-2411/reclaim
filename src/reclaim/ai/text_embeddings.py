from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require

# Feature 1b Stage 2 (spec §2): sentence embeddings for the RESIDUAL only — clusters
# MinHash/LSH (minhash_lsh.py) couldn't cleanly resolve. `all-MiniLM-L6-v2` (Apache-2.0, tiny,
# CPU-fast) per spec, matching spec §0.6: raw cosine similarity is the reported number, never
# manufactured into a probability. Mirrors phash.py -> image_similarity.py's two-stage role,
# just for the text pipeline's second stage instead of a whole-set prefilter.

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Supply-chain integrity (audit findings E17/E18, ADR-0028): pinned to an explicit commit hash
# so a future HF Hub `main` repoint can never silently change what gets downloaded -- the
# revision below is what `main` resolves to today, determined via
# `HfApi().model_info(_MODEL_NAME).sha` (network-resolved) and cross-checked against this
# machine's local HF Hub cache `refs/main` file (both agreed). `SentenceTransformer` itself
# accepts `revision=` directly (unlike open_clip's tag-based loading -- see
# image_embeddings.py), so no bypass is needed here.
_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"  # pinned; see ADR-0028
_MODEL_WEIGHTS_FILENAME = "model.safetensors"
_MODEL_WEIGHTS_SHA256 = "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db"

# Lazily constructed once per process — loading the model is the expensive part (disk read +
# weight materialization), computing an embedding from an already-loaded model is fast. Not
# thread-safe by construction (module-level mutable cache); this codebase's AI-layer code has
# no concurrent-access requirement today (see ADR-0011 — "no UI wiring" posture).
_model_cache: object | None = None


def _verify_pinned_weights_or_quarantine() -> None:
    """Downloads (or reuses the local HF Hub cache for) the pinned MiniLM weights file and
    verifies its SHA-256 against the known-good digest above before `SentenceTransformer`
    loads it (E18). HF Hub's own download path is already atomic (tmp-file + rename,
    size-checked before the rename happens), so a genuinely partial/interrupted download is
    never left where it could be loaded as if complete; this adds an independent content-hash
    check on top, catching a case the size-only check inside `huggingface_hub` cannot: a
    server (or a compromised mirror/proxy) that serves the wrong but correctly-sized bytes for
    the pinned revision. A mismatch deletes the bad local blob (quarantine) rather than
    letting it be cached silently as if valid."""
    huggingface_hub = require("huggingface_hub", feature="model weight integrity verification")
    weights_path = Path(
        huggingface_hub.hf_hub_download(
            repo_id=_MODEL_NAME, filename=_MODEL_WEIGHTS_FILENAME, revision=_MODEL_REVISION
        )
    )
    digest = hashlib.sha256()
    with weights_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != _MODEL_WEIGHTS_SHA256:
        weights_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"sentence-transformers weight integrity check failed for {weights_path}: expected "
            f"sha256 {_MODEL_WEIGHTS_SHA256}, got {actual_sha256}. The corrupted/tampered file "
            "has been deleted; retry to re-download."
        )


def _model() -> object:
    global _model_cache
    if _model_cache is None:
        sentence_transformers = require(
            "sentence_transformers", feature="sentence-embedding residual resolution"
        )
        _verify_pinned_weights_or_quarantine()
        _model_cache = sentence_transformers.SentenceTransformer(
            _MODEL_NAME, revision=_MODEL_REVISION
        )
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
