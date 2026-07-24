from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw

from reclaim.ai import image_embeddings as image_embeddings_module
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


def test_compute_image_embedding_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    not_an_image = tmp_path / "fake.png"
    not_an_image.write_bytes(b"not image data")
    assert compute_image_embedding(not_an_image) is None


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


# E17/E18 (audit findings, ADR-0026): pinned-revision checkpoint download + sha256 integrity
# verification for the CLIP checkpoint. Real download/inference is exercised by the other
# tests in this file when the `ai` extra + network are available; these tests instead mock
# `require()`'s return value so the pinning/verification wiring is provable without a real
# download, per the task's own guidance.


@pytest.fixture
def _reset_model_cache() -> object:
    """Module-level `_model_cache`/`_preprocess_cache` are a process-wide lazy singleton --
    save/restore around tests that need `_model_and_preprocess()` to actually re-run its
    loading logic, so this doesn't leak a mocked model into (or lose a real model already
    cached by) other tests in this file."""
    original_model = image_embeddings_module._model_cache
    original_preprocess = image_embeddings_module._preprocess_cache
    image_embeddings_module._model_cache = None
    image_embeddings_module._preprocess_cache = None
    yield object()
    image_embeddings_module._model_cache = original_model
    image_embeddings_module._preprocess_cache = original_preprocess


def test_model_and_preprocess_downloads_pinned_revision_and_verifies_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_model_cache: object
) -> None:
    fake_checkpoint = tmp_path / "checkpoint.safetensors"
    fake_checkpoint.write_bytes(b"fake-clip-weights")
    monkeypatch.setattr(
        image_embeddings_module,
        "_OPEN_CLIP_HF_WEIGHTS_SHA256",
        hashlib.sha256(b"fake-clip-weights").hexdigest(),
    )

    hf_hub_download_calls: list[dict[str, object]] = []

    class _FakeHfHub:
        @staticmethod
        def hf_hub_download(**kwargs: object) -> str:
            hf_hub_download_calls.append(kwargs)
            return str(fake_checkpoint)

    create_model_calls: list[dict[str, object]] = []

    class _FakeModel:
        def eval(self) -> None:
            pass

    class _FakeOpenClip:
        @staticmethod
        def get_pretrained_cfg(model_name: str, tag: str) -> dict[str, object]:
            assert model_name == image_embeddings_module._OPEN_CLIP_MODEL_NAME
            assert tag == "openai"
            return {
                "quick_gelu": True,
                "mean": (0.48145466, 0.4578275, 0.40821073),
                "std": (0.26862954, 0.26130258, 0.27577711),
                "interpolation": "bicubic",
                "resize_mode": "shortest",
            }

        @staticmethod
        def create_model_and_transforms(*args: object, **kwargs: object) -> tuple[object, ...]:
            create_model_calls.append(kwargs)
            return _FakeModel(), object(), object()

    def _fake_require(module_name: str, *, feature: str) -> object:
        if module_name == "huggingface_hub":
            return _FakeHfHub()
        if module_name == "open_clip":
            return _FakeOpenClip()
        raise AssertionError(f"unexpected require() call: {module_name}")

    monkeypatch.setattr(image_embeddings_module, "require", _fake_require)

    model, _ = image_embeddings_module._model_and_preprocess()

    assert isinstance(model, _FakeModel)
    assert hf_hub_download_calls == [
        {
            "repo_id": image_embeddings_module._OPEN_CLIP_HF_REPO,
            "filename": image_embeddings_module._OPEN_CLIP_HF_WEIGHTS_FILENAME,
            "revision": image_embeddings_module._OPEN_CLIP_HF_REVISION,
        }
    ]
    assert len(create_model_calls) == 1
    assert create_model_calls[0]["pretrained"] == str(fake_checkpoint)
    assert create_model_calls[0]["force_quick_gelu"] is True
    assert create_model_calls[0]["image_mean"] == (0.48145466, 0.4578275, 0.40821073)


def test_pinned_checkpoint_sha256_mismatch_quarantines_the_bad_file_and_raises(
    tmp_path: Path,
) -> None:
    bad_checkpoint = tmp_path / "bad.safetensors"
    bad_checkpoint.write_bytes(b"tampered content")

    with pytest.raises(RuntimeError, match="integrity check failed"):
        image_embeddings_module._verify_checkpoint_sha256_or_quarantine(bad_checkpoint, "0" * 64)

    assert not bad_checkpoint.exists()  # quarantined, never left for a caller to load


def test_pinned_checkpoint_sha256_match_leaves_the_file_in_place(tmp_path: Path) -> None:
    good_checkpoint = tmp_path / "good.safetensors"
    good_checkpoint.write_bytes(b"real content")
    expected = hashlib.sha256(b"real content").hexdigest()

    image_embeddings_module._verify_checkpoint_sha256_or_quarantine(good_checkpoint, expected)

    assert good_checkpoint.exists()
