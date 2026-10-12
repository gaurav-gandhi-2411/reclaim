from __future__ import annotations

import re
from pathlib import Path

# Regression (2026-10-09 install screenshots): `.rc-recovery-banner { display: flex }` and
# `.rc-badge { display: inline-flex }` beat the browser's `[hidden] { display: none }`, so an empty
# banner with a Dismiss button and an empty footer pill were visible on every page load. The
# dashboard hides things only through the `hidden` attribute, so one author rule must make it win.
# Verified in a real browser (before: both visible; after: neither); this keeps the rule from
# being dropped in a stylesheet edit.

_CSS = (
    Path(__file__).parent.parent / "src" / "reclaim" / "api" / "static" / "styles.css"
).read_text(encoding="utf-8")


def test_hidden_attribute_always_wins_over_class_display_rules() -> None:
    rule = re.search(r"(?m)^\[hidden\]\s*\{([^}]*)\}", _CSS)
    assert rule is not None, "global [hidden] rule missing from styles.css"
    assert re.search(r"display:\s*none\s*!important", rule.group(1))


def test_the_elements_that_showed_while_hidden_still_set_their_own_display() -> None:
    # If these ever stop setting `display`, the global rule above is no longer what protects them.
    assert re.search(r"\.rc-recovery-banner\s*\{[^}]*display:\s*flex", _CSS)
    assert re.search(r"\.rc-badge\s*\{[^}]*display:\s*inline-flex", _CSS)
