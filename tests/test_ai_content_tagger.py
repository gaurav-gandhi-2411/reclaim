from __future__ import annotations

from reclaim.ai.content_tagger import KEEP_BIASED_TAGS, ContentTag, tag_content


def test_tag_content_none_and_empty_and_whitespace_are_unknown() -> None:
    assert tag_content(None).tag == ContentTag.UNKNOWN
    assert tag_content("").tag == ContentTag.UNKNOWN
    assert tag_content("   \n  ").tag == ContentTag.UNKNOWN


def test_tag_content_recognizes_a_receipt() -> None:
    text = (
        "Thank You For Your Purchase\n"
        "Item          Qty   Price\n"
        "Coffee         2    $4.50\n"
        "Sandwich       1    $8.99\n"
        "Subtotal            $17.99\n"
        "Tax                 $1.44\n"
        "Total               $19.43\n"
        "Cash                $20.00\n"
        "Change Due          $0.57"
    )
    assert tag_content(text).tag == ContentTag.RECEIPT


def test_tag_content_recognizes_code() -> None:
    text = (
        "def compute_hash(path):\n"
        "    import hashlib\n"
        "    h = hashlib.blake3()\n"
        "    with open(path, 'rb') as f:\n"
        "        while True:\n"
        "            chunk = f.read(65536)\n"
        "            if not chunk:\n"
        "                return h.hexdigest()\n"
        "class Foo:\n"
        "    def __init__(self, x):\n"
        "        self.x = x"
    )
    assert tag_content(text).tag == ContentTag.CODE


def test_tag_content_recognizes_chat() -> None:
    text = (
        "9:41 AM\n"
        "You: hey are you around?\n"
        "9:42 AM\n"
        "Sam: yeah whats up\n"
        "9:42 AM\n"
        "You: wanna grab lunch\n"
        "Delivered\n"
        "Read 9:43 AM"
    )
    assert tag_content(text).tag == ContentTag.CHAT


def test_tag_content_recognizes_a_document() -> None:
    text = (
        "Chapter Three: The Long Winter\n"
        "In the years that followed the great migration, the settlers found themselves "
        "increasingly dependent on trade routes that stretched far beyond the valley. "
        "Introduction of new crops changed the economy in ways nobody anticipated. "
        "This chapter summarizes those changes and their long-term consequences for "
        "the region as a whole, drawing on archival records and oral histories."
    )
    assert tag_content(text).tag == ContentTag.DOCUMENT


def test_tag_content_recognizes_transient_ui() -> None:
    assert tag_content("Loading...\nPlease wait").tag == ContentTag.TRANSIENT_UI
    assert tag_content("Retry").tag == ContentTag.TRANSIENT_UI


def test_tag_content_sparse_gibberish_is_unknown_not_transient_ui() -> None:
    """Regression: short OCR noise with zero transient-UI vocabulary must never resolve to
    TRANSIENT_UI just because it's short -- TRANSIENT_UI is the one tag that's ever
    deletion-eligible, so sparseness alone (with no real keyword match) must fall back to
    UNKNOWN, never confidently claim the deletion-eligible tag."""
    assert tag_content("xk qz 42").tag == ContentTag.UNKNOWN
    assert tag_content("ok sure yeah").tag == ContentTag.UNKNOWN


def test_only_transient_ui_is_outside_the_keep_biased_set() -> None:
    """Structural proof of the spec's "bias STRONGLY toward keep for receipt/document/code"
    instruction: every tag except TRANSIENT_UI is keep-biased, including CHAT (may hold a
    meaningful conversation) and UNKNOWN (low classifier confidence -- exactly when caution
    matters most)."""
    assert {
        ContentTag.RECEIPT,
        ContentTag.DOCUMENT,
        ContentTag.CODE,
        ContentTag.CHAT,
        ContentTag.UNKNOWN,
    } == KEEP_BIASED_TAGS
    assert ContentTag.TRANSIENT_UI not in KEEP_BIASED_TAGS
