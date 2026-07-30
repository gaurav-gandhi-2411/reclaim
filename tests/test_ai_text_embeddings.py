from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reclaim.ai import text_embeddings as text_embeddings_module
from reclaim.ai._optional import AIModelMissingError

# Wave 1 P0-B (2026-07-30): MiniLM now loads a bundled, pinned, SHA256-verified ONNX file
# (`reclaim/ai/models/minilm_int8.onnx`) + tokenizer (`minilm_tokenizer.json`) via ONNX
# Runtime + the `tokenizers` package instead of downloading a torch/sentence-transformers
# checkpoint from HF Hub at first use. These tests cover the new loading/integrity-check
# wiring; real embedding computation (`compute_document_embedding`) is exercised by
# `evals/test_ai_document_gold.py`/`evals/test_ai_paws_embedding_gold.py` when the `ai` extra
# is installed.


@pytest.fixture
def _reset_caches() -> object:
    """Module-level `_session_cache`/`_tokenizer_cache` are process-wide lazy singletons --
    save/restore around tests that need `_session()`/`_tokenizer()` to actually re-run their
    loading logic, so this doesn't leak a mocked object into (or lose a real one already
    cached by) other tests in this process."""
    original_session = text_embeddings_module._session_cache
    original_tokenizer = text_embeddings_module._tokenizer_cache
    text_embeddings_module._session_cache = None
    text_embeddings_module._tokenizer_cache = None
    yield object()
    text_embeddings_module._session_cache = original_session
    text_embeddings_module._tokenizer_cache = original_tokenizer


def test_session_loads_the_bundled_model_and_verifies_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_caches: object
) -> None:
    fake_model = tmp_path / "minilm_int8.onnx"
    fake_model.write_bytes(b"fake-onnx-bytes")
    monkeypatch.setattr(text_embeddings_module, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(
        text_embeddings_module,
        "_MINILM_ONNX_SHA256",
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

    monkeypatch.setattr(text_embeddings_module, "require", _fake_require)

    session = text_embeddings_module._session()

    assert isinstance(session, _FakeSession)
    assert session_calls == [(str(fake_model), ["CPUExecutionProvider"])]

    session_again = text_embeddings_module._session()
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


def test_session_raises_actionable_error_when_bundled_model_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_caches: object
) -> None:
    monkeypatch.setattr(text_embeddings_module, "_MODELS_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(
        text_embeddings_module, "require", lambda module_name, *, feature: _FakeOnnxRuntimeModule()
    )

    with pytest.raises(AIModelMissingError, match="bundled AI model file"):
        text_embeddings_module._session()


def test_session_raises_on_sha256_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_caches: object
) -> None:
    tampered_model = tmp_path / "minilm_int8.onnx"
    tampered_model.write_bytes(b"tampered-bytes")
    monkeypatch.setattr(text_embeddings_module, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(text_embeddings_module, "_MINILM_ONNX_SHA256", "0" * 64)
    monkeypatch.setattr(
        text_embeddings_module, "require", lambda module_name, *, feature: _FakeOnnxRuntimeModule()
    )

    with pytest.raises(AIModelMissingError, match="integrity verification"):
        text_embeddings_module._session()


def test_tokenizer_loads_the_bundled_file_and_verifies_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_caches: object
) -> None:
    fake_tokenizer = tmp_path / "minilm_tokenizer.json"
    fake_tokenizer.write_bytes(b"fake-tokenizer-bytes")
    monkeypatch.setattr(text_embeddings_module, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(
        text_embeddings_module,
        "_TOKENIZER_SHA256",
        hashlib.sha256(b"fake-tokenizer-bytes").hexdigest(),
    )

    from_file_calls: list[str] = []

    class _FakeTokenizer:
        def enable_padding(self) -> None:
            pass

        def enable_truncation(self, *, max_length: int) -> None:
            assert max_length == text_embeddings_module._MAX_SEQ_LENGTH

    class _FakeTokenizersModule:
        class Tokenizer:
            @staticmethod
            def from_file(path: str) -> _FakeTokenizer:
                from_file_calls.append(path)
                return _FakeTokenizer()

    def _fake_require(module_name: str, *, feature: str) -> object:
        assert module_name == "tokenizers"
        return _FakeTokenizersModule()

    monkeypatch.setattr(text_embeddings_module, "require", _fake_require)

    tokenizer = text_embeddings_module._tokenizer()

    assert isinstance(tokenizer, _FakeTokenizer)
    assert from_file_calls == [str(fake_tokenizer)]

    tokenizer_again = text_embeddings_module._tokenizer()
    assert tokenizer_again is tokenizer
    assert len(from_file_calls) == 1


class _FakeTokenizersModuleForMismatch:
    class Tokenizer:
        @staticmethod
        def from_file(path: str) -> object:
            raise AssertionError("must not load a tokenizer that failed integrity verification")


def test_tokenizer_raises_on_sha256_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _reset_caches: object
) -> None:
    tampered_tokenizer = tmp_path / "minilm_tokenizer.json"
    tampered_tokenizer.write_bytes(b"tampered-bytes")
    monkeypatch.setattr(text_embeddings_module, "_MODELS_DIR", tmp_path)
    monkeypatch.setattr(text_embeddings_module, "_TOKENIZER_SHA256", "0" * 64)
    monkeypatch.setattr(
        text_embeddings_module,
        "require",
        lambda module_name, *, feature: _FakeTokenizersModuleForMismatch(),
    )

    with pytest.raises(AIModelMissingError, match="integrity verification"):
        text_embeddings_module._tokenizer()


def test_compute_document_embedding_returns_none_for_empty_text(tmp_path: Path) -> None:
    from reclaim.ai.text_embeddings import compute_document_embedding

    assert compute_document_embedding(tmp_path / "a.txt", "   ") is None
