from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Feature 2's classical (v1, not ML) content-tag classifier — same posture as keep_best.py's
# classical quality scorer: a transparent, documented, keyword/pattern-based formula, not a
# trained model, per spec's "NIMA/a learned classifier is a v2, add-only-if-measured-to-help
# upgrade, never a default." Every signal here is directly inspectable; `tag_content` never
# returns a manufactured "confidence probability" (spec §0.6) — `TagResult.score` is a raw,
# documented weighted sum, a ranking signal only.
#
# PRIVACY LOCK: this module receives OCR'd text (from screenshot_ocr.py) and returns ONLY a
# `ContentTag` (+ a numeric score) — never the text itself, never a text excerpt, never a
# keyword match location. Callers holding onto the original text after tagging are outside
# this module's control, but this module's own return value structurally cannot leak it.


class ContentTag(StrEnum):
    RECEIPT = "receipt"
    DOCUMENT = "document"
    CODE = "code"
    CHAT = "chat"
    TRANSIENT_UI = "transient_ui"
    UNKNOWN = "unknown"  # no tag's score cleared the minimum confidence — deliberately NOT a
    # silent default toward any specific tag, especially not toward TRANSIENT_UI (the only
    # deletion-eligible tag): an ambiguous screenshot must never be misread as "safe to flag."


# Only TRANSIENT_UI is ever deletion-eligible (spec: "bias STRONGLY toward keep for receipt/
# document/code tags"; CHAT and UNKNOWN are conservatively included in the keep-biased set too
# — a chat screenshot may hold a meaningful personal conversation, and UNKNOWN means the
# classifier itself isn't confident, which is exactly when caution is warranted most).
KEEP_BIASED_TAGS: frozenset[ContentTag] = frozenset(
    {ContentTag.RECEIPT, ContentTag.DOCUMENT, ContentTag.CODE, ContentTag.CHAT, ContentTag.UNKNOWN}
)

_MIN_CONFIDENT_SCORE = 1.5  # below this, no tag is confident enough to commit to — UNKNOWN.

_SCORED_TAGS: tuple[ContentTag, ...] = (
    ContentTag.RECEIPT,
    ContentTag.CODE,
    ContentTag.CHAT,
    ContentTag.TRANSIENT_UI,
    ContentTag.DOCUMENT,
)

_CURRENCY_AMOUNT_RE = re.compile(r"[\$€£]\s?\d+[.,]\d{2}\b")
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:AM|PM|am|pm)?\b")
_CODE_SYMBOL_RE = re.compile(r"[{}();=<>]")
_CODE_KEYWORDS = (
    "def ",
    "function ",
    "class ",
    "import ",
    "return ",
    "const ",
    "let ",
    "var ",
    "public ",
    "private ",
    "void ",
    "if (",
    "if(",
    "for (",
    "for(",
    "while (",
    "while(",
    "#include",
    "public class",
    "self.",
    "async ",
    "await ",
)
_RECEIPT_KEYWORDS = (
    "total",
    "subtotal",
    "tax",
    "receipt",
    "change due",
    "cash",
    "qty",
    "item",
    "thank you for your purchase",
    "cashier",
    "invoice",
    "amount due",
    "balance due",
)
_CHAT_KEYWORDS = (
    "sent",
    "delivered",
    "read",
    "typing...",
    "online",
    "last seen",
    "you:",
    "replied to",
)
_TRANSIENT_UI_KEYWORDS = (
    "loading",
    "please wait",
    "connecting",
    "retry",
    "try again",
    "tap to continue",
    "swipe to continue",
    "no internet connection",
    "reconnecting",
    "syncing",
)
_DOCUMENT_KEYWORDS = (
    "chapter",
    "section",
    "introduction",
    "summary",
    "conclusion",
    "paragraph",
)


@dataclass(frozen=True, slots=True)
class TagResult:
    tag: ContentTag
    score: float  # the winning tag's raw score — a ranking signal, never a probability


def _count_keyword_hits(text_lower: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in text_lower)


def _score_receipt(text: str, text_lower: str, word_count: int) -> float:
    currency_hits = len(_CURRENCY_AMOUNT_RE.findall(text))
    keyword_hits = _count_keyword_hits(text_lower, _RECEIPT_KEYWORDS)
    return currency_hits * 1.5 + keyword_hits * 1.0


def _score_code(text: str, text_lower: str, word_count: int) -> float:
    symbol_hits = len(_CODE_SYMBOL_RE.findall(text))
    keyword_hits = _count_keyword_hits(text_lower, _CODE_KEYWORDS)
    # Symbol density matters more than raw count for short OCR snippets — a receipt with a
    # couple of parentheses shouldn't out-score real code, but 20 braces/semicolons in 50
    # words is a strong code signal regardless of absolute length.
    density_bonus = 2.0 if word_count > 0 and (symbol_hits / max(word_count, 1)) > 0.15 else 0.0
    return symbol_hits * 0.3 + keyword_hits * 1.5 + density_bonus


def _score_chat(text: str, text_lower: str, word_count: int) -> float:
    timestamp_hits = len(_TIMESTAMP_RE.findall(text))
    keyword_hits = _count_keyword_hits(text_lower, _CHAT_KEYWORDS)
    lines = [line for line in text.splitlines() if line.strip()]
    short_line_ratio = (
        sum(1 for line in lines if len(line.split()) <= 8) / len(lines) if lines else 0.0
    )
    # Many short lines (message-bubble-like) plus at least one timestamp/chat-UI keyword is a
    # much stronger signal than either alone — a receipt also has short lines, a document
    # might mention a time once; the combination is what's actually distinctive.
    short_line_bonus = 1.5 if short_line_ratio > 0.6 and len(lines) >= 3 else 0.0
    return timestamp_hits * 1.0 + keyword_hits * 1.5 + short_line_bonus


def _score_transient_ui(text: str, text_lower: str, word_count: int) -> float:
    keyword_hits = _count_keyword_hits(text_lower, _TRANSIENT_UI_KEYWORDS)
    # Sparseness alone is deliberately capped BELOW `_MIN_CONFIDENT_SCORE` (1.5) — a few words
    # of OCR noise/gibberish with zero actual transient-UI vocabulary must resolve to UNKNOWN,
    # never to the one tag that's deletion-eligible. Sparseness only PUSHES an already-present
    # keyword signal higher (a real "Loading..." screen is both sparse AND keyword-matched);
    # it must never be sufficient on its own to confidently claim transient-UI.
    sparse_bonus = 1.0 if 0 < word_count <= 8 else (0.5 if word_count <= 15 else 0.0)
    return keyword_hits * 2.0 + sparse_bonus


def _score_document(text: str, text_lower: str, word_count: int) -> float:
    keyword_hits = _count_keyword_hits(text_lower, _DOCUMENT_KEYWORDS)
    sentence_count = len(re.findall(r"[.!?]\s|[.!?]$", text))
    # Real prose: enough words to form actual sentences, a plausible sentences-per-word ratio
    # (not one giant run-on, not fragments), and comfortably longer than a transient-UI screen.
    prose_bonus = 1.5 if word_count >= 25 and sentence_count >= 2 else 0.0
    return keyword_hits * 1.0 + prose_bonus


def score_all_tags(text: str | None) -> dict[ContentTag, float]:
    """The raw per-class scores BEFORE `_MIN_CONFIDENT_SCORE` is applied — every real tag's
    score, always, even when none of them would clear the confidence floor. `tag_content`
    (below) is a thin wrapper over this: argmax, then threshold. Exposed as its own public
    function so a caller (an eval sweeping candidate confidence thresholds, e.g.) can simulate
    "what would this classify as at a DIFFERENT threshold" without duplicating the scoring
    logic — the threshold decision and the scoring itself are deliberately separable.

    `None`/empty/whitespace-only text scores every tag at 0.0 (never treated as evidence for
    any specific tag).
    """
    if not text or not text.strip():
        return {tag: 0.0 for tag in _SCORED_TAGS}

    text_lower = text.lower()
    word_count = len(text.split())
    return {
        ContentTag.RECEIPT: _score_receipt(text, text_lower, word_count),
        ContentTag.CODE: _score_code(text, text_lower, word_count),
        ContentTag.CHAT: _score_chat(text, text_lower, word_count),
        ContentTag.TRANSIENT_UI: _score_transient_ui(text, text_lower, word_count),
        ContentTag.DOCUMENT: _score_document(text, text_lower, word_count),
    }


def tag_content(text: str | None) -> TagResult:
    """`text` is typically `screenshot_ocr.extract_screenshot_text`'s output — `None` or
    empty/whitespace-only text is treated as `UNKNOWN` (no OCR content to reason about at
    all), never silently assumed to be any specific tag. Scores every candidate tag with a
    transparent, documented heuristic (see the `_score_*` functions above) and returns the
    highest-scoring one — UNKNOWN if nothing clears `_MIN_CONFIDENT_SCORE`.
    """
    if not text or not text.strip():
        return TagResult(tag=ContentTag.UNKNOWN, score=0.0)

    scores = score_all_tags(text)
    best_tag = max(scores, key=lambda tag: scores[tag])
    best_score = scores[best_tag]
    if best_score < _MIN_CONFIDENT_SCORE:
        return TagResult(tag=ContentTag.UNKNOWN, score=best_score)
    return TagResult(tag=best_tag, score=best_score)
