from __future__ import annotations

import hashlib
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

# Supply-chain integrity (audit findings E17/E18, ADR-0028): open_clip==3.3.0 (pinned in
# pyproject.toml/uv.lock) has NO `revision=` passthrough anywhere on the tag-based
# `pretrained="openai"` loading path -- `create_model` always calls
# `download_pretrained_from_hf(model_id, cache_dir=cache_dir)` with `revision=None`, i.e. HF
# Hub's MUTABLE "main" ref (verified directly against the installed package source, not
# assumed). The "openai" tag resolves (via `open_clip.get_pretrained_cfg`) to HF Hub repo
# "timm/vit_base_patch32_clip_224.openai", not the more obviously-named
# "openai/clip-vit-base-patch32". `_pinned_open_clip_checkpoint_path` below bypasses this
# entirely: it downloads the checkpoint itself, pinned to an explicit commit hash, verifies
# its SHA-256, and hands `create_model_and_transforms` a concrete local file path instead of
# the "openai" tag -- the ONLY call site that ever touches HF Hub for this checkpoint.
_OPEN_CLIP_HF_REPO = "timm/vit_base_patch32_clip_224.openai"
_OPEN_CLIP_HF_REVISION = "a6f597a30f7b82c51704746581f9a4e41421e878"  # pinned; see ADR-0028
_OPEN_CLIP_HF_WEIGHTS_FILENAME = "open_clip_model.safetensors"
_OPEN_CLIP_HF_WEIGHTS_SHA256 = "e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31"

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


def _verify_checkpoint_sha256_or_quarantine(checkpoint_path: Path, expected_sha256: str) -> None:
    """Hashes `checkpoint_path` and compares it to `expected_sha256`, raising `RuntimeError`
    (and deleting the file -- quarantine, never a silent reuse) on mismatch. HF Hub's own
    download path is already atomic (tmp-file + rename, size-checked before the rename ever
    happens -- a genuinely partial/interrupted download is never left where a caller could
    read it), so this is a deliberate, independent layer on top: it catches a case the
    size-only check inside `huggingface_hub` cannot, a server (or a compromised mirror/proxy)
    that serves the wrong but correctly-sized bytes for the pinned revision."""
    digest = hashlib.sha256()
    with checkpoint_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        checkpoint_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"CLIP checkpoint integrity check failed for {checkpoint_path}: expected sha256 "
            f"{expected_sha256}, got {actual_sha256}. The corrupted/tampered file has been "
            "deleted; retry to re-download."
        )


def _pinned_open_clip_checkpoint_path() -> str:
    """Downloads (or reuses the local HF Hub cache for) the exact pinned CLIP checkpoint
    revision and verifies its content hash (E18) before returning its path -- see the module
    docstring comment above `_OPEN_CLIP_HF_REPO` for why this bypasses open_clip's own
    `pretrained="openai"` tag resolution entirely."""
    huggingface_hub = require("huggingface_hub", feature="pinned CLIP checkpoint download")
    checkpoint_path = str(
        huggingface_hub.hf_hub_download(
            repo_id=_OPEN_CLIP_HF_REPO,
            filename=_OPEN_CLIP_HF_WEIGHTS_FILENAME,
            revision=_OPEN_CLIP_HF_REVISION,
        )
    )
    _verify_checkpoint_sha256_or_quarantine(Path(checkpoint_path), _OPEN_CLIP_HF_WEIGHTS_SHA256)
    return checkpoint_path


def _model_and_preprocess() -> tuple[object, object]:
    global _model_cache, _preprocess_cache
    if _model_cache is None:
        open_clip = require("open_clip", feature="CLIP semantic image embeddings")
        checkpoint_path = _pinned_open_clip_checkpoint_path()
        # The "openai" tag's mean/std/interpolation/resize_mode/quick_gelu -- read from
        # open_clip's own built-in config rather than duplicated as magic numbers here, so this
        # reproduces EXACTLY what `pretrained="openai"` would have configured, just sourcing the
        # weights file from the pinned/verified download above instead of the tag's own (mutable
        # "main"-resolving) lookup.
        tag_cfg = open_clip.get_pretrained_cfg(_OPEN_CLIP_MODEL_NAME, "openai")
        model, _, preprocess = open_clip.create_model_and_transforms(
            _OPEN_CLIP_MODEL_NAME,
            pretrained=checkpoint_path,
            force_quick_gelu=tag_cfg["quick_gelu"],
            image_mean=tag_cfg["mean"],
            image_std=tag_cfg["std"],
            image_interpolation=tag_cfg["interpolation"],
            image_resize_mode=tag_cfg["resize_mode"],
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
