from __future__ import annotations

import ast
from pathlib import Path

# Audit finding E1: users were shown wrong "how much you'll get back" figures whenever a
# reclaim-labeled value (reclaimable_bytes / total_bytes / bytes_freed) was summed from
# `Candidate.size_bytes`/`item.size_bytes` directly instead of through the hardlink-aware path
# (`Candidate.reclaimable_bytes` / `_effective_reclaimable_bytes`) -- see `detectors.
# _reclaimable_bytes_for_candidate` (ADR-0006 extension) and `api.service._effective_
# reclaimable_bytes`. This is the same "structural, not conventional" AST-gate pattern
# `evals/test_ai_safety_gate.py` already uses for a different boundary (recommend-only AI
# output never reaching auto-delete): parse the real source, prove a structural property, prove
# the gate has teeth against an injected violation.
#
# Scope, deliberately narrow (three files only, matching where reclaim-purpose figures are
# actually surfaced to a caller/UI):
#   - src/reclaim/api/schemas.py  -- the response models themselves
#   - src/reclaim/api/service.py -- builds every schema instance the API returns
#   - src/reclaim/cli.py         -- the CLI's own report printing
#
# `src/reclaim/executor.py` is deliberately OUT of scope: `BatchApplyReport.bytes_freed` is a
# documented, deliberate design choice ("Sum of Candidate.size_bytes... across successfully-
# quarantined items -- a real measured value, not an estimate... Deliberately kept separate from
# [disk_free_delta_bytes]: the two can legitimately differ (hardlinks, filesystem block
# rounding) and conflating them would claim false precision" -- see `BatchApplyReport`'s own
# docstring) -- a POST-apply measured count of what was actually processed, not a PRE-apply
# estimate of what a user will get back. `service.py`/`cli.py` only ever forward that
# already-computed value (`report.bytes_freed`, `breakdown.bytes_freed`) -- never re-derive it
# from a bare `.size_bytes` themselves, which this gate proves below (zero violations found for
# the "bytes_freed" name even though it IS in the scanned field-name set).

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "reclaim"
_SCANNED_FILES: tuple[Path, ...] = (
    _SRC_ROOT / "api" / "schemas.py",
    _SRC_ROOT / "api" / "service.py",
    _SRC_ROOT / "cli.py",
)

# The exact field/keyword names this gate treats as "a reclaim-purpose figure presented to the
# user as what they'll get back" -- deliberately NOT the bare `size_bytes`/`bytes_total` names,
# which legitimately appear all over these same files for non-reclaim purposes (an item's own
# logical size shown next to its reclaim estimate, a treemap's total disk usage, a quarantine
# batch's current vault contents -- none of those are a reclaim promise, see the module docstring
# above and detectors.py/models.py for the exact-name audit backing this set).
_RECLAIM_LABELED_FIELD_NAMES = frozenset({"reclaimable_bytes", "total_bytes", "bytes_freed"})

# Any of these substrings appearing in an expression is proof the expression passed through the
# hardlink-aware path somewhere -- `_effective_reclaimable_bytes(...)` (service.py's own
# never-substitute-the-naive-total helper) or a direct `.reclaimable_bytes` reference (the
# None-aware ternary fallback pattern cli.py uses, e.g. `c.reclaimable_bytes if c.reclaimable_
# bytes is not None else c.size_bytes` -- semantically identical to `_effective_reclaimable_
# bytes`, just inlined).
_LINK_AWARE_MARKERS: tuple[str, ...] = ("_effective_reclaimable_bytes", ".reclaimable_bytes")


def _contains_link_aware_marker(expr_source: str) -> bool:
    return any(marker in expr_source for marker in _LINK_AWARE_MARKERS)


def _contains_bare_size_bytes(expr_source: str) -> bool:
    return ".size_bytes" in expr_source


def _naive_variable_names(tree: ast.AST) -> set[str]:
    """Every variable (module- or function-local; this gate doesn't need per-function scoping
    for the codebase it actually runs against -- see the docstring on
    `test_reclaim_labeled_fields_never_derive_bare_size_bytes` for why) assigned an expression
    that references `.size_bytes` WITHOUT also going through a link-aware marker anywhere in
    that same expression. These are exactly the "naive, not hardlink-aware" values a
    reclaim-labeled keyword must never be fed, whether directly inline or via this variable."""
    naive: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value_source = ast.unparse(node.value)
            if _contains_bare_size_bytes(value_source) and not _contains_link_aware_marker(
                value_source
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        naive.add(target.id)
    return naive


def _violations_in_source(source: str, *, label: str) -> list[str]:
    """Structural scan: every call-site keyword argument named one of
    `_RECLAIM_LABELED_FIELD_NAMES` must not be fed (directly, or via a tracked naive local
    variable) from an expression touching `.size_bytes` without a link-aware marker present."""
    tree = ast.parse(source, filename=label)
    naive_vars = _naive_variable_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg not in _RECLAIM_LABELED_FIELD_NAMES:
                continue
            value_source = ast.unparse(kw.value)
            direct_violation = _contains_bare_size_bytes(
                value_source
            ) and not _contains_link_aware_marker(value_source)
            indirect_violation = isinstance(kw.value, ast.Name) and kw.value.id in naive_vars
            if direct_violation or indirect_violation:
                violations.append(
                    f"{label}:{node.lineno}: keyword {kw.arg!r}= is fed from `.size_bytes` "
                    "without a link-aware helper (_effective_reclaimable_bytes / "
                    ".reclaimable_bytes)"
                )
    return violations


def _violations_in_file(path: Path) -> list[str]:
    return _violations_in_source(path.read_text(encoding="utf-8"), label=path.name)


# --- 1. The real gate: zero violations in the actual, current source --------------------------


def test_reclaim_labeled_fields_never_derive_bare_size_bytes() -> None:
    """The single most important assertion in this file: re-run on every CI pass against the
    real, current `schemas.py`/`service.py`/`cli.py` -- a future PR that adds a new reclaim-
    labeled field/keyword fed straight from `.size_bytes` (the exact shape of audit finding E1's
    `build_one_click_summary` bug, fixed alongside this gate) fails here immediately, before it
    can ship a wrong number to a user."""
    all_violations: list[str] = []
    for path in _SCANNED_FILES:
        assert path.exists(), f"expected to find {path}"
        all_violations.extend(_violations_in_file(path))
    assert all_violations == [], "\n".join(all_violations)


# --- 2. Teeth: an injected violation, on a scratch copy, must be caught ------------------------


def test_gate_catches_a_directly_inlined_violation() -> None:
    """The exact original bug, inlined at the call site with no intermediate variable."""
    source = (
        "def f(items):\n    return Response(total_bytes=sum(item.size_bytes for item in items))\n"
    )
    violations = _violations_in_source(source, label="scratch_inline.py")
    assert violations != [], "expected the inline .size_bytes violation to be caught"
    assert "total_bytes" in violations[0]


def test_gate_catches_the_real_pre_fix_one_click_summary_shape() -> None:
    """Regression fixture: this is `build_one_click_summary`'s real pre-fix shape (audit finding
    E1) -- a naive local variable computed from `.size_bytes`, then fed to a reclaim-labeled
    keyword one statement later. Proves the gate's variable-tracking half, not just the
    direct-inline half above."""
    source = (
        "def build_one_click_summary(items):\n"
        "    total_bytes = sum(item.size_bytes for item in items)\n"
        "    return OneClickGroupOut(total_bytes=total_bytes)\n"
    )
    violations = _violations_in_source(source, label="scratch_one_click.py")
    assert violations != [], "expected the naive-variable violation to be caught"


def test_gate_catches_a_bytes_freed_violation_too() -> None:
    """`bytes_freed` is in-scope for the field-name check too (task-named example pattern) --
    proven separately from `reclaimable_bytes`/`total_bytes` so a regression specific to that
    name doesn't silently slip through even though today's real source has zero such sites."""
    source = (
        "def f(items):\n    return ApplyResponse(bytes_freed=sum(i.size_bytes for i in items))\n"
    )
    violations = _violations_in_source(source, label="scratch_bytes_freed.py")
    assert violations != [], "expected the bytes_freed violation to be caught"


# --- 3. No false positives: the real, correct patterns this codebase uses must pass ------------


def test_gate_accepts_the_effective_reclaimable_bytes_helper_pattern() -> None:
    source = (
        "def f(items):\n"
        "    total_bytes = sum(_effective_reclaimable_bytes(item) for item in items)\n"
        "    return Response(total_bytes=total_bytes)\n"
    )
    assert _violations_in_source(source, label="scratch_helper.py") == []


def test_gate_accepts_the_reclaimable_bytes_none_aware_ternary_pattern() -> None:
    """cli.py's own real pattern (`_print_duplicate_reclaim_estimate`): `c.reclaimable_bytes if
    c.reclaimable_bytes is not None else c.size_bytes` -- semantically identical to
    `_effective_reclaimable_bytes`, inlined. Must never be flagged just because `.size_bytes`
    appears in the same expression as a legitimate None-fallback."""
    source = (
        "def f(items):\n"
        "    reclaimable_total = sum(\n"
        "        c.reclaimable_bytes if c.reclaimable_bytes is not None else c.size_bytes\n"
        "        for c in items\n"
        "    )\n"
        "    return Response(reclaimable_bytes=reclaimable_total)\n"
    )
    assert _violations_in_source(source, label="scratch_ternary.py") == []


def test_gate_accepts_non_reclaim_size_bytes_fields_unconditionally() -> None:
    """A bare `.size_bytes` feeding a field NOT in `_RECLAIM_LABELED_FIELD_NAMES` (e.g. the
    item's own logical `size_bytes`, or a `bytes_total` vault-contents figure like
    `QuarantineBatchOut.bytes_total`) is never a violation -- this gate only ever constrains
    fields presented as a reclaim promise, never every appearance of `.size_bytes` in the file."""
    source = (
        "def f(items):\n"
        "    return Response(\n"
        "        size_bytes=items[0].size_bytes,\n"
        "        bytes_total=sum(i.size_bytes for i in items),\n"
        "    )\n"
    )
    assert _violations_in_source(source, label="scratch_non_reclaim.py") == []


def test_gate_accepts_treemap_total_bytes_derived_from_subtree_size_bytes_query() -> None:
    """The real `TreemapResponse.total_bytes` shape (`index.subtree_size_bytes(root)`, a SQL
    aggregate call, never a `.size_bytes` attribute access) -- proves `total_bytes` isn't banned
    outright, only a `.size_bytes`-derived `total_bytes` without a link-aware marker."""
    source = (
        "def f(index, root):\n"
        "    total_bytes = index.subtree_size_bytes(root)\n"
        "    return TreemapResponse(total_bytes=total_bytes)\n"
    )
    assert _violations_in_source(source, label="scratch_treemap.py") == []
