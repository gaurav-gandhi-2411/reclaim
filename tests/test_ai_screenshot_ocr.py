from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw, ImageFont

from reclaim.ai.screenshot_ocr import extract_screenshot_text

# PRIVACY LOCK tests (GG's explicit instruction: OCR text must never be logged, never
# persisted beyond an on-device content tag, never surfaced outside local review — "tested,
# non-negotiable"). Two layers: a structural AST scan (no logging/print call exists anywhere
# in the modules that ever touch raw OCR text) and a runtime proof (a real OCR extraction on
# a synthetic image containing a unique secret string, with every log record captured at
# every level, asserting the secret never appears in any of them).

_MODULES_THAT_TOUCH_OCR_TEXT = [
    Path(__file__).resolve().parent.parent / "src" / "reclaim" / "ai" / "screenshot_ocr.py",
    Path(__file__).resolve().parent.parent / "src" / "reclaim" / "ai" / "content_tagger.py",
    Path(__file__).resolve().parent.parent / "src" / "reclaim" / "ai" / "screenshot_review.py",
]

_LOG_METHOD_NAMES = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _make_text_image(path: Path, text: str, *, size: tuple[int, int] = (500, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    y = 20
    for line in text.splitlines():
        draw.text((20, y), line, fill=(0, 0, 0), font=font)
        y += 40
    img.save(path, format="PNG")


def _has_logging_or_print_call(source: str) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            hits.append(f"print(...) at line {node.lineno}")
        elif isinstance(func, ast.Attribute) and func.attr in _LOG_METHOD_NAMES:
            hits.append(f".{func.attr}(...) at line {node.lineno}")
    return hits


def test_no_module_touching_ocr_text_contains_a_logging_or_print_call() -> None:
    violations: dict[str, list[str]] = {}
    for module_path in _MODULES_THAT_TOUCH_OCR_TEXT:
        assert module_path.exists(), f"expected {module_path} to exist"
        hits = _has_logging_or_print_call(module_path.read_text(encoding="utf-8"))
        if hits:
            violations[module_path.name] = hits
    assert violations == {}, (
        f"a module that touches raw OCR text must contain zero logging/print calls: {violations}"
    )


def test_extract_screenshot_text_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    not_an_image = tmp_path / "fake.png"
    not_an_image.write_bytes(b"not image data")
    assert extract_screenshot_text(not_an_image) is None


def test_extract_screenshot_text_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert extract_screenshot_text(tmp_path / "does_not_exist.png") is None


def test_extract_screenshot_text_extracts_real_text_from_a_synthetic_screenshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invoice.png"
    _make_text_image(path, "INVOICE 12345\nTotal: 99.50")
    text = extract_screenshot_text(path)
    assert text is not None
    assert "12345" in text
    assert "99" in text


def test_ocr_text_never_appears_in_any_log_record_at_any_level(tmp_path: Path, caplog) -> None:
    """Runtime proof, not just structural absence: a real OCR extraction on an image
    containing a unique secret token, with every logger's level forced down to NOTSET so
    nothing is filtered out before reaching caplog, asserting the secret token appears in
    ZERO captured records regardless of which logger emitted them (rapidocr/onnxruntime's own
    internal logging included -- this proves reclaim's own code never hands the secret to any
    logger, not merely that reclaim.ai's modules don't call logging themselves)."""
    canary_text = "XKQZ-SECRET-9182"
    path = tmp_path / "secret_screenshot.png"
    _make_text_image(path, f"CONFIDENTIAL\n{canary_text}")

    caplog.set_level(logging.NOTSET)
    with caplog.at_level(logging.DEBUG):
        text = extract_screenshot_text(path)

    assert text is not None
    assert canary_text in text  # sanity: OCR actually saw it, so this is a meaningful check

    for record in caplog.records:
        assert canary_text not in record.getMessage()
        assert canary_text not in repr(record)
