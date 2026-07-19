from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require

# Feature 1a Track B (spec §1, ADR-0022): CLIP semantic image embeddings — the
# whole-scene/subject similarity signal pHash (Track A) can't provide (pHash is a low-
# frequency luminance/gradient DCT, sensitive to near-identical PIXELS, not semantic
# content — two different photos of the same beach on the same day are pHash-distant but
# CLIP-close). Track B groups the RESIDUAL after Track A's near-identical clustering
# (image_similarity.py) already ran — this module only computes/caches embeddings and
# reports raw cosine similarity, never a manufactured probability (spec §0.6).
#
# SQLite embedding cache keyed (path, size, mtime, model_id) per GG's explicit instruction —
# computing a CLIP embedding is comparatively expensive (a real forward pass through a ViT),
# so a cache keyed on the exact file-identity signals that would invalidate a stale hash
# (size/mtime changed = the file changed = the old embedding may no longer be valid) avoids
# re-embedding a whole photo library on every run. `model_id` is part of the key so switching
# model checkpoints never silently mixes incompatible embedding spaces.

_OPEN_CLIP_MODEL_NAME = "ViT-B-32-quickgelu"  # NOT the bare "ViT-B-32" -- the "openai"
# checkpoint was trained with QuickGELU activation; open_clip's default "ViT-B-32" config
# uses standard GELU and warns "QuickGELU mismatch" if paired with the "openai" pretrained
# tag (caught during this ADR's build, not shipped with a silently-degraded embedding space —
# see ADR-0022).
_EMBEDDING_MODEL_ID = f"open_clip:{_OPEN_CLIP_MODEL_NAME}:openai"  # see ADR-0022

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS image_embeddings (
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    model_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (path, size_bytes, mtime, model_id)
)
"""


@dataclass(frozen=True, slots=True)
class ImageEmbedding:
    path: Path
    vector: tuple[float, ...]  # plain tuple, trivially serializable — same convention as
    # text_embeddings.DocumentEmbedding/phash.ImageHashRecord's hex strings.


class ImageEmbeddingCache:
    """SQLite-backed embedding cache keyed (path, size_bytes, mtime, model_id) — a cache hit
    requires ALL FOUR to match exactly; any change to the file (size or mtime) or the model
    in use produces a cache miss, never a stale/wrong embedding silently reused."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ImageEmbeddingCache:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(
        self, path: Path, *, size_bytes: int, mtime: float, model_id: str = _EMBEDDING_MODEL_ID
    ) -> ImageEmbedding | None:
        row = self._conn.execute(
            "SELECT vector FROM image_embeddings WHERE path = ? AND size_bytes = ? "
            "AND mtime = ? AND model_id = ?",
            (str(path), size_bytes, mtime, model_id),
        ).fetchone()
        if row is None:
            return None
        vector = tuple(float(v) for v in row[0].decode("utf-8").split(","))
        return ImageEmbedding(path=path, vector=vector)

    def put(
        self,
        embedding: ImageEmbedding,
        *,
        size_bytes: int,
        mtime: float,
        model_id: str = _EMBEDDING_MODEL_ID,
    ) -> None:
        serialized = ",".join(repr(v) for v in embedding.vector).encode("utf-8")
        self._conn.execute(
            "INSERT OR REPLACE INTO image_embeddings "
            "(path, size_bytes, mtime, model_id, vector) VALUES (?, ?, ?, ?, ?)",
            (str(embedding.path), size_bytes, mtime, model_id, serialized),
        )
        self._conn.commit()


_model_cache: object | None = None
_preprocess_cache: object | None = None


def _model_and_preprocess() -> tuple[object, object]:
    global _model_cache, _preprocess_cache
    if _model_cache is None:
        open_clip = require("open_clip", feature="CLIP semantic image embeddings")
        model, _, preprocess = open_clip.create_model_and_transforms(
            _OPEN_CLIP_MODEL_NAME, pretrained="openai"
        )
        model.eval()
        _model_cache = model
        _preprocess_cache = preprocess
    return _model_cache, _preprocess_cache


def compute_image_embedding(
    path: Path, *, cache: ImageEmbeddingCache | None = None
) -> ImageEmbedding | None:
    """Returns `None` (not an error) for a file that fails to decode as an image — same
    "skip, don't abort" posture as `phash.compute_image_hashes`/`keep_best.score_image_
    quality`. Checks `cache` first (if provided) before running a real forward pass."""
    if not path.exists():
        return None
    stat = path.stat()

    if cache is not None:
        cached = cache.get(path, size_bytes=stat.st_size, mtime=stat.st_mtime)
        if cached is not None:
            return cached

    pil_image = require("PIL.Image", feature="image loading")
    torch = require("torch", feature="CLIP inference")
    try:
        image = pil_image.open(path).convert("RGB")
    except Exception:
        return None

    model, preprocess = _model_and_preprocess()
    tensor = preprocess(image).unsqueeze(0)  # type: ignore[operator]
    with torch.no_grad():
        features = model.encode_image(tensor)  # type: ignore[attr-defined]
    vector = tuple(float(v) for v in features[0])
    embedding = ImageEmbedding(path=path, vector=vector)

    if cache is not None:
        cache.put(embedding, size_bytes=stat.st_size, mtime=stat.st_mtime)
    return embedding


def cosine_similarity(embedding_a: ImageEmbedding, embedding_b: ImageEmbedding) -> float:
    """Raw cosine similarity (-1.0 to 1.0, higher = more similar) — never calibrated or
    presented as a probability (spec §0.6). Same implementation shape as
    `text_embeddings.cosine_similarity`."""
    numpy = require("numpy", feature="cosine similarity computation")
    vector_a = numpy.asarray(embedding_a.vector)
    vector_b = numpy.asarray(embedding_b.vector)
    norm_a = numpy.linalg.norm(vector_a)
    norm_b = numpy.linalg.norm(vector_b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(numpy.dot(vector_a, vector_b) / (norm_a * norm_b))


def compute_embeddings_batch(
    paths: Sequence[Path], *, cache: ImageEmbeddingCache | None = None
) -> list[ImageEmbedding]:
    """Computes (or fetches from `cache`) an embedding for every path that decodes
    successfully; paths that fail to decode are silently skipped (same posture as the
    single-image function this batches)."""
    embeddings: list[ImageEmbedding] = []
    for path in paths:
        embedding = compute_image_embedding(path, cache=cache)
        if embedding is not None:
            embeddings.append(embedding)
    return embeddings
