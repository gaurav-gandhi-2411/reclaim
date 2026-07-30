from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require, require_bundled_model

# Feature 1b Stage 2 (spec §2): sentence embeddings for the RESIDUAL only — clusters
# MinHash/LSH (minhash_lsh.py) couldn't cleanly resolve. `all-MiniLM-L6-v2` (Apache-2.0, tiny,
# CPU-fast) per spec, matching spec §0.6: raw cosine similarity is the reported number, never
# manufactured into a probability. Mirrors phash.py -> image_similarity.py's two-stage role,
# just for the text pipeline's second stage instead of a whole-set prefilter.
#
# Wave 1 P0-B (2026-07-30): rewritten from sentence-transformers/torch to ONNX Runtime +
# the lightweight `tokenizers` package — sentence-transformers (and its own torch/transformers
# dependency) dropped entirely. The full pipeline (BertModel forward -> mean pooling -> L2
# normalize) ships as a single pinned, SHA256-verified int8 ONNX file (23.6MB); a decision-
# grade PR comparison on the real Gutenberg realistic-tier distribution at the shipped 0.95
# operating threshold measured int8 as quality-equivalent to the original torch-fp32 model
# (recall 313/360 -> 312/360, precision 1.0 unchanged across all 7,140 negative pairs) — see
# reports/ai/onnx_quality_parity/ for the full numbers. Tokenization uses the model's own
# exported `tokenizer.json` (WordPiece vocab, identical to what sentence-transformers used
# internally) loaded via the `tokenizers` package alone — confirmed byte-identical token
# ids/attention masks against the original sentence-transformers tokenizer before this
# replaced it.

_PACKAGE_DIR = Path(__file__).parent
_MODELS_DIR = _PACKAGE_DIR / "models"

_MINILM_ONNX_FILENAME = "minilm_int8.onnx"
_MINILM_ONNX_SHA256 = "3dafd08bc939d0c870495d2abbf6a70101d7ac44497eb28bd4597f64cee38096"
_TOKENIZER_FILENAME = "minilm_tokenizer.json"
_TOKENIZER_SHA256 = "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0"
# all-MiniLM-L6-v2's own configured max sequence length (SentenceTransformer.max_seq_length) —
# hardcoded since sentence-transformers, the only thing that used to expose this, is gone.
_MAX_SEQ_LENGTH = 256

# Lazily constructed once per process — loading the ONNX session/tokenizer is the expensive
# part; running inference against an already-loaded session is fast. Not thread-safe by
# construction (module-level mutable cache), same posture as image_embeddings.py's
# `_session_cache` — this codebase's AI-layer code has no concurrent-access requirement today
# (ADR-0011 — "no UI wiring" posture).
_session_cache: object | None = None
_tokenizer_cache: object | None = None


def _session() -> object:
    global _session_cache
    if _session_cache is None:
        onnxruntime = require("onnxruntime", feature="sentence-embedding residual resolution")
        model_path = require_bundled_model(
            _MODELS_DIR / _MINILM_ONNX_FILENAME,
            expected_sha256=_MINILM_ONNX_SHA256,
            feature="sentence-embedding residual resolution",
        )
        _session_cache = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
    return _session_cache


def _tokenizer() -> object:
    global _tokenizer_cache
    if _tokenizer_cache is None:
        tokenizers_module = require("tokenizers", feature="sentence-embedding residual resolution")
        tokenizer_path = require_bundled_model(
            _MODELS_DIR / _TOKENIZER_FILENAME,
            expected_sha256=_TOKENIZER_SHA256,
            feature="sentence-embedding residual resolution",
        )
        tokenizer = tokenizers_module.Tokenizer.from_file(str(tokenizer_path))
        tokenizer.enable_padding()
        tokenizer.enable_truncation(max_length=_MAX_SEQ_LENGTH)
        _tokenizer_cache = tokenizer
    return _tokenizer_cache


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
    numpy = require("numpy", feature="cosine similarity computation")
    tokenizer = _tokenizer()
    encoded = tokenizer.encode(text)  # type: ignore[attr-defined]
    input_ids = numpy.array([encoded.ids], dtype=numpy.int64)
    attention_mask = numpy.array([encoded.attention_mask], dtype=numpy.int64)
    session = _session()
    output = session.run(  # type: ignore[attr-defined]
        None, {"input_ids": input_ids, "attention_mask": attention_mask}
    )[0]
    return DocumentEmbedding(path=path, vector=tuple(float(v) for v in output[0]))


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
