from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require, require_bundled_model

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
#
# Wave 1 P0-B (2026-07-30): rewritten from torch/open_clip to ONNX Runtime — torch/
# open-clip-torch dropped entirely (see ADR-0029's successor decision; the "download a
# separate AI runtime" plan was superseded by "the model is small enough to bundle directly
# once quantized"). The CLIP vision encoder ships as a pinned, SHA256-verified fp16 ONNX file
# under `reclaim/ai/models/` (175.8MB) — fp16, not int8: a decision-grade BCubed comparison on
# real INRIA Copydays blocks measured int8 at a material -11pp precision regression at the
# shipped operating threshold, while fp16 was bit-identical BCubed behavior to the original
# torch-fp32 model (see reports/ai/onnx_quality_parity/). Quality-parity numbers, payload
# sizes, and the full decision rationale are documented there, not repeated here.

_PACKAGE_DIR = Path(__file__).parent
_MODELS_DIR = _PACKAGE_DIR / "models"

_CLIP_ONNX_FILENAME = "clip_vision_fp16.onnx"
_CLIP_ONNX_SHA256 = "6ea2e813b38fc77b70b2e4930bff6c144d420c42bcb77ca1eeeaa4b0d2a2db02"
# Bumped from the old `open_clip:ViT-B-32-quickgelu:openai` id (torch backend) — a different
# backend/precision is, by this cache's own stated contract ("model_id is part of the key so
# switching model checkpoints never silently mixes incompatible embedding spaces"), a
# different embedding space, even though the underlying weights are the same CLIP checkpoint
# just fp16-converted. Old cached torch embeddings simply become unreachable cache misses,
# never silently compared against new ONNX ones.
_EMBEDDING_MODEL_ID = f"onnx-fp16:clip_vision:{_CLIP_ONNX_SHA256[:12]}"

# CLIP ViT-B-32/openai's own preprocessing constants (read from open_clip's built-in
# "openai" tag config during Wave 1's conversion — see image_embeddings.py's git history
# pre-P0-B for the torch/torchvision pipeline this replicates): Resize(shortest side to 224,
# bicubic) -> CenterCrop(224) -> scale to [0,1] -> normalize(mean, std). Hardcoded here since
# open_clip (the only thing that used to expose this config) is no longer a dependency.
_CLIP_IMAGE_SIZE = 224
_CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
_CLIP_STD = (0.26862954, 0.26130258, 0.27577711)

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


_session_cache: object | None = None


def _clip_session() -> object:
    """Lazily constructs the ONNX Runtime inference session once per process — loading the
    session (reading + validating the 175.8MB model file) is the expensive part; running
    inference against an already-loaded session is fast. Never called at import time or at
    app startup — only the first time a caller actually needs a CLIP embedding (P1-A: lazy
    loading, cold start must never pay this cost)."""
    global _session_cache
    if _session_cache is None:
        onnxruntime = require("onnxruntime", feature="CLIP semantic image embeddings")
        model_path = require_bundled_model(
            _MODELS_DIR / _CLIP_ONNX_FILENAME,
            expected_sha256=_CLIP_ONNX_SHA256,
            feature="CLIP semantic image embeddings",
        )
        _session_cache = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
    return _session_cache


def _preprocess_image(image: object, pil_image_module: object, numpy: object) -> object:
    r"""Reimplements CLIP ViT-B-32/openai's preprocessing without torchvision (Wave 1 P0-B —
    torch/torchvision are no longer a dependency): resize the shortest side to 224 (bicubic),
    center-crop to 224x224, scale to [0,1], normalize by CLIP's mean/std, transpose to CHW,
    add a batch dimension.

    `reducing_gap=3.0` on the resize: Pillow's plain BICUBIC resize is not numerically
    identical to torchvision's antialiased bicubic resize (`antialias=True`) for a large
    downscale ratio (a real photo down to 224px) — `reducing_gap` makes Pillow do a
    two-step reduce-then-resize, which measurably improves the match (mean cosine similarity
    between the two pipelines' resulting embeddings on real Copydays images: 0.994 without
    `reducing_gap`, 0.996 with it — see reports/ai/onnx_quality_parity/ for the full,
    disclosed measurement, including its real, small, accepted impact on BCubed grouping
    quality: shipped-threshold F1 delta -0.008 for fp16, still comfortably above Track B's
    own 0.70 precision floor).
    """
    image = image.convert("RGB")  # type: ignore[attr-defined]
    width, height = image.size  # type: ignore[attr-defined]
    if width < height:
        new_width, new_height = _CLIP_IMAGE_SIZE, round(_CLIP_IMAGE_SIZE * height / width)
    else:
        new_width, new_height = round(_CLIP_IMAGE_SIZE * width / height), _CLIP_IMAGE_SIZE
    bicubic = pil_image_module.BICUBIC  # type: ignore[attr-defined]
    image = image.resize((new_width, new_height), bicubic, reducing_gap=3.0)  # type: ignore[attr-defined]
    left = (new_width - _CLIP_IMAGE_SIZE) // 2
    top = (new_height - _CLIP_IMAGE_SIZE) // 2
    image = image.crop((left, top, left + _CLIP_IMAGE_SIZE, top + _CLIP_IMAGE_SIZE))  # type: ignore[attr-defined]

    array = numpy.asarray(image, dtype=numpy.float32) / 255.0  # type: ignore[attr-defined]
    mean = numpy.array(_CLIP_MEAN, dtype=numpy.float32)  # type: ignore[attr-defined]
    std = numpy.array(_CLIP_STD, dtype=numpy.float32)  # type: ignore[attr-defined]
    array = (array - mean) / std
    array = array.transpose(2, 0, 1)  # HWC -> CHW
    return array[None, ...].astype(numpy.float32)  # type: ignore[attr-defined]


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

    pil_image_module = require("PIL.Image", feature="image loading")
    numpy = require("numpy", feature="CLIP inference")
    try:
        image = pil_image_module.open(path)
    except Exception:
        return None

    tensor = _preprocess_image(image, pil_image_module, numpy)
    session = _clip_session()
    output = session.run(None, {"pixel_values": tensor})[0]  # type: ignore[attr-defined]
    vector = tuple(float(v) for v in output[0])
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
