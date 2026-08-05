from __future__ import annotations

import hashlib
import io
import struct
import zlib
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw

from reclaim.ai import image_embeddings as image_embeddings_module
from reclaim.ai._optional import AIModelMissingError
from reclaim.ai.image_embeddings import (
    ImageEmbeddingCache,
    compute_embeddings_batch,
    compute_image_embedding,
    cosine_similarity,
)


def _make_image(
    path: Path, *, color: tuple[int, int, int], shape_color: tuple[int, int, int]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (224, 224), color=color)
    draw = ImageDraw.Draw(img)
    draw.ellipse([40, 40, 180, 180], fill=shape_color)
    img.save(path, format="PNG")


def _make_decompression_bomb_png(path: Path, *, declared_size: tuple[int, int]) -> None:
    """D15 fixture (see `tests/test_ai_phash.py`'s twin of this helper for the full mechanism
    explanation): a real, tiny (4x4-pixel) PNG whose IHDR chunk is patched post-save to CLAIM
    `declared_size`, tripping Pillow's decompression-bomb check inside `Image.open()` without a
    genuinely huge real image on disk. Duplicated here rather than imported — this test module
    and `test_ai_phash.py` deliberately don't share a cross-test-module dependency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), color=(1, 2, 3)).save(stream, format="PNG")
    buf = bytearray(stream.getvalue())

    width, height = declared_size
    buf[16:20] = struct.pack(">I", width)
    buf[20:24] = struct.pack(">I", height)
    buf[29:33] = struct.pack(">I", zlib.crc32(bytes(buf[12:29])) & 0xFFFFFFFF)

    path.write_bytes(bytes(buf))


def test_compute_image_embedding_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    not_an_image = tmp_path / "fake.png"
    not_an_image.write_bytes(b"not image data")
    assert compute_image_embedding(not_an_image) is None


def test_compute_image_embedding_returns_none_for_declared_dimensions_past_bomb_cap(
    tmp_path: Path,
) -> None:
    """D15 regression: same decompression-bomb cap as `phash.compute_image_hashes` (both call
    sites share the single `require("PIL.Image", ...)` cap set in `reclaim.ai._optional`) --
    `compute_image_embedding`'s own broad `except Exception: return None` around `Image.open()`
    must catch the resulting `DecompressionBombError` the same way it already catches any other
    unreadable/corrupt image."""
    bomb = tmp_path / "bomb.png"
    _make_decompression_bomb_png(bomb, declared_size=(20000, 20000))

    assert compute_image_embedding(bomb) is None


def test_compute_image_embedding_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert compute_image_embedding(tmp_path / "gone.png") is None


def test_compute_image_embedding_returns_a_real_vector(tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    _make_image(path, color=(100, 150, 200), shape_color=(255, 100, 50))
    embedding = compute_image_embedding(path)
    assert embedding is not None
    assert len(embedding.vector) > 0
    assert embedding.path == path


def test_similar_images_score_higher_cosine_similarity_than_different_ones(
    tmp_path: Path,
) -> None:
    beach1 = tmp_path / "beach1.png"
    beach2 = tmp_path / "beach2.png"
    forest = tmp_path / "forest.png"
    _make_image(beach1, color=(135, 206, 235), shape_color=(255, 220, 100))
    _make_image(beach2, color=(130, 200, 230), shape_color=(250, 215, 105))
    _make_image(forest, color=(20, 60, 20), shape_color=(80, 40, 10))

    emb_beach1 = compute_image_embedding(beach1)
    emb_beach2 = compute_image_embedding(beach2)
    emb_forest = compute_image_embedding(forest)
    assert emb_beach1 is not None
    assert emb_beach2 is not None
    assert emb_forest is not None

    beach_similarity = cosine_similarity(emb_beach1, emb_beach2)
    cross_similarity = cosine_similarity(emb_beach1, emb_forest)
    assert beach_similarity > cross_similarity


def test_cosine_similarity_self_is_approximately_one(tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    _make_image(path, color=(100, 150, 200), shape_color=(255, 100, 50))
    embedding = compute_image_embedding(path)
    assert embedding is not None
    assert cosine_similarity(embedding, embedding) > 0.999


def test_embedding_cache_round_trips_identical_vector(tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    _make_image(path, color=(100, 150, 200), shape_color=(255, 100, 50))
    db_path = tmp_path / "cache.sqlite3"

    with ImageEmbeddingCache(db_path) as cache:
        first = compute_image_embedding(path, cache=cache)
        second = compute_image_embedding(path, cache=cache)
    assert first is not None
    assert second is not None
    assert first.vector == second.vector


def test_embedding_cache_miss_on_size_change(tmp_path: Path) -> None:
    """A cache keyed (path, size, mtime, model_id) must MISS (not silently reuse a stale
    embedding) when the file's size changes -- proven directly against the cache API, not
    just inferred from `compute_image_embedding`'s behavior."""
    path = tmp_path / "photo.png"
    _make_image(path, color=(100, 150, 200), shape_color=(255, 100, 50))
    db_path = tmp_path / "cache.sqlite3"

    with ImageEmbeddingCache(db_path) as cache:
        assert cache.get(path, size_bytes=1000, mtime=123.0) is None
        from reclaim.ai.image_embeddings import ImageEmbedding

        cache.put(ImageEmbedding(path=path, vector=(1.0, 2.0)), size_bytes=1000, mtime=123.0)
        assert cache.get(path, size_bytes=1000, mtime=123.0) is not None
        assert cache.get(path, size_bytes=2000, mtime=123.0) is None  # different size -> miss
        assert cache.get(path, size_bytes=1000, mtime=456.0) is None  # different mtime -> miss


def test_embedding_cache_miss_on_different_model_id(tmp_path: Path) -> None:
    from reclaim.ai.image_embeddings import ImageEmbedding

    path = tmp_path / "photo.png"
    db_path = tmp_path / "cache.sqlite3"
    with ImageEmbeddingCache(db_path) as cache:
        cache.put(
            ImageEmbedding(path=path, vector=(1.0,)),
            size_bytes=100,
            mtime=1.0,
            model_id="model_a",
        )
        assert cache.get(path, size_bytes=100, mtime=1.0, model_id="model_a") is not None
        assert cache.get(path, size_bytes=100, mtime=1.0, model_id="model_b") is None


def test_compute_embeddings_batch_skips_unreadable_files(tmp_path: Path) -> None:
    good = tmp_path / "good.png"
    bad = tmp_path / "bad.png"
    _make_image(good, color=(100, 150, 200), shape_color=(255, 100, 50))
    bad.write_bytes(b"not an image")

    embeddings = compute_embeddings_batch([good, bad])
    assert len(embeddings) == 1
    assert embeddings[0].path == good


# Wave 1 P0-B (2026-07-30): CLIP now loads a bundled, pinned, SHA256-verified ONNX file
# (`reclaim/ai/models/clip_vision_fp16.onnx`) via ONNX Runtime instead of downloading a torch
# checkpoint from HF Hub at first use. These tests cover the new loading/integrity-check
# wiring; real inference against the bundled file is exercised by the tests above whenever the
# `ai` extra is installed (same "mock the wiring, exercise the real thing separately" split the
# old torch-based tests used).


@pytest.fixture
def _reset_session_cache() -> object:
    """Module-level `_session_cache` is a process-wide lazy singleton -- save/restore around
    tests that need `_clip_session()` to actually re-run its loading logic, so this doesn't
    leak a mocked session into (or lose a real one already cached by) other tests in this
    file."""
    original = image_embeddings_module._session_cache
    image_embeddings_module._session_cache = None
    yield object()
    image_embeddings_module._session_cache = original


def test_clip_session_loads_the_bundled_model_and_verifies_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_session_cache: object
) -> None:
    fake_model = tmp_path / "clip_vision_fp16.onnx"
    fake_model.write_bytes(b"fake-onnx-bytes")
    monkeypatch.setattr(image_embeddings_module, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(
        image_embeddings_module,
        "_CLIP_ONNX_SHA256",
        hashlib.sha256(b"fake-onnx-bytes").hexdigest(),
    )

    session_calls: list[tuple[str, list[str]]] = []

    class _FakeSession:
        def __init__(self, path: str, providers: list[str]) -> None:
            session_calls.append((path, providers))

    class _FakeOnnxRuntime:
        InferenceSession = _FakeSession

    def _fake_require(module_name: str, *, feature: str) -> object:
        assert module_name == "onnxruntime"
        return _FakeOnnxRuntime()

    monkeypatch.setattr(image_embeddings_module, "require", _fake_require)

    session = image_embeddings_module._clip_session()

    assert isinstance(session, _FakeSession)
    assert session_calls == [(str(fake_model), ["CPUExecutionProvider"])]

    # Second call reuses the cached session -- no second require()/InferenceSession call.
    session_again = image_embeddings_module._clip_session()
    assert session_again is session
    assert len(session_calls) == 1


class _FakeOnnxRuntimeModule:
    """Stands in for a real `onnxruntime` import -- these tests exercise the bundled-model
    existence/integrity check, which must be provable independent of whether `onnxruntime`
    happens to be installed in whatever environment runs the test (core-only `scripts/
    verify.py` included)."""

    class InferenceSession:
        def __init__(self, path: str, providers: list[str]) -> None:
            self.path = path
            self.providers = providers


def test_clip_session_raises_actionable_error_when_bundled_model_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_session_cache: object
) -> None:
    monkeypatch.setattr(image_embeddings_module, "_MODELS_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(
        image_embeddings_module, "require", lambda module_name, *, feature: _FakeOnnxRuntimeModule()
    )

    with pytest.raises(AIModelMissingError, match="bundled AI model file"):
        image_embeddings_module._clip_session()


def test_clip_session_raises_on_sha256_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_session_cache: object
) -> None:
    tampered_model = tmp_path / "clip_vision_fp16.onnx"
    tampered_model.write_bytes(b"tampered-bytes")
    monkeypatch.setattr(image_embeddings_module, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(image_embeddings_module, "_CLIP_ONNX_SHA256", "0" * 64)
    monkeypatch.setattr(
        image_embeddings_module, "require", lambda module_name, *, feature: _FakeOnnxRuntimeModule()
    )

    with pytest.raises(AIModelMissingError, match="integrity verification"):
        image_embeddings_module._clip_session()
