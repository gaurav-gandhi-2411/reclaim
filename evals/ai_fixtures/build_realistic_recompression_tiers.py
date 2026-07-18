from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# Generates the REALISTIC near-dup distribution Feature 1a actually targets — ordinary
# consumer accumulation (re-saves, resizes, format round-trips, messaging-app re-compression)
# — applied to REAL photographic content (Copydays' 157 real, unmodified originals), not
# synthetic drawn shapes. Built because Copydays' own milder graduated splits (`jpeg`, `crop`)
# were not recoverable from any reachable mirror (see ADR-0015/ADR-0012's follow-up): GG's
# instruction was explicit that if a public mild-tier set isn't scriptable, generate the
# transformations programmatically from clean images with known ground truth instead of
# measuring recall against Copydays' `strong` split alone, which is a single, deliberately
# adversarial attack tier, not the operating distribution.
#
# Every transform here is DETERMINISTIC (no randomness) — same source image always produces
# the same byte-identical output, so this is reproducible without needing a seed the way
# build_image_similarity_fixtures.py's synthetic generator does.

_MILD = "mild"
_MODERATE = "moderate"
_MESSAGING_APP = "messaging_app"

_MESSAGING_APP_MAX_LONG_EDGE = 1600  # WhatsApp/Instagram-style downscale ceiling


@dataclass(frozen=True, slots=True)
class RealisticVariant:
    path: Path
    source_original: Path
    block_id: str
    tier: str  # "mild" | "moderate" | "messaging_app"
    profile: str  # specific named transform, for diagnostics


def _mild_recompress_q92(img: Image.Image, out_path: Path) -> None:
    """Re-save at the same dimensions, high JPEG quality — the lightest possible touch: an
    app re-exporting a photo without resizing it, just re-encoding (e.g. a share-sheet
    re-save)."""
    img.convert("RGB").save(out_path, format="JPEG", quality=92)


def _mild_resize_97_q90(img: Image.Image, out_path: Path) -> None:
    """Barely-perceptible downscale (97%) + high quality — a near-lossless re-export, the
    mildest realistic accumulation case."""
    width, height = img.size
    resized = img.resize((round(width * 0.97), round(height * 0.97)), Image.LANCZOS)
    resized.convert("RGB").save(out_path, format="JPEG", quality=90)


def _moderate_resize_80_q70(img: Image.Image, out_path: Path) -> None:
    """A typical "resize to share" copy: noticeably smaller, moderate JPEG quality — the
    shape of an email attachment downsize or a manual "make it smaller" export."""
    width, height = img.size
    resized = img.resize((round(width * 0.80), round(height * 0.80)), Image.LANCZOS)
    resized.convert("RGB").save(out_path, format="JPEG", quality=70)


def _moderate_roundtrip_png_q65(img: Image.Image, out_path: Path, *, scratch_png: Path) -> None:
    """Format-conversion generation loss: resize, round-trip through lossless PNG (simulating
    an intermediate editing step), then re-compress as JPEG — two compression generations
    stacked, which real accumulated duplicates often carry (edited-then-re-exported, or
    passed through more than one app)."""
    width, height = img.size
    resized = img.resize((round(width * 0.90), round(height * 0.90)), Image.LANCZOS).convert("RGB")
    resized.save(scratch_png, format="PNG")
    with Image.open(scratch_png) as roundtripped:
        roundtripped.convert("RGB").save(out_path, format="JPEG", quality=65)
    scratch_png.unlink()


def _messaging_app_resave(img: Image.Image, out_path: Path) -> None:
    """WhatsApp/Instagram/Messenger-style re-save: downscale to fit within a max long edge
    (never upscale a smaller original), moderate-low JPEG quality, no EXIF preserved (Pillow
    strips metadata by default when no `exif=` kwarg is passed) — this is the single most
    common real-world source of "why do I have two copies of this photo" in a consumer
    library, named explicitly per GG's instruction."""
    width, height = img.size
    long_edge = max(width, height)
    if long_edge > _MESSAGING_APP_MAX_LONG_EDGE:
        scale = _MESSAGING_APP_MAX_LONG_EDGE / long_edge
        img = img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    img.convert("RGB").save(out_path, format="JPEG", quality=75)


_PROFILES: tuple[tuple[str, str], ...] = (
    ("mild_recompress_q92", _MILD),
    ("mild_resize_97_q90", _MILD),
    ("moderate_resize_80_q70", _MODERATE),
    ("moderate_roundtrip_png_q65", _MODERATE),
    ("messaging_app_resave", _MESSAGING_APP),
)


def build_realistic_tiers(
    originals: list[Path], output_root: Path, *, force: bool = False
) -> list[RealisticVariant]:
    """One variant per (original, profile) — 5 variants per original, all deterministic.
    Skips regeneration when `output_root` is already populated with the expected file count,
    unless `force` (mirrors fetch_copydays.py's idempotency posture)."""
    output_root.mkdir(parents=True, exist_ok=True)
    expected_count = len(originals) * len(_PROFILES)
    existing = list(output_root.glob("*.jpg"))
    if not force and len(existing) == expected_count:
        return _catalog(originals, output_root)

    for original_path in originals:
        block_id = original_path.stem[:4]
        for profile, _tier in _PROFILES:
            out_path = output_root / f"{block_id}_{profile}.jpg"
            with Image.open(original_path) as fresh:  # fresh handle per profile: each
                fresh.load()  # transform mutates/resizes its own copy in place
                if profile == "mild_recompress_q92":
                    _mild_recompress_q92(fresh, out_path)
                elif profile == "mild_resize_97_q90":
                    _mild_resize_97_q90(fresh, out_path)
                elif profile == "moderate_resize_80_q70":
                    _moderate_resize_80_q70(fresh, out_path)
                elif profile == "moderate_roundtrip_png_q65":
                    _moderate_roundtrip_png_q65(
                        fresh, out_path, scratch_png=output_root / f"_scratch_{block_id}.png"
                    )
                elif profile == "messaging_app_resave":
                    _messaging_app_resave(fresh, out_path)
                else:  # pragma: no cover - exhaustive over _PROFILES above
                    raise AssertionError(f"unhandled profile {profile}")

    return _catalog(originals, output_root)


def _catalog(originals: list[Path], output_root: Path) -> list[RealisticVariant]:
    variants = []
    for original_path in originals:
        block_id = original_path.stem[:4]
        for profile, tier in _PROFILES:
            variants.append(
                RealisticVariant(
                    path=output_root / f"{block_id}_{profile}.jpg",
                    source_original=original_path,
                    block_id=block_id,
                    tier=tier,
                    profile=profile,
                )
            )
    return variants
