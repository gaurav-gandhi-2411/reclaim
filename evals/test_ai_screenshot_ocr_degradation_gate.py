from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from ai_fixtures.build_content_tag_fixtures import chat_texts, code_texts, receipt_texts
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from reclaim.ai.content_tagger import KEEP_BIASED_TAGS, ContentTag, tag_content
from reclaim.ai.eval_harness import EvalReport, current_commit_sha
from reclaim.ai.screenshot_ocr import extract_screenshot_text

# GG's follow-up: "confirm the OCR-failure -> transient_ui path is closed, not just the
# gibberish case." The gibberish fix (content_tagger.py's sparse_bonus cap, tested in
# tests/test_ai_content_tagger.py and evals/test_ai_content_tag_gold.py's `unknown` stress
# pool) proves synthetic short strings never buy a confident TRANSIENT_UI classification. It
# does NOT prove the same holds for a REAL content-bearing screenshot that OCRs poorly for a
# real-world reason -- dark/low-contrast capture, unusual font, a partial/cut-off screenshot,
# heavy blur. Real content degraded into sparse/fragmented OCR output is architecturally the
# same shape of risk as gibberish (little text, few/no keyword hits), so it must be proven
# separately, on real degraded images through the real OCR engine, not assumed to be covered
# by the synthetic case.
#
# This is F2's one real over-deletion path: TRANSIENT_UI is the only content tag that is ever
# deletion-eligible (content_tagger.KEEP_BIASED_TAGS excludes only it). If a genuinely
# meaningful screenshot (a receipt, a document, code, a chat) degrades into sparse OCR output
# that still crosses the confidence floor as TRANSIENT_UI, this classifier would recommend
# deleting something the user actually wants kept -- the exact failure "bias STRONGLY toward
# keep" exists to prevent. The required behavior: "OCR found little" must mean "I can't tell"
# (UNKNOWN, browse-only), never "transient" (deletion-eligible).


def _make_base_image(text: str, *, size: tuple[int, int] = (640, 480)) -> Image.Image:
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    y = 20
    for line in text.splitlines():
        draw.text((20, y), line, fill=(20, 20, 20), font=font)
        y += 30
    return img


def _degrade_blur_heavy(img: Image.Image) -> Image.Image:
    """Simulates an out-of-focus/motion-blurred capture."""
    return img.filter(ImageFilter.GaussianBlur(radius=5))


def _degrade_low_contrast(img: Image.Image) -> Image.Image:
    """Simulates a dark/washed-out capture -- text barely distinguishable from background."""
    return ImageEnhance.Contrast(img).enhance(0.06)


def _degrade_cropped(img: Image.Image) -> Image.Image:
    """Simulates a partial/cut-off screenshot -- only the top sliver of content survives."""
    width, height = img.size
    return img.crop((0, 0, width, max(1, height // 6)))


def _degrade_combined_worst_case(img: Image.Image) -> Image.Image:
    """Blur + low-contrast + crop together -- the realistic worst case: a bad capture of a
    partial screen, not a single isolated defect."""
    return _degrade_cropped(_degrade_low_contrast(_degrade_blur_heavy(img)))


_DEGRADATIONS = {
    "blur_heavy": _degrade_blur_heavy,
    "low_contrast": _degrade_low_contrast,
    "cropped": _degrade_cropped,
    "combined_worst_case": _degrade_combined_worst_case,
}


@dataclass(frozen=True, slots=True)
class BaseSample:
    content_kind: str  # receipt | document | code | chat -- NOT a ContentTag, just a label
    text: str


def _document_text() -> str:
    # Deliberately NOT sourced from the Gutenberg corpus (ADR-0017/ADR-0019's document-tag
    # fixture) -- this eval must run unconditionally (no fetch-corpus precondition) since it
    # is proving a safety property, not measuring an operating point.
    return (
        "Chapter Three: The Long Winter\n"
        "In the years that followed the great migration, the settlers found themselves\n"
        "increasingly dependent on trade routes that stretched far beyond the valley.\n"
        "This chapter summarizes those changes and their long-term consequences for\n"
        "the region as a whole, drawing on archival records and oral histories."
    )


def _base_samples() -> list[BaseSample]:
    return [
        BaseSample("receipt", receipt_texts(n=1)[0]),
        BaseSample("document", _document_text()),
        BaseSample("code", code_texts()[0]),
        BaseSample("chat", chat_texts(n=1)[0]),
    ]


def test_degraded_real_content_never_tags_transient_ui(tmp_path: Path) -> None:
    samples = _base_samples()
    assert len(samples) == 4

    rows: list[dict[str, object]] = []
    near_empty_rows: list[dict[str, object]] = []

    for sample in samples:
        base_img = _make_base_image(sample.text)
        for degradation_name, degrade_fn in _DEGRADATIONS.items():
            degraded = degrade_fn(base_img)
            path = tmp_path / f"{sample.content_kind}_{degradation_name}.png"
            degraded.save(path, format="PNG")

            extracted = extract_screenshot_text(path)
            result = tag_content(extracted)

            rows.append(
                {
                    "content_kind": sample.content_kind,
                    "degradation": degradation_name,
                    "extracted_char_count": len(extracted) if extracted else 0,
                    "predicted_tag": result.tag.value,
                    "score": result.score,
                }
            )

            assert result.tag != ContentTag.TRANSIENT_UI, (
                f"degraded {sample.content_kind} image ({degradation_name}) was classified "
                f"TRANSIENT_UI -- the one deletion-eligible tag -- from real content that "
                f"OCR'd poorly, extracted={extracted!r}"
            )
            assert result.tag in KEEP_BIASED_TAGS, (
                f"degraded {sample.content_kind} image ({degradation_name}) produced a tag "
                f"outside the keep-biased set: {result.tag}"
            )

            is_near_empty = extracted is None or len(extracted.strip()) <= 3
            if is_near_empty:
                near_empty_rows.append(
                    {
                        "content_kind": sample.content_kind,
                        "degradation": degradation_name,
                        "extracted": extracted,
                        "predicted_tag": result.tag.value,
                    }
                )
                assert result.tag == ContentTag.UNKNOWN, (
                    f"OCR found almost nothing for degraded {sample.content_kind} "
                    f"({degradation_name}, extracted={extracted!r}) but the classifier "
                    f"committed to {result.tag!r} instead of UNKNOWN -- 'OCR found little' "
                    f"must mean 'I can't tell' (browse-only), never a confident tag"
                )

    # --- Direct proof of the specific invariant, independent of whether any real image
    # above happened to degrade all the way to near-empty: OCR returning nothing at all, or
    # a couple of stray characters, must resolve to UNKNOWN -- never TRANSIENT_UI. ---
    for near_empty_text in (None, "", "  ", ".", "1", "x", "..", "||"):
        result = tag_content(near_empty_text)
        assert result.tag == ContentTag.UNKNOWN, (
            f"near-empty OCR text {near_empty_text!r} must tag UNKNOWN, got {result.tag!r}"
        )

    print(f"\n{len(rows)} degraded (content_kind x degradation) combinations tested:")  # noqa: T201
    for row in rows:
        print(  # noqa: T201
            f"  {row['content_kind']:10s} + {row['degradation']:20s} -> "
            f"tag={row['predicted_tag']:10s} score={row['score']:.2f} "
            f"chars_extracted={row['extracted_char_count']}"
        )
    print(f"\n{len(near_empty_rows)} combinations degraded to near-empty OCR output — all UNKNOWN.")  # noqa: T201
    assert len(near_empty_rows) > 0, (
        "sanity: expected at least one degradation to actually produce near-empty OCR "
        "output (combined_worst_case is designed to) -- if this never happens, the "
        "degradation fixtures aren't actually stressing the near-empty path and this "
        "eval isn't testing what it claims to"
    )

    report_path = Path("reports/ai/screenshot_ocr_degradation_tier.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "commit_sha": current_commit_sha(),
                "command": "uv run pytest evals/test_ai_screenshot_ocr_degradation_gate.py -v -s",
                "fixture": "in-memory PIL degradation of receipt/document/code/chat renders",
                "rows": rows,
                "near_empty_rows": near_empty_rows,
                "note": (
                    "Zero degraded real-content sample tagged TRANSIENT_UI; every near-empty "
                    "OCR result tagged UNKNOWN. Closes F2's one real over-deletion path."
                ),
            },
            indent=2,
        )
    )
    report = EvalReport(
        metric_name="screenshot_ocr_degradation_transient_ui_false_positives",
        value=0.0,
        commit_sha=current_commit_sha(),
        command="uv run pytest evals/test_ai_screenshot_ocr_degradation_gate.py -v -s",
        fixture_path=str(report_path),
    )
    print(f"\n{report}")  # noqa: T201
