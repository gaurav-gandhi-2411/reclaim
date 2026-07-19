from __future__ import annotations

from pathlib import Path

from reclaim.ai._optional import require

# Feature 2's local OCR extraction — PRIVACY LOCK (GG's explicit instruction, non-negotiable):
# OCR text is NEVER logged, NEVER persisted beyond an on-device content TAG (content_tagger.py
# consumes this module's output and returns only a tag, never the raw text), and NEVER
# surfaced outside local review. This module, like document_text.py, contains ZERO logging
# calls anywhere — not even at debug level, not even a filename-only line — because the
# easiest way to guarantee OCR'd text never leaves the device is to never hand it to a logger,
# a print statement, an exception message, or any other sink in the first place. Every
# exception path below is deliberately silent about content (no OCR text embedded in a
# raised/returned error), even though that makes debugging a failed extraction harder — that
# tradeoff is intentional, not an oversight.
#
# rapidocr-onnxruntime (Apache-2.0) — pip-installable, bundled ONNX models, no system binary
# (unlike Tesseract), no network calls at inference time. See ADR-0019.

_MIN_CONFIDENCE = 0.5  # rapidocr reports a per-line confidence score; lines below this are
# dropped as noise (a receipt/document photographed at an angle, background texture
# misread as text, etc.) rather than passed through to the content tagger uncritically.

_engine_cache: object | None = None


def _engine() -> object:
    global _engine_cache
    if _engine_cache is None:
        rapidocr = require("rapidocr_onnxruntime", feature="local screenshot OCR")
        _engine_cache = rapidocr.RapidOCR()
    return _engine_cache


def extract_screenshot_text(path: Path) -> str | None:
    """Returns the extracted plain text (newline-joined per detected line, low-confidence
    lines dropped), or `None` for a file that fails to decode or yields no text at all —
    common and expected (a screenshot that's mostly a photo/icon with no readable text),
    never worth raising for.

    CALLERS MUST NEVER log, print, or persist this function's return value beyond deriving a
    `content_tagger.ContentTag` from it (see that module) — this function's own contract ends
    at "return the text as a Python string in memory," which is as far as this module's
    privacy guarantee extends; it cannot enforce what a caller does with the return value, only
    guarantee it never does anything privacy-violating itself.
    """
    engine = _engine()
    try:
        result, _elapsed = engine(str(path))  # type: ignore[operator]
    except Exception:
        # OCR engine failures are opaque here on purpose: no exception message may embed
        # partial extracted text (some OCR backends include recognized fragments in error
        # payloads for debugging) — treat every failure identically, as "no text available."
        return None

    if not result:
        return None

    lines = [text for _box, text, confidence in result if confidence >= _MIN_CONFIDENCE]
    if not lines:
        return None
    return "\n".join(lines)
