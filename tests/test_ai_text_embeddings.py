from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reclaim.ai import text_embeddings as text_embeddings_module

# E17/E18 (audit findings, ADR-0028): pinned-revision model download + sha256 integrity
# verification for the sentence-transformers MiniLM checkpoint. `reclaim.ai.text_embeddings`
# imports lazily (`_optional.require`) so this module-level import succeeds even without the
# `ai` extra installed -- these tests mock `require()`'s return value so the pinning/
# verification wiring is provable without a real download, per the task's own guidance. Real
# embedding computation (`compute_document_embedding`) is exercised by
# `evals/test_ai_document_gold.py`/`evals/test_ai_paws_embedding_gold.py` when the `ai` extra
# and network are available.


@pytest.fixture
def _reset_model_cache() -> object:
    """Module-level `_model_cache` is a process-wide lazy singleton -- save/restore around
    tests that need `_model()` to actually re-run its loading logic, so this doesn't leak a
    mocked model into (or lose a real model already cached by) other tests in this process."""
    original_model = text_embeddings_module._model_cache
    text_embeddings_module._model_cache = None
    yield object()
    text_embeddings_module._model_cache = original_model


def test_model_downloads_pinned_revision_and_verifies_checksum_before_constructing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_model_cache: object
) -> None:
    fake_weights = tmp_path / "model.safetensors"
    fake_weights.write_bytes(b"fake-minilm-weights")
    monkeypatch.setattr(
        text_embeddings_module,
        "_MODEL_WEIGHTS_SHA256",
        hashlib.sha256(b"fake-minilm-weights").hexdigest(),
    )

    hf_hub_download_calls: list[dict[str, object]] = []

    class _FakeHfHub:
        @staticmethod
        def hf_hub_download(**kwargs: object) -> str:
            hf_hub_download_calls.append(kwargs)
            return str(fake_weights)

    sentence_transformer_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _FakeSentenceTransformer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            sentence_transformer_calls.append((args, kwargs))

    class _FakeSentenceTransformersModule:
        SentenceTransformer = _FakeSentenceTransformer

    def _fake_require(module_name: str, *, feature: str) -> object:
        if module_name == "huggingface_hub":
            return _FakeHfHub()
        if module_name == "sentence_transformers":
            return _FakeSentenceTransformersModule()
        raise AssertionError(f"unexpected require() call: {module_name}")

    monkeypatch.setattr(text_embeddings_module, "require", _fake_require)

    model = text_embeddings_module._model()

    assert isinstance(model, _FakeSentenceTransformer)
    assert hf_hub_download_calls == [
        {
            "repo_id": text_embeddings_module._MODEL_NAME,
            "filename": text_embeddings_module._MODEL_WEIGHTS_FILENAME,
            "revision": text_embeddings_module._MODEL_REVISION,
        }
    ]
    expected_call = (
        (text_embeddings_module._MODEL_NAME,),
        {"revision": text_embeddings_module._MODEL_REVISION},
    )
    assert sentence_transformer_calls == [expected_call]


def test_pinned_weights_sha256_mismatch_quarantines_the_bad_file_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad_weights = tmp_path / "model.safetensors"
    bad_weights.write_bytes(b"tampered content")

    class _FakeHfHub:
        @staticmethod
        def hf_hub_download(**kwargs: object) -> str:
            return str(bad_weights)

    def _fake_require(module_name: str, *, feature: str) -> object:
        assert module_name == "huggingface_hub"
        return _FakeHfHub()

    monkeypatch.setattr(text_embeddings_module, "require", _fake_require)

    with pytest.raises(RuntimeError, match="integrity check failed"):
        text_embeddings_module._verify_pinned_weights_or_quarantine()

    assert not bad_weights.exists()  # quarantined, never left for SentenceTransformer to load


def test_pinned_weights_sha256_match_leaves_the_file_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    good_weights = tmp_path / "model.safetensors"
    good_weights.write_bytes(b"real content")

    class _FakeHfHub:
        @staticmethod
        def hf_hub_download(**kwargs: object) -> str:
            return str(good_weights)

    def _fake_require(module_name: str, *, feature: str) -> object:
        assert module_name == "huggingface_hub"
        return _FakeHfHub()

    monkeypatch.setattr(text_embeddings_module, "require", _fake_require)
    monkeypatch.setattr(
        text_embeddings_module, "_MODEL_WEIGHTS_SHA256", hashlib.sha256(b"real content").hexdigest()
    )

    text_embeddings_module._verify_pinned_weights_or_quarantine()

    assert good_weights.exists()
