"""Regenerates `src/reclaim/ai/models/*` (the bundled ONNX CLIP + MiniLM models and MiniLM's
tokenizer) from the project's pinned torch checkpoints.

Wave 1 P0-B (2026-07-30): NOT part of the normal dev workflow — `reclaim.ai.image_embeddings`/
`text_embeddings` load these already-generated files directly, with no torch/open_clip/
sentence-transformers dependency at runtime. This script exists only to REGENERATE them (e.g.
if the pinned upstream checkpoint revision in `image_embeddings.py`/`text_embeddings.py` is
ever bumped), and needs the separate `ai-export` extras group, never installed by a normal
`uv sync --extra ai`:

    uv sync --extra ai-export
    uv run python scripts/export_ai_models.py

CLIP ships fp16 (not int8): a decision-grade BCubed comparison on real INRIA Copydays blocks
measured int8 at a material -11pp precision regression at the shipped 0.82 operating
threshold, while fp16 was bit-identical BCubed behavior to the original torch-fp32 model. See
`reports/ai/onnx_quality_parity/` for the full measurement this decision is based on — if you
change what this script produces, re-run that comparison, don't just assume parity holds.

MiniLM ships int8: the same real-eval methodology found it quality-equivalent (recall
313/360 -> 312/360, precision 1.0 unchanged across 7,140 negative pairs) at the shipped 0.95
operating threshold on the Gutenberg realistic-tier distribution.

Known gap, disclosed not silently worked around: MiniLM's fp16 conversion via
`onnxconverter_common.float16.convert_float_to_float16` produces an ONNX Runtime load error
(`Type Error: Type (tensor(float16)) of output arg (_to_copy_1) ... does not match expected
type (tensor(float))`) — a real bug in how the float16 converter handles a cast node this
particular exported graph shape produces, not something this script's own logic causes.
Un-investigated because MiniLM-int8 already passed the quality bar with no need for an fp16
fallback; if a future reason to want MiniLM-fp16 specifically comes up, start there.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "src" / "reclaim" / "ai" / "models"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Pinned CLIP checkpoint (ADR-0028's supply-chain-integrity pattern) -- this script's OWN
# standalone copy, since the production `image_embeddings.py` this used to live in no longer
# has any torch/open_clip loading code at all (Wave 1 P0-B dropped it entirely).
_OPEN_CLIP_MODEL_NAME = "ViT-B-32-quickgelu"
_OPEN_CLIP_HF_REPO = "timm/vit_base_patch32_clip_224.openai"
_OPEN_CLIP_HF_REVISION = "a6f597a30f7b82c51704746581f9a4e41421e878"
_OPEN_CLIP_HF_WEIGHTS_FILENAME = "open_clip_model.safetensors"
_OPEN_CLIP_HF_WEIGHTS_SHA256 = "e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31"

# Pinned MiniLM checkpoint, same reasoning.
_MINILM_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_MINILM_HF_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
_MINILM_WEIGHTS_FILENAME = "model.safetensors"
_MINILM_WEIGHTS_SHA256 = "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db"


def _verify_sha256_or_quarantine(path: Path, expected_sha256: str) -> None:
    actual = _sha256(path)
    if actual != expected_sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"checkpoint integrity check failed for {path}: expected sha256 {expected_sha256}, "
            f"got {actual}. The corrupted/tampered file has been deleted; retry to re-download."
        )


def _load_pinned_clip() -> tuple[object, object]:
    import huggingface_hub
    import open_clip

    checkpoint_path = Path(
        huggingface_hub.hf_hub_download(
            repo_id=_OPEN_CLIP_HF_REPO,
            filename=_OPEN_CLIP_HF_WEIGHTS_FILENAME,
            revision=_OPEN_CLIP_HF_REVISION,
        )
    )
    _verify_sha256_or_quarantine(checkpoint_path, _OPEN_CLIP_HF_WEIGHTS_SHA256)
    tag_cfg = open_clip.get_pretrained_cfg(_OPEN_CLIP_MODEL_NAME, "openai")
    model, _, preprocess = open_clip.create_model_and_transforms(
        _OPEN_CLIP_MODEL_NAME,
        pretrained=str(checkpoint_path),
        force_quick_gelu=tag_cfg["quick_gelu"],
        image_mean=tag_cfg["mean"],
        image_std=tag_cfg["std"],
        image_interpolation=tag_cfg["interpolation"],
        image_resize_mode=tag_cfg["resize_mode"],
    )
    model.eval()
    return model, preprocess


def _load_pinned_minilm() -> object:
    import huggingface_hub
    from sentence_transformers import SentenceTransformer

    weights_path = Path(
        huggingface_hub.hf_hub_download(
            repo_id=_MINILM_MODEL_NAME,
            filename=_MINILM_WEIGHTS_FILENAME,
            revision=_MINILM_HF_REVISION,
        )
    )
    _verify_sha256_or_quarantine(weights_path, _MINILM_WEIGHTS_SHA256)
    return SentenceTransformer(_MINILM_MODEL_NAME, revision=_MINILM_HF_REVISION)


def export_clip() -> Path:
    """Exports the pinned CLIP vision encoder (open_clip ViT-B-32-quickgelu/openai) to ONNX
    fp32, verifies it against the torch reference, then converts to fp16."""
    import onnx
    import torch
    from onnxconverter_common import float16

    print("Loading pinned CLIP checkpoint (torch)...")
    model, _preprocess = _load_pinned_clip()

    class _VisionWrapper(torch.nn.Module):
        def __init__(self, clip_model: torch.nn.Module) -> None:
            super().__init__()
            self.clip_model = clip_model

        def forward(self, pixel_values: torch.Tensor) -> torch.Tensor:
            return self.clip_model.encode_image(pixel_values)

    wrapper = _VisionWrapper(model)
    wrapper.eval()
    dummy = torch.randn(1, 3, 224, 224)
    with torch.no_grad():
        reference = wrapper(dummy).numpy()

    fp32_path = MODELS_DIR / "_clip_vision_fp32_intermediate.onnx"
    torch.onnx.export(
        wrapper,
        dummy,
        str(fp32_path),
        input_names=["pixel_values"],
        output_names=["image_embeds"],
        dynamic_axes={"pixel_values": {0: "batch"}, "image_embeds": {0: "batch"}},
        opset_version=17,
        do_constant_folding=True,
    )

    import numpy as np
    import onnxruntime as ort

    session = ort.InferenceSession(str(fp32_path), providers=["CPUExecutionProvider"])
    onnx_output = session.run(None, {"pixel_values": dummy.numpy()})[0]
    max_abs_diff = float(np.max(np.abs(onnx_output - reference)))
    print(f"  fp32 ONNX vs torch max abs diff: {max_abs_diff:.6e}")
    if max_abs_diff >= 1e-3:
        raise RuntimeError(f"fp32 CLIP ONNX export diverges from torch: {max_abs_diff}")

    fp16_path = MODELS_DIR / "clip_vision_fp16.onnx"
    fp32_model = onnx.load(str(fp32_path))
    fp16_model = float16.convert_float_to_float16(fp32_model, keep_io_types=True)
    onnx.save(fp16_model, str(fp16_path))

    fp32_path.unlink()
    data_file = fp32_path.with_suffix(".onnx.data")
    if data_file.exists():
        data_file.unlink()

    print(f"CLIP fp16 exported: {fp16_path} ({fp16_path.stat().st_size / 1e6:.1f}MB)")
    print(f"  sha256: {_sha256(fp16_path)}")
    return fp16_path


def export_minilm() -> tuple[Path, Path]:
    """Exports the pinned MiniLM full pipeline (BertModel -> mean pooling -> L2 normalize) to
    ONNX fp32, verifies it against the torch reference, converts to int8, and exports the
    tokenizer (`tokenizer.json`) alongside it."""
    import onnxruntime as ort
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic

    print("Loading pinned MiniLM checkpoint (torch)...")
    st_model = _load_pinned_minilm()

    class _MiniLMWrapper(torch.nn.Module):
        def __init__(self, st_model: torch.nn.Module) -> None:
            super().__init__()
            self.transformer = st_model[0]
            self.pooling = st_model[1]
            self.normalize = st_model[2]

        def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
            features = {"input_ids": input_ids, "attention_mask": attention_mask}
            features = self.transformer(features)
            features = self.pooling(features)
            features = self.normalize(features)
            return features["sentence_embedding"]

    wrapper = _MiniLMWrapper(st_model)
    wrapper.eval()
    tokenizer = st_model.tokenizer
    sample = tokenizer(
        ["This is a test sentence.", "Another example sentence for tracing."],
        padding=True,
        truncation=True,
        return_tensors="pt",
    )
    with torch.no_grad():
        reference = wrapper(sample["input_ids"], sample["attention_mask"]).numpy()

    fp32_path = MODELS_DIR / "_minilm_fp32_intermediate.onnx"
    torch.onnx.export(
        wrapper,
        (sample["input_ids"], sample["attention_mask"]),
        str(fp32_path),
        input_names=["input_ids", "attention_mask"],
        output_names=["sentence_embedding"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "seq"},
            "attention_mask": {0: "batch", 1: "seq"},
            "sentence_embedding": {0: "batch"},
        },
        opset_version=17,
        do_constant_folding=True,
    )

    import numpy as np

    session = ort.InferenceSession(str(fp32_path), providers=["CPUExecutionProvider"])
    onnx_output = session.run(
        None,
        {
            "input_ids": sample["input_ids"].numpy(),
            "attention_mask": sample["attention_mask"].numpy(),
        },
    )[0]
    max_abs_diff = float(np.max(np.abs(onnx_output - reference)))
    print(f"  fp32 ONNX vs torch max abs diff: {max_abs_diff:.6e}")
    if max_abs_diff >= 1e-3:
        raise RuntimeError(f"fp32 MiniLM ONNX export diverges from torch: {max_abs_diff}")

    int8_path = MODELS_DIR / "minilm_int8.onnx"
    quantize_dynamic(str(fp32_path), str(int8_path), weight_type=QuantType.QInt8)
    fp32_path.unlink()

    tokenizer_path = MODELS_DIR / "minilm_tokenizer.json"
    tokenizer.save_pretrained(str(MODELS_DIR / "_tokenizer_export_tmp"))
    (MODELS_DIR / "_tokenizer_export_tmp" / "tokenizer.json").replace(tokenizer_path)
    for leftover in (MODELS_DIR / "_tokenizer_export_tmp").glob("*"):
        leftover.unlink()
    (MODELS_DIR / "_tokenizer_export_tmp").rmdir()

    print(f"MiniLM int8 exported: {int8_path} ({int8_path.stat().st_size / 1e6:.1f}MB)")
    print(f"  sha256: {_sha256(int8_path)}")
    print(f"Tokenizer exported: {tokenizer_path}")
    print(f"  sha256: {_sha256(tokenizer_path)}")
    return int8_path, tokenizer_path


def main() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    clip_path = export_clip()
    minilm_path, tokenizer_path = export_minilm()

    print("\nDone. Update these SHA256 constants if they changed:")
    print(f"  image_embeddings._CLIP_ONNX_SHA256 = {_sha256(clip_path)!r}")
    print(f"  text_embeddings._MINILM_ONNX_SHA256 = {_sha256(minilm_path)!r}")
    print(f"  text_embeddings._TOKENIZER_SHA256 = {_sha256(tokenizer_path)!r}")


if __name__ == "__main__":
    main()
