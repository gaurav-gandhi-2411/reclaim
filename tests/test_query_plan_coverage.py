from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import reclaim.index as index_module
from reclaim.index import ScanIndex
from reclaim.models import FileRecord

# Mechanical guard against the ESCAPE-clause-defeats-index bug recurring. It has already hit
# twice: `files_matching_path_pattern` (caught at design time, before it shipped) and
# `direct_children` (shipped, then measured on a real 3.1M-row disk at ~1.5s *per call* doing a
# full table scan — `detect_archive_pairs` called it once per archive file, turning a
# should-be-fast candidate-generation pass into a 20+ minute stall). Two independent checks:
#
# 1. `test_every_sql_issuing_method_is_classified` + `test_classified_methods_match_expectation`
#    — every `ScanIndex` method that executes SQL against `files` is discovered via
#    introspection (not hand-maintained), and must be explicitly classified below as either
#    "must hit an index" (the default expectation for a query meant to narrow a large table) or
#    a named, justified full-scan exception. A new method that issues SQL and isn't classified
#    fails the completeness test — there is no silent third option where it's just uncovered.
# 2. `test_no_unmarked_like_escape_in_query_layer` — a static grep over the query-layer source
#    for `LIKE ... ESCAPE`, which must never appear without an adjacent `LIKE-ESCAPE-OK:`
#    marker comment explaining why *that specific instance* doesn't have the index-defeating
#    cost (i.e., it's a residual filter over an already-narrowed row set, not a primary lookup).


def _plan_details(index: ScanIndex, action: Callable[[ScanIndex], object]) -> list[str]:
    """Captures the *actual* SQL a method issues (via `set_trace_callback`, so this can never
    drift from the implementation) and returns each `EXPLAIN QUERY PLAN` row's `detail` column
    — the field that actually says `SCAN ...` or `SEARCH ... USING INDEX ...`."""
    captured: list[str] = []
    index._conn.set_trace_callback(captured.append)
    try:
        action(index)
    finally:
        index._conn.set_trace_callback(None)
    assert captured, "action executed no SQL to capture"
    sql = captured[-1]
    plan_rows = index._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return [row["detail"] for row in plan_rows]


def _has_bare_scan(details: list[str]) -> bool:
    """True if any plan row is a bare scan of the real `files` table (`SCAN files` with no
    `USING INDEX`/`USING COVERING INDEX` qualifier) — the actual dangerous pattern (a full read
    of every row). Two things that look similar but aren't this bug:
    - `SCAN ... USING COVERING INDEX` is index-assisted and fine (e.g. `has_any_records`'s
      `EXISTS` check visits index entries, not table rows, and stops at the first match).
    - `SCAN (subquery-N)`/`SCAN (co-routine)` scans an already-computed, typically small
      intermediate result (e.g. a `GROUP BY` aggregate), not the 3.1M-row base table — the
      `(...)` form is SQLite's own way of naming that it's not a real table.
    """
    return any(
        detail.startswith("SCAN") and "USING" not in detail and not detail.startswith("SCAN (")
        for detail in details
    )


@dataclass(frozen=True)
class Case:
    """One `ScanIndex` method's classification. `action=None` means the method doesn't produce
    a meaningful `EXPLAIN QUERY PLAN` result at all (schema DDL, or an `INSERT` whose plan has
    no SEARCH/SCAN decision to inspect) — still must be listed, with a reason, so the
    completeness check covers it explicitly rather than by omission."""

    action: Callable[[ScanIndex], object] | None
    expect_index: bool
    reason: str


def _consume(action: Callable[[ScanIndex], Iterator[object]]) -> Callable[[ScanIndex], object]:
    """Wraps a generator-returning action so its SQL actually executes (generators are lazy)."""
    return lambda index: list(action(index))


_SCOPE = Path("C:/Data/dir1")

# Every `ScanIndex` method whose source references `self._conn.execute` must have exactly one
# entry here — see `test_every_sql_issuing_method_is_classified`.
_CASES: dict[str, Case] = {
    "get_record": Case(
        lambda idx: idx.get_record(Path("C:/Data/dir1/file1.bin")),
        expect_index=True,
        reason="primary-key point lookup",
    ),
    "record_exists": Case(
        lambda idx: idx.record_exists(Path("C:/Data/dir1/file1.bin")),
        expect_index=True,
        reason="primary-key point lookup",
    ),
    "files_by_name": Case(
        _consume(lambda idx: idx.files_by_name(["file1.bin"])),
        expect_index=True,
        reason="indexed via the `name` column",
    ),
    "files_by_ext": Case(
        _consume(lambda idx: idx.files_by_ext([".bin"])),
        expect_index=True,
        reason="indexed via the `ext` column",
    ),
    "files_larger_than": Case(
        _consume(lambda idx: idx.files_larger_than(500)),
        expect_index=True,
        reason="indexed via the `size` column",
    ),
    "files_matching_path_pattern": Case(
        _consume(lambda idx: idx.files_matching_path_pattern("C:/Data/dir1/*")),
        expect_index=True,
        reason="indexed via `path_lower COLLATE NOCASE` (no ESCAPE clause, by design)",
    ),
    "duplicate_size_candidates": Case(
        _consume(lambda idx: idx.duplicate_size_candidates(min_reclaim_bytes=0)),
        expect_index=True,
        reason="indexed via the `size` column (GROUP BY/IN)",
    ),
    "duplicate_size_candidate_count": Case(
        lambda idx: idx.duplicate_size_candidate_count(min_reclaim_bytes=0),
        expect_index=True,
        reason="same query shape as duplicate_size_candidates",
    ),
    "immaterial_duplicate_bucket_stats": Case(
        lambda idx: idx.immaterial_duplicate_bucket_stats(min_reclaim_bytes=0),
        expect_index=True,
        reason="same query shape as duplicate_size_candidates",
    ),
    "subtree_size_bytes": Case(
        lambda idx: idx.subtree_size_bytes(_SCOPE),
        expect_index=True,
        reason="prefix-range scoped via _prefix_range",
    ),
    "direct_children": Case(
        lambda idx: idx.direct_children(_SCOPE),
        expect_index=True,
        reason="prefix-range scoped via _prefix_range",
    ),
    "load_stat_cache": Case(
        lambda idx: idx.load_stat_cache(_SCOPE),
        expect_index=True,
        reason="prefix-range scoped via _prefix_range (root= given)",
    ),
    "load_hash_cache": Case(
        lambda idx: idx.load_hash_cache(_SCOPE),
        expect_index=True,
        reason="prefix-range scoped via _prefix_range (root= given)",
    ),
    "_query_inventory": Case(
        lambda idx: idx.full_inventory(under=_SCOPE),
        expect_index=True,
        reason="prefix-range scoped via _prefix_range (under= given); exercised here via the "
        "full_inventory(under=...) delegate since _query_inventory itself is private",
    ),
    "prune_missing": Case(
        lambda idx: idx.prune_missing(["C:/Data/dir1/file1.bin"], seen_paths=[]),
        expect_index=True,
        reason="DELETE ... WHERE path = ? — primary-key point delete",
    ),
    "store_partial_hashes": Case(
        lambda idx: idx.store_partial_hashes(
            [(Path("C:/Data/dir1/file1.bin"), 100, 1.0, "digest")]
        ),
        expect_index=True,
        reason="UPDATE ... WHERE path = ? — primary-key point update",
    ),
    "store_full_hashes": Case(
        lambda idx: idx.store_full_hashes([(Path("C:/Data/dir1/file1.bin"), 100, 1.0, "digest")]),
        expect_index=True,
        reason="UPDATE ... WHERE path = ? — primary-key point update",
    ),
    # --- Documented full-scan exceptions: no index could help, or the "SCAN" is short-circuited
    # before it matters. Each of these is proven, not just claimed — see
    # `test_documented_scan_exceptions_are_still_accurate` below.
    "has_any_records": Case(
        lambda idx: idx.has_any_records(),
        expect_index=False,
        reason="EXISTS(SELECT 1 FROM files) short-circuits on the first row regardless of scan "
        "vs search — there's no WHERE clause to narrow, by design (it's an emptiness check)",
    ),
    "full_inventory": Case(
        lambda idx: idx.full_inventory(),
        expect_index=False,
        reason="under=None means 'read the whole table' by design (dashboard total-usage view) "
        "— no index narrows a query that intentionally wants every row",
    ),
    "candidate_inventory": Case(
        lambda idx: idx.candidate_inventory(),
        expect_index=True,
        reason="under=None still filters `is_cloud_placeholder = 0` — indexed via "
        "idx_files_is_cloud_placeholder, even with no path prefix scope",
    ),
    "_backfill_name_and_path_lower": Case(
        lambda idx: idx._backfill_name_and_path_lower(),
        expect_index=True,
        reason="`WHERE name IS NULL OR path_lower IS NULL` — verified SQLite plans this as a "
        "MULTI-INDEX OR using idx_files_name/idx_files_path_lower (NULLs are indexable B-tree "
        "entries too), not a bare scan",
    ),
    # --- Not applicable: schema DDL / no meaningful SEARCH-vs-SCAN plan to inspect.
    "upsert_records": Case(
        action=None,
        expect_index=False,
        reason="INSERT ... ON CONFLICT — EXPLAIN QUERY PLAN produces no plan rows for this "
        "statement shape (verified empirically); conflict resolution is index-based internally "
        "but there's no SEARCH/SCAN decision to assert on",
    ),
    "_ensure_name_and_path_lower_columns": Case(
        action=None,
        expect_index=False,
        reason="PRAGMA table_info + ALTER TABLE ADD COLUMN — schema introspection/DDL, not a "
        "data query; no SEARCH/SCAN plan applies",
    ),
}


@pytest.fixture
def indexed_bulk(tmp_path: Path) -> Iterator[ScanIndex]:
    """A representative-size synthetic index (thousands of rows, real directory nesting) so
    SQLite's planner has a genuine reason to prefer an index over a scan — on a near-empty
    table the planner may reasonably pick either, which would make this check meaningless."""
    idx = ScanIndex(tmp_path / "bulk.sqlite3")
    records = [
        FileRecord(
            path=Path(f"C:/Data/dir{i % 50}/file{i}.bin"),
            is_dir=False,
            size_bytes=100 if i % 37 == 0 else i + 1000,
            attributes=0,
            ext=".bin",
            git_repo_root=None,
            git_repo_clean=False,
            mtime=1000.0,
            ctime=1000.0,
        )
        for i in range(5000)
    ]
    idx.upsert_records(records, scanned_at=1000.0)
    yield idx
    idx.close()


def _discover_sql_issuing_methods() -> set[str]:
    """Every method on `ScanIndex` whose source references `self._conn.execute` — found via
    introspection, not a hand-maintained list, so a new query method is covered by default
    (making it *fail* `test_every_sql_issuing_method_is_classified` until someone adds a `Case`
    for it, rather than silently shipping with no index-usage check at all)."""
    discovered: set[str] = set()
    for name, member in inspect.getmembers(ScanIndex, predicate=inspect.isfunction):
        if name in ("__init__",):
            continue  # schema DDL only (CREATE TABLE/INDEX) — no query to classify
        try:
            source = inspect.getsource(member)
        except (OSError, TypeError):
            continue
        if "self._conn.execute" in source:
            discovered.add(name)
    return discovered


def test_every_sql_issuing_method_is_classified() -> None:
    discovered = _discover_sql_issuing_methods()
    classified = set(_CASES.keys())
    missing = discovered - classified
    assert not missing, (
        f"{missing} execute SQL but have no Case in _CASES — classify each as expect_index=True "
        "(the default expectation) or a named, justified exception before this test can pass"
    )
    # A classified name not found by the `self._conn.execute`-in-source heuristic is fine as
    # long as it's still a real method (e.g. `full_inventory`/`candidate_inventory` are thin
    # delegates to `_query_inventory` and deliberately given their own Case too, for direct
    # public-API coverage) — only a name that doesn't exist at all indicates a stale/typo'd
    # entry left over from a rename or removal.
    unknown = {name for name in classified if not hasattr(ScanIndex, name)}
    assert not unknown, f"{unknown} are classified but don't exist on ScanIndex — fix or remove"


@pytest.mark.parametrize("name", sorted(_CASES.keys()))
def test_classified_methods_match_expectation(name: str, indexed_bulk: ScanIndex) -> None:
    case = _CASES[name]
    if case.action is None:
        pytest.skip(f"{name}: {case.reason}")
    details = _plan_details(indexed_bulk, case.action)
    bare_scan = _has_bare_scan(details)
    if case.expect_index:
        assert not bare_scan, (
            f"{name} was expected to hit an index ({case.reason}) but its plan shows a bare "
            f"scan: {details}"
        )
    else:
        # Prove the documented exception is still accurate, not a stale leftover from a
        # previous design that's since been fixed and should now be promoted to expect_index=True.
        assert bare_scan, (
            f"{name} is documented as a full-scan exception ({case.reason}) but its plan no "
            f"longer shows one: {details} — if this was fixed, flip it to expect_index=True"
        )


# --- Static guard: no new LIKE ... ESCAPE without an explicit, reviewed marker ----------------

_QUERY_LAYER_FILES = (Path(index_module.__file__),)
# Matches only *executable* SQL text (a parameterized `LIKE ? ESCAPE ...`, this codebase's only
# real SQL shape for it), not prose/docstrings that merely discuss "LIKE ... ESCAPE" or show an
# illustrative literal pattern like `LIKE 'prefix/%' ESCAPE '\'` — those aren't executed SQL and
# would otherwise false-positive on this file's own docstrings explaining the bug.
_LIKE_ESCAPE_PATTERN = re.compile(r"LIKE\s*\?\s*ESCAPE", re.IGNORECASE)
_ALLOW_MARKER = "LIKE-ESCAPE-OK:"


def test_no_unmarked_like_escape_in_query_layer() -> None:
    """`LIKE ... ESCAPE` unconditionally defeats SQLite's index-range-scan optimization — this
    has already caused two real stalls (`files_matching_path_pattern`'s design avoided it;
    `direct_children`'s didn't, and cost ~1.5s per call on a 3.1M-row real index). Any new
    occurrence must carry a `LIKE-ESCAPE-OK:` comment on the line immediately before it,
    explaining why *that* instance is safe (e.g. a residual filter over an already-narrowed
    row set) — otherwise this test fails and points at the indexed range-query pattern instead
    (`path >= lo AND path < hi`, see `_prefix_range`).
    """
    violations: list[str] = []
    for path in _QUERY_LAYER_FILES:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not _LIKE_ESCAPE_PATTERN.search(line):
                continue
            preceding = lines[max(0, i - 10) : i]
            if any(_ALLOW_MARKER in prior_line for prior_line in preceding):
                continue
            violations.append(f"{path}:{i + 1}: {line.strip()}")
    assert not violations, (
        "LIKE ... ESCAPE found without a preceding 'LIKE-ESCAPE-OK:' marker comment — this "
        "unconditionally defeats SQLite's index-range-scan optimization (measured: ~1.5s/call "
        "full scan on a 3.1M-row real index). Use the indexed range-query pattern instead "
        "(path >= lo AND path < hi, see _prefix_range), or add the marker with a justification "
        "if this specific instance is a residual filter over an already-narrowed row set:\n"
        + "\n".join(violations)
    )
