from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from ai_fixtures.build_document_realistic_tiers import build_all_chunks

# Feature 2's content-tag classifier fixtures. Per the dataset-sourcing decision (ADR-0019):
# no public dataset matches this exact "screenshot content type" taxonomy cleanly (SROIE
# receipts and RVL-CDIP documents were both considered and rejected — SROIE's canonical
# source is gated-registration-only and its GitHub mirror's MIT license likely covers only
# the mirror's own scripts, not the underlying receipt images; RVL-CDIP's Hugging Face card
# reports `"license": ["other"]`, tracing back to real tobacco-litigation business records —
# real, if debated, ambiguity in both cases). Two of five classes reuse already-vetted, real,
# zero-risk content instead: Project Gutenberg prose (public domain, already downloaded for
# Feature 1b) for DOCUMENT, and Reclaim's own source code for CODE. The remaining three
# classes (RECEIPT, CHAT, TRANSIENT_UI) have no plausible public dataset for this exact
# screenshot-specific taxonomy regardless of licensing — synthetic-but-structurally-realistic
# generation, fully disclosed as such (this is why the resulting operating point is PROVISIONAL,
# never MEASURED — see `assert_safe_to_promote_to_measured` in the eval itself).
#
# Every pool below also includes a deliberate minority of HARD cases (a document that mentions
# a dollar figure once, a chat message with a stray currency symbol, very short ambiguous UI
# text) — same "realistic, not just clean synthetic" discipline as ADR-0012/0015/0017's
# recompression/transform tiers, applied here to a classification task instead of a similarity
# task.

_SEED = 42
_GUTENBERG_ROOT = Path("data/ai_datasets/gutenberg_texts/cleaned")
_REPO_SRC_ROOT = Path("src/reclaim")
_DOCUMENT_SNIPPET_WORDS = 70  # a realistic "screenshot of a document" shows a paragraph or
# two, not a full page — truncating Gutenberg's 300+-word near-dup chunks down to a
# screenshot-sized snippet, not reusing them at their original (much larger) length.


@dataclass(frozen=True, slots=True)
class TaggedSample:
    sample_id: str
    true_tag: str  # a reclaim.ai.content_tagger.ContentTag value
    text: str


def document_texts() -> list[str]:
    """Real Gutenberg prose (public domain), truncated to a screenshot-realistic snippet
    length. Requires `evals/ai_fixtures/fetch_gutenberg_texts.py` to have been run — same
    precondition as `test_ai_document_templated_gold.py`."""
    chunks = build_all_chunks(_GUTENBERG_ROOT)
    texts: list[str] = []
    for chunk in chunks:
        words = chunk.text.split()
        texts.append(" ".join(words[:_DOCUMENT_SNIPPET_WORDS]))
    return texts


def code_texts() -> list[str]:
    """Real code: contiguous line windows sampled from Reclaim's own source tree — zero
    licensing risk (the project's own code), and genuinely representative of "a screenshot of
    an editor/terminal showing Python.\""""
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic fixture sampling
    py_files = sorted(_REPO_SRC_ROOT.rglob("*.py"))
    snippets: list[str] = []
    window = 14
    for py_file in py_files:
        lines = py_file.read_text(encoding="utf-8").splitlines()
        if len(lines) < window:
            continue
        start = rng.randrange(0, len(lines) - window + 1)
        snippet = "\n".join(lines[start : start + window]).strip()
        if snippet:
            snippets.append(snippet)
    return snippets


_RECEIPT_ITEMS = (
    ("Coffee", 4.50),
    ("Sandwich", 8.99),
    ("Bagel", 3.25),
    ("Bottled Water", 2.00),
    ("Salad", 7.50),
    ("Muffin", 3.75),
    ("Iced Tea", 3.00),
    ("Chips", 2.50),
    ("Yogurt Parfait", 4.25),
    ("Croissant", 3.50),
)
_STORE_NAMES = (
    "Corner Cafe",
    "Riverside Market",
    "Main Street Deli",
    "Oakview Grocery",
    "Sunrise Bakery",
    "Harbor Coffee Co.",
)


def receipt_texts(n: int = 30) -> list[str]:
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic fixture sampling
    texts: list[str] = []
    for _index in range(n):
        store = rng.choice(_STORE_NAMES)
        items = rng.sample(_RECEIPT_ITEMS, rng.randint(2, 4))
        subtotal = sum(price for _, price in items)
        tax = round(subtotal * 0.08, 2)
        total = round(subtotal + tax, 2)
        cash = round(total + rng.choice([0.0, 1.0, 5.0, 10.0]), 2)
        change = round(cash - total, 2)
        lines_text = "\n".join(f"{name:20s} ${price:>6.2f}" for name, price in items)
        texts.append(
            f"{store}\n"
            f"{lines_text}\n"
            f"Subtotal    ${subtotal:.2f}\n"
            f"Tax         ${tax:.2f}\n"
            f"Total       ${total:.2f}\n"
            f"Cash        ${cash:.2f}\n"
            f"Change Due  ${change:.2f}\n"
            "Thank You For Your Purchase"
        )
    # Hard cases: a very short receipt (register tape, few items) and a receipt with a typo'd
    # OCR-plausible line missing a keyword or two.
    texts.append("Qty 1  Item  Total $4.99\nCash $5.00\nChange $0.01")
    texts.append(f"{rng.choice(_STORE_NAMES)}\nTotal Due: $12.40\nBalance Due: $0.00")
    return texts


_CHAT_NAMES = ("Sam", "Alex", "Jordan", "Priya", "Chen", "Maya", "Leo", "Nina")
_CHAT_LINES = (
    "hey are you around?",
    "yeah whats up",
    "wanna grab lunch",
    "sounds good, see you at noon",
    "running a few minutes late",
    "no worries take your time",
    "did you see the game last night",
    "omg yes that was wild",
    "can you send me that file",
    "just sent it, check your email",
)


def chat_texts(n: int = 30) -> list[str]:
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic fixture sampling
    texts: list[str] = []
    for _index in range(n):
        contact = rng.choice(_CHAT_NAMES)
        hour = rng.randint(1, 11)
        minute = rng.randint(0, 59)
        lines = []
        for turn in range(rng.randint(3, 5)):
            speaker = "You" if turn % 2 == 0 else contact
            lines.append(f"{hour}:{minute:02d} {'AM' if hour < 12 else 'PM'}")
            lines.append(f"{speaker}: {rng.choice(_CHAT_LINES)}")
            minute = (minute + rng.randint(1, 5)) % 60
        lines.append("Delivered")
        texts.append("\n".join(lines))
    # Hard cases: a chat message that mentions code ("just push the fix") and one with a
    # currency amount ("can you venmo me $12 for lunch") — still genuinely chat by structure.
    texts.append("9:14 AM\nYou: can you push the fix to main?\n9:15 AM\nSam: on it\nRead 9:16 AM")
    texts.append("6:02 PM\nYou: can you venmo me $12 for lunch\n6:03 PM\nAlex: sent!\nDelivered")
    return texts


_TRANSIENT_UI_LINES = (
    "Loading...",
    "Please wait",
    "Connecting...",
    "Retry",
    "Try Again",
    "Tap to Continue",
    "No Internet Connection",
    "Reconnecting...",
    "Syncing...",
    "Please Wait While We Set Things Up",
)


def transient_ui_texts(n: int = 30) -> list[str]:
    rng = random.Random(_SEED)  # noqa: S311 -- deterministic fixture sampling
    texts: list[str] = []
    for _index in range(n):
        n_lines = rng.randint(1, 2)
        texts.append("\n".join(rng.sample(_TRANSIENT_UI_LINES, n_lines)))
    return texts


def unknown_texts() -> list[str]:
    """Genuinely ambiguous/no-ground-truth OCR noise — bare short strings with NO real
    transient-UI vocabulary (a stray "OK" button, a menu icon's mis-OCR'd label, a clock
    readout). NOT true transient-UI content, deliberately included as negatives for EVERY
    real tag so the eval measures whether sparseness alone still wrongly buys TRANSIENT_UI a
    confident classification — the exact false-positive class that tag must never produce
    (see tests/test_ai_content_tagger.py's dedicated regression test for the unit-level
    version of this same property)."""
    return ["OK", "...", "Menu", "9:41", "xk qz 42", "ok sure yeah", "•••", "Untitled"]


def build_all_tagged_samples() -> list[TaggedSample]:
    samples: list[TaggedSample] = []
    for index, text in enumerate(document_texts()):
        samples.append(TaggedSample(f"document_{index:04d}", "document", text))
    for index, text in enumerate(code_texts()):
        samples.append(TaggedSample(f"code_{index:04d}", "code", text))
    for index, text in enumerate(receipt_texts()):
        samples.append(TaggedSample(f"receipt_{index:04d}", "receipt", text))
    for index, text in enumerate(chat_texts()):
        samples.append(TaggedSample(f"chat_{index:04d}", "chat", text))
    for index, text in enumerate(transient_ui_texts()):
        samples.append(TaggedSample(f"transient_ui_{index:04d}", "transient_ui", text))
    for index, text in enumerate(unknown_texts()):
        samples.append(TaggedSample(f"unknown_{index:04d}", "unknown", text))
    return samples
