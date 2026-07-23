from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import structlog

from reclaim.config import Config
from reclaim.index import ScanIndex
from reclaim.models import (
    REBUILDABLE_CATEGORY_GROUPS,
    Candidate,
    Mode,
    RawCandidate,
    Tier,
    Verdict,
)
from reclaim.safety import SafetyValidator

logger = structlog.get_logger(__name__)

_SECONDS_PER_DAY = 86400.0

# Config `[categories]` group ids — the coarse identifier every `RawCandidate.category_group`
# must be one of, matched against `_CATEGORY_GROUP_ENABLED_GETTERS` in `_category_enabled`.
_GROUP_DEV_ARTIFACTS = "dev_artifacts"
_GROUP_PACKAGE_CACHES = "package_caches"
_GROUP_MODEL_CACHES = "model_caches"
_GROUP_TEMP_AND_BROWSER_CACHES = "temp_and_browser_caches"
_GROUP_CRASH_DUMPS = "crash_dumps"
_GROUP_OLD_INSTALLERS = "old_installers"
_GROUP_ARCHIVE_PAIRS = "archive_pairs"
_GROUP_LARGE_LOGS = "large_logs"

# --- SQL-pushdown note ----------------------------------------------------------------------
#
# Every detector below queries `ScanIndex` directly through a narrow, indexed method
# (`files_by_name`/`files_by_ext`/`files_larger_than`/`files_matching_path_pattern`/
# `direct_children`/`record_exists`) instead of iterating an in-memory copy of the whole
# inventory. This replaced an earlier `InventoryContext`/`build_inventory_context()` design
# that loaded every row into a `{path: FileRecord}` dict up front — correct, but it meant a
# 3.1M-file real-disk index cost ~5GB of RAM and 20+ minutes just to materialize Python objects
# before a single detector ran. No detector here may call `ScanIndex.candidate_inventory()`
# (or any other whole-table load) again — see that method's docstring in `index.py`.
#
# A few checks genuinely cannot be pushed into a SQL `WHERE` clause without losing correctness
# (see `detect_archive_pairs`'s fuzzy sibling-name match, and the "downloads"/"log" substring
# checks in `detect_old_installers`/`detect_large_logs`) — those still run in Python, but only
# over the small, already-indexed-narrowed candidate set each detector's SQL query returns, not
# over the whole table. ADR-0002 documents this trade-off and why it's the honest option.


def _has_path_segment(path: Path, segment: str) -> bool:
    segment_lower = segment.lower()
    return any(part.lower() == segment_lower for part in path.parts)


# --- Dev artifacts -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DevArtifactSpec:
    dir_name: str
    category: str
    manifest_names: tuple[str, ...]
    artifact_label: str
    rebuild_instruction: str


_DEV_ARTIFACT_SPECS: tuple[_DevArtifactSpec, ...] = (
    _DevArtifactSpec(
        "node_modules",
        "dev_artifact_node_modules",
        ("package.json",),
        "Node.js dependency cache",
        "npm ci (or npm install)",
    ),
    _DevArtifactSpec(
        ".venv",
        "dev_artifact_python_venv",
        ("pyproject.toml", "requirements.txt", "setup.py"),
        "Python virtual environment",
        "python -m venv .venv && pip install -r requirements.txt (or pip install -e .)",
    ),
    _DevArtifactSpec(
        "venv",
        "dev_artifact_python_venv",
        ("pyproject.toml", "requirements.txt", "setup.py"),
        "Python virtual environment",
        "python -m venv venv && pip install -r requirements.txt (or pip install -e .)",
    ),
    _DevArtifactSpec(
        "target",
        "dev_artifact_rust_or_maven_target",
        ("Cargo.toml", "pom.xml"),
        "Rust/Maven build output directory",
        "cargo build (Rust) or mvn package (Maven)",
    ),
    _DevArtifactSpec(
        "build",
        "dev_artifact_build_output",
        ("package.json", "pyproject.toml", "setup.py"),
        "Build output directory",
        "npm run build / python -m build (rebuild from source)",
    ),
    _DevArtifactSpec(
        "dist",
        "dev_artifact_dist_output",
        ("package.json", "pyproject.toml", "setup.py"),
        "Distribution output directory",
        "npm run build / python -m build (rebuild from source)",
    ),
    _DevArtifactSpec(
        ".next",
        "dev_artifact_nextjs_build",
        ("package.json",),
        "Next.js build cache",
        "npm run build (rebuilds the Next.js production output)",
    ),
    _DevArtifactSpec(
        ".gradle",
        "dev_artifact_gradle_project_cache",
        ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
        "Project-local Gradle cache",
        "gradle build --refresh-dependencies",
    ),
)
_DEV_ARTIFACT_SPECS_BY_NAME: dict[str, _DevArtifactSpec] = {
    spec.dir_name.lower(): spec for spec in _DEV_ARTIFACT_SPECS
}
_PYCACHE_NAME = "__pycache__"
# The full set of directory basenames `detect_dev_artifacts` cares about — passed to
# `files_by_name` as a single indexed IN (...) query rather than one query per name.
_DEV_ARTIFACT_NAMES: tuple[str, ...] = (
    *_DEV_ARTIFACT_SPECS_BY_NAME.keys(),
    _PYCACHE_NAME,
)


def detect_dev_artifacts(index: ScanIndex) -> list[RawCandidate]:
    """Dev-artifact directories, gated on manifest adjacency: a rebuildable-cache directory is
    only ever proposed when a manifest proving rebuildability sits in its parent directory —
    no manifest adjacent means the path is never proposed, not even as a Tier B candidate
    (spec invariant, absolute). `__pycache__` is the one exception: unconditionally
    regeneratable bytecode, so it needs no manifest check at all.

    SQL-pushdown: `files_by_name` narrows the whole index down to just directories named one of
    the fixed dev-artifact names via the indexed `name` column — millions of unrelated rows are
    never touched. The manifest-adjacency check is one indexed point lookup
    (`record_exists`) per candidate, not a full-table scan.
    """
    candidates: list[RawCandidate] = []
    for record in index.files_by_name(_DEV_ARTIFACT_NAMES, is_dir=True):
        name_lower = record.path.name.lower()
        if name_lower == _PYCACHE_NAME:
            candidates.append(
                RawCandidate(
                    path=record.path,
                    is_dir=True,
                    category="dev_artifact_pycache",
                    category_group=_GROUP_DEV_ARTIFACTS,
                    suggested_tier=Tier.A,
                    rationale=(
                        "Python bytecode cache — unconditionally regeneratable, recreated "
                        "automatically the next time the module is imported."
                    ),
                    rebuild_instruction="Regenerates automatically on next Python import.",
                )
            )
            continue
        spec = _DEV_ARTIFACT_SPECS_BY_NAME.get(name_lower)
        if spec is None:
            continue  # defensive: files_by_name only returns names we asked for
        if not any(
            index.record_exists(record.path.parent / manifest_name)
            for manifest_name in spec.manifest_names
        ):
            continue  # no manifest adjacent: never proposed, not even Tier B
        candidates.append(
            RawCandidate(
                path=record.path,
                is_dir=True,
                category=spec.category,
                category_group=_GROUP_DEV_ARTIFACTS,
                suggested_tier=Tier.A,
                rationale=(
                    f"{spec.artifact_label}, rebuildable from "
                    f"{' or '.join(spec.manifest_names)} in the parent directory."
                ),
                rebuild_instruction=spec.rebuild_instruction,
            )
        )
    return candidates


# --- Package/model caches -----------------------------------------------------------------


def detect_package_caches(index: ScanIndex, cache_paths: Sequence[str]) -> list[RawCandidate]:
    """Global package download caches (pip, npm, uv, conda pkgs, .m2/.gradle) — no manifest-
    adjacency check, matched purely by configured path patterns
    (`config.categories.package_caches.paths`, defaulting to the real Windows locations).

    Model-weight caches (HuggingFace hub, torch hub, Ollama) are NOT detected here — see
    `detect_model_caches` / ADR-0003: their recovery cost (bandwidth, time, and sometimes
    unrecoverable gated/private/fine-tuned models) is not the same as a package manager's
    re-download, so they get their own category, tier ceiling, and retention default.

    SQL-pushdown: each configured pattern is one `files_matching_path_pattern` query against
    the indexed `path_lower` column, not a Python-side `fnmatch` over every row.
    """
    candidates: list[RawCandidate] = []
    seen: set[Path] = set()
    for pattern in cache_paths:
        for record in index.files_matching_path_pattern(pattern, is_dir=True):
            if record.path in seen:
                continue
            seen.add(record.path)
            candidates.append(
                RawCandidate(
                    path=record.path,
                    is_dir=True,
                    category="package_cache",
                    category_group=_GROUP_PACKAGE_CACHES,
                    suggested_tier=Tier.A,
                    rationale=(
                        "Package download cache — the owning tool re-downloads packages into "
                        "this directory automatically on next use."
                    ),
                    rebuild_instruction=(
                        "Re-run the package manager; the cache repopulates automatically."
                    ),
                )
            )
    return candidates


# --- Model-weight caches (ADR-0003) ----------------------------------------------------------

_MODEL_CACHE_RATIONALE = (
    "Model-weight cache — large ML checkpoints that redownload deterministically for public "
    "models, but at real cost: bandwidth, time, and — for gated, private, fine-tuned, or "
    "manually-pushed models — authentication that may no longer be available."
)
_MODEL_CACHE_REBUILD_INSTRUCTION = (
    "Re-run the model download (huggingface-cli / torch.hub / ollama pull); the cache "
    "repopulates automatically for public models only."
)
_MODEL_CACHE_RECOVERY_COST_NOTE = (
    "Recovery cost scales with model size (commonly several GB, sometimes 100+ GB) and requires "
    "network access; gated, private, fine-tuned, or manually-pushed models may be permanently "
    "unrecoverable if the original access or credentials are gone."
)


def detect_model_caches(
    index: ScanIndex, cache_paths: Sequence[str], model_extensions: Sequence[str]
) -> list[RawCandidate]:
    """Model-weight caches (HuggingFace hub, torch hub, Ollama models, ...) — split out from
    `package_caches` (ADR-0003): "rebuildable" was being decided by path type, not real rebuild
    cost, and a 100+GB HuggingFace hub redownload (or an unrecoverable gated/fine-tuned/
    manually-pushed model) is not the same risk as `npm ci` repopulating a cache.

    Always proposed at `suggested_tier=Tier.B` (review-queue) — model caches are never
    Tier-A/auto-quarantine-eligible, regardless of `config.categories.model_caches.enabled`; a
    human reviews every one before it's ever quarantined.

    Two independent detection surfaces, both scoped to `cache_paths` roots only (never a
    disk-wide sweep): whole matching cache-root directories (same pattern as
    `detect_package_caches`), and individual model-weight files by extension — defense in depth
    for a cache layout the directory-level match alone might not fully cover.
    """
    candidates: list[RawCandidate] = []
    seen: set[Path] = set()

    for pattern in cache_paths:
        for record in index.files_matching_path_pattern(pattern, is_dir=True):
            if record.path in seen:
                continue
            seen.add(record.path)
            candidates.append(
                RawCandidate(
                    path=record.path,
                    is_dir=True,
                    category="model_cache",
                    category_group=_GROUP_MODEL_CACHES,
                    suggested_tier=Tier.B,
                    rationale=_MODEL_CACHE_RATIONALE,
                    rebuild_instruction=_MODEL_CACHE_REBUILD_INSTRUCTION,
                    recovery_cost_note=_MODEL_CACHE_RECOVERY_COST_NOTE,
                )
            )

    for root in cache_paths:
        for ext in model_extensions:
            for record in index.files_matching_path_pattern(f"{root}/*{ext}", is_dir=False):
                if record.path in seen:
                    continue
                seen.add(record.path)
                candidates.append(
                    RawCandidate(
                        path=record.path,
                        is_dir=False,
                        category="model_cache",
                        category_group=_GROUP_MODEL_CACHES,
                        suggested_tier=Tier.B,
                        rationale=_MODEL_CACHE_RATIONALE,
                        rebuild_instruction=_MODEL_CACHE_REBUILD_INSTRUCTION,
                        recovery_cost_note=_MODEL_CACHE_RECOVERY_COST_NOTE,
                    )
                )
    return candidates


# --- Browser/temp/thumbnail caches ----------------------------------------------------------


def detect_temp_and_browser_caches(
    index: ScanIndex, cache_paths: Sequence[str], temp_roots: Sequence[str]
) -> list[RawCandidate]:
    """Browser cache directories and the Explorer thumbnail cache are proposed whole (matched
    by pattern); `%TEMP%`/`C:\\Windows\\Temp` are never proposed as a whole directory — only
    their direct children are, since some processes expect the root directory itself to keep
    existing between runs.

    The thumbnail cache is a `.db` file, which `SafetyValidator` blocks by default as a
    database extension — that's intentional and correct (spec: let the validator's verdict be
    authoritative here, the detector doesn't special-case it away).
    """
    candidates: list[RawCandidate] = []
    seen: set[Path] = set()
    for pattern in cache_paths:
        for record in index.files_matching_path_pattern(pattern):
            if record.path in seen:
                continue
            seen.add(record.path)
            is_thumbnail = record.path.name.lower().startswith("thumbcache_")
            candidates.append(
                RawCandidate(
                    path=record.path,
                    is_dir=record.is_dir,
                    category="thumbnail_cache" if is_thumbnail else "browser_cache",
                    category_group=_GROUP_TEMP_AND_BROWSER_CACHES,
                    suggested_tier=Tier.A,
                    rationale=(
                        "Windows Explorer thumbnail cache — regenerated automatically as "
                        "folders are browsed."
                        if is_thumbnail
                        else "Browser cache directory — regenerated automatically by the browser."
                    ),
                    rebuild_instruction="Regenerates automatically on next use; no manual step.",
                )
            )

    seen_roots: set[Path] = set()
    for pattern in temp_roots:
        for root_record in index.files_matching_path_pattern(pattern, is_dir=True):
            if root_record.path in seen_roots:
                continue
            seen_roots.add(root_record.path)
            for child in index.direct_children(root_record.path):
                candidates.append(
                    RawCandidate(
                        path=child.path,
                        is_dir=child.is_dir,
                        category="windows_temp",
                        category_group=_GROUP_TEMP_AND_BROWSER_CACHES,
                        suggested_tier=Tier.A,
                        rationale=(
                            f"Item in the Windows temp directory ('{root_record.path}') — temp "
                            "files are transient by design and safe to remove."
                        ),
                        rebuild_instruction=None,
                    )
                )
    return candidates


# --- Crash dumps ---------------------------------------------------------------------------

_CRASH_DUMP_EXT = (".dmp",)


def detect_crash_dumps(index: ScanIndex, root_paths: Sequence[str]) -> list[RawCandidate]:
    """`.dmp` files anywhere in the inventory, plus every direct child of the configured
    CrashDumps/WER report root directories (never the root directories themselves)."""
    candidates: list[RawCandidate] = []
    for record in index.files_by_ext(_CRASH_DUMP_EXT, is_dir=False):
        candidates.append(
            RawCandidate(
                path=record.path,
                is_dir=False,
                category="crash_dump_file",
                category_group=_GROUP_CRASH_DUMPS,
                suggested_tier=Tier.A,
                rationale=(
                    "Crash/memory dump (.dmp) — a diagnostic artifact from a past crash, not "
                    "needed for normal operation."
                ),
                rebuild_instruction=None,
            )
        )

    seen_roots: set[Path] = set()
    for pattern in root_paths:
        for root_record in index.files_matching_path_pattern(pattern, is_dir=True):
            if root_record.path in seen_roots:
                continue
            seen_roots.add(root_record.path)
            for child in index.direct_children(root_record.path):
                candidates.append(
                    RawCandidate(
                        path=child.path,
                        is_dir=child.is_dir,
                        category="crash_dump_wer_report",
                        category_group=_GROUP_CRASH_DUMPS,
                        suggested_tier=Tier.A,
                        rationale=(
                            "Windows Error Reporting artifact under "
                            f"'{root_record.path}' — diagnostic data from a past crash, "
                            "regenerated automatically as needed."
                        ),
                        rebuild_instruction=None,
                    )
                )
    return candidates


# --- Old installers --------------------------------------------------------------------------

_INSTALLER_EXTENSIONS = (".exe", ".msi", ".iso")


def detect_old_installers(index: ScanIndex, *, max_age_days: int, now: float) -> list[RawCandidate]:
    """Installer files (`.exe`/`.msi`/`.iso`) under a `Downloads` directory older than
    `max_age_days` (by mtime). Every detector in this module suggests Tier A uniformly —
    whether an old-installer candidate actually reaches Tier A or degrades to Tier B (spec:
    review-queue by default) is decided once, centrally, in `generate_candidates`, based on
    `config.categories.old_installers.enabled`.

    SQL-pushdown: `files_by_ext` narrows to installer-extension files via the indexed `ext`
    column first; the "is it under a Downloads directory" segment check and the age threshold
    only ever run against that already-small subset, never the whole table.
    """
    threshold_seconds = max_age_days * _SECONDS_PER_DAY
    candidates: list[RawCandidate] = []
    for record in index.files_by_ext(_INSTALLER_EXTENSIONS, is_dir=False):
        if not _has_path_segment(record.path, "downloads"):
            continue
        age_seconds = now - record.mtime
        if age_seconds < threshold_seconds:
            continue
        age_days = int(age_seconds // _SECONDS_PER_DAY)
        candidates.append(
            RawCandidate(
                path=record.path,
                is_dir=False,
                category="old_installer",
                category_group=_GROUP_OLD_INSTALLERS,
                suggested_tier=Tier.A,
                rationale=(
                    f"Installer ({record.ext}) in Downloads, last modified {age_days} days ago "
                    f"(older than the {max_age_days}-day threshold) — likely already run; "
                    "re-download from the vendor if needed again."
                ),
                rebuild_instruction="Re-download from the original source if needed again.",
            )
        )
    return candidates


# --- Extracted-archive pairs -----------------------------------------------------------------

# Longest-suffix-first so ".tar.gz" matches before the plain ".gz"/".tar" would.
_ARCHIVE_SUFFIXES: tuple[str, ...] = (".tar.gz", ".zip", ".rar", ".7z", ".tar")
_ARCHIVE_OVERLAP_THRESHOLD = 0.90
# `ScanIndex.ext` stores `Path.suffix` — the *last* suffix component only, so a "backup.tar.gz"
# file is stored with ext=".gz", not ".tar.gz". This prefilter set includes ".gz" for exactly
# that reason; `_archive_stem` below still requires the full ".tar.gz" suffix before treating a
# ".gz" file as an archive-pair candidate, so a bare (non-tar) ".gz" file is correctly rejected
# after the indexed narrowing, not before it.
_ARCHIVE_EXTS_FOR_PREFILTER: tuple[str, ...] = (".gz", ".zip", ".rar", ".7z", ".tar")


def _archive_stem(name: str) -> str | None:
    lower = name.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return None


def detect_archive_pairs(index: ScanIndex) -> list[RawCandidate]:
    """An archive file with a sibling directory whose name has >=90% overlap with the
    archive's stem (`difflib.SequenceMatcher` ratio, case-insensitive) proposes deleting
    *only* the archive — the extracted directory (and everything inside it) is never proposed
    by this detector, since it's explicitly the copy being kept.

    SQL-pushdown: `files_by_ext` narrows to archive-extension files first (indexed); the fuzzy
    name-overlap ratio itself has no SQL equivalent, so it still runs in Python — but only
    between one archive file and its own directory's siblings (`direct_children`, an indexed
    prefix query), never against the whole inventory.
    """
    candidates: list[RawCandidate] = []
    for record in index.files_by_ext(_ARCHIVE_EXTS_FOR_PREFILTER, is_dir=False):
        stem = _archive_stem(record.path.name)
        if stem is None:
            continue
        best_match: str | None = None
        best_ratio = 0.0
        for sibling in index.direct_children(record.path.parent):
            if not sibling.is_dir:
                continue
            ratio = SequenceMatcher(None, stem.lower(), sibling.path.name.lower()).ratio()
            if ratio >= _ARCHIVE_OVERLAP_THRESHOLD and ratio > best_ratio:
                best_ratio, best_match = ratio, sibling.path.name
        if best_match is None:
            continue
        candidates.append(
            RawCandidate(
                path=record.path,
                is_dir=False,
                category="archive_pair_extracted",
                category_group=_GROUP_ARCHIVE_PAIRS,
                suggested_tier=Tier.A,
                rationale=(
                    f"Archive '{record.path.name}' has an extracted sibling directory "
                    f"'{best_match}' ({best_ratio:.0%} name overlap) — the extracted copy is "
                    "being kept; only the archive itself is proposed for deletion."
                ),
                rebuild_instruction=None,
            )
        )
    return candidates


# --- Large logs ------------------------------------------------------------------------------


def detect_large_logs(
    index: ScanIndex, *, min_size_bytes: int, stale_days: int, now: float
) -> list[RawCandidate]:
    """SQL-pushdown: `files_larger_than` narrows to files at/above the size threshold via the
    indexed `size` column first — on a typical disk the overwhelming majority of files are far
    smaller than a 50MB default threshold, so this alone eliminates most rows before the
    ext/name-substring/age checks (none of which are cleanly SQL-expressible as a single
    indexed predicate) ever run.
    """
    threshold_seconds = stale_days * _SECONDS_PER_DAY
    candidates: list[RawCandidate] = []
    for record in index.files_larger_than(min_size_bytes, is_dir=False):
        name_lower = record.path.name.lower()
        if record.ext != ".log" and "log" not in name_lower:
            continue
        age_seconds = now - record.mtime
        if age_seconds < threshold_seconds:
            continue
        age_days = int(age_seconds // _SECONDS_PER_DAY)
        size_mb = record.size_bytes / (1024 * 1024)
        threshold_mb = min_size_bytes // (1024 * 1024)
        candidates.append(
            RawCandidate(
                path=record.path,
                is_dir=False,
                category="large_log",
                category_group=_GROUP_LARGE_LOGS,
                suggested_tier=Tier.A,
                rationale=(
                    f"Log file, {size_mb:.1f} MB, not modified in {age_days} days "
                    f"(threshold: {threshold_mb} MB / {stale_days} days) — safe to remove; "
                    "the application creates a new log file as needed."
                ),
                rebuild_instruction=None,
            )
        )
    return candidates


# --- Same-path duplicate suppression (ADR-0004) -------------------------------------------


def _dedupe_by_path(raw: Sequence[RawCandidate]) -> list[RawCandidate]:
    """Keeps only the first-seen `RawCandidate` for any path proposed more than once.

    `detect_crash_dumps` proposes a `.dmp` file both by extension (anywhere in the inventory)
    and, when it happens to sit directly under a configured CrashDumps/WER root, again as that
    root's direct child — two independent `RawCandidate`s for the exact same real file, under
    two different `category` labels. `_drop_nested_candidates` doesn't catch this: same-path
    isn't ancestor-nesting (`Path.parents` never includes the path itself). Left undeduped, a
    real apply processes both: whichever runs second finds the file already gone (deleted by
    its twin) and is recorded as a spurious failure — no data loss, but false "failed" noise
    and a manifest/report that double-counts the candidate. This is a general, detector-agnostic
    guard (not a `detect_crash_dumps`-specific patch) so any other detector introducing the same
    kind of overlap in the future is covered automatically.

    "First seen" is deterministic given `_run_all_detectors`'s fixed call order and each
    detector's own internal iteration order — for `detect_crash_dumps` specifically, its
    extension-based loop runs before its root-children loop, so `crash_dump_file` wins over
    `crash_dump_wer_report` for a shared path, matching which of the two already succeeds first
    in practice.
    """
    seen: set[Path] = set()
    deduped: list[RawCandidate] = []
    for candidate in raw:
        if candidate.path in seen:
            continue
        seen.add(candidate.path)
        deduped.append(candidate)
    return deduped


# --- Nested-candidate suppression --------------------------------------------------------


def _drop_nested_candidates(raw: Sequence[RawCandidate]) -> list[RawCandidate]:
    """Drops any candidate whose path is a strict descendant of another kept *directory*
    candidate — deleting the ancestor directory already removes everything under it, so a
    nested proposal would double-count size and clutter the review queue with redundant
    entries.

    Operates on the raw candidate list every detector above already narrowed via an indexed
    query — bounded by the real number of candidates found, not by total inventory size, so
    this stays a plain in-memory pass (no SQL needed here).

    `kept_dirs` is a `set`, not a list: real-disk finding (2026-07-17) — with 42,185 raw
    candidates (dominated by many sibling, non-nested `__pycache__`/dev-artifact directories
    from a heavily-used Python dev machine), the original `any(directory in
    candidate.path.parents for directory in kept_dirs)` re-scanned the *entire* `kept_dirs`
    list for every candidate (each comparison itself re-scanning `candidate.path.parents`),
    an O(candidates * kept_dirs * depth) blow-up that made this the next real-disk stall after
    every earlier fix in this chain landed. No test/eval fixture had ever used more than a few
    dozen candidates, so the quadratic-ish growth was invisible until this scale. A `set` turns
    "is this ancestor a kept directory" into an O(1) hash lookup, making the whole pass
    O(candidates * depth) regardless of how many directories end up kept.
    """
    ordered = sorted(raw, key=lambda c: len(c.path.parts))
    kept: list[RawCandidate] = []
    kept_dirs: set[Path] = set()
    for candidate in ordered:
        if any(ancestor in kept_dirs for ancestor in candidate.path.parents):
            continue
        kept.append(candidate)
        if candidate.is_dir:
            kept_dirs.add(candidate.path)
    return kept


# --- The SafetyValidator integration boundary ---------------------------------------------

_CATEGORY_GROUP_ENABLED_GETTERS: dict[str, Callable[[Config], bool]] = {
    _GROUP_DEV_ARTIFACTS: lambda c: c.categories.dev_artifacts.enabled,
    _GROUP_PACKAGE_CACHES: lambda c: c.categories.package_caches.enabled,
    _GROUP_MODEL_CACHES: lambda c: c.categories.model_caches.enabled,
    _GROUP_TEMP_AND_BROWSER_CACHES: lambda c: c.categories.temp_and_browser_caches.enabled,
    _GROUP_CRASH_DUMPS: lambda c: c.categories.crash_dumps.enabled,
    _GROUP_OLD_INSTALLERS: lambda c: c.categories.old_installers.enabled,
    _GROUP_ARCHIVE_PAIRS: lambda c: c.categories.archive_pairs.enabled,
    _GROUP_LARGE_LOGS: lambda c: c.categories.large_logs.enabled,
}


def _category_enabled(category_group: str, config: Config) -> bool:
    try:
        getter = _CATEGORY_GROUP_ENABLED_GETTERS[category_group]
    except KeyError as exc:
        raise ValueError(f"unknown candidate category_group: {category_group!r}") from exc
    return getter(config)


# ADR-0001: mirrors `_category_enabled`'s exact dict-of-lambdas shape/error behavior — one
# getter per category group, resolving `config.categories.<group>.retention_days` instead of
# `.enabled`. Kept as a separate dict/function (not folded into the enabled-check) since a
# category's retention window is an independent axis from whether it's Tier-A-capable.
_CATEGORY_GROUP_RETENTION_GETTERS: dict[str, Callable[[Config], int | None]] = {
    _GROUP_DEV_ARTIFACTS: lambda c: c.categories.dev_artifacts.retention_days,
    _GROUP_PACKAGE_CACHES: lambda c: c.categories.package_caches.retention_days,
    _GROUP_MODEL_CACHES: lambda c: c.categories.model_caches.retention_days,
    _GROUP_TEMP_AND_BROWSER_CACHES: lambda c: c.categories.temp_and_browser_caches.retention_days,
    _GROUP_CRASH_DUMPS: lambda c: c.categories.crash_dumps.retention_days,
    _GROUP_OLD_INSTALLERS: lambda c: c.categories.old_installers.retention_days,
    _GROUP_ARCHIVE_PAIRS: lambda c: c.categories.archive_pairs.retention_days,
    _GROUP_LARGE_LOGS: lambda c: c.categories.large_logs.retention_days,
}


def _category_retention_days(category_group: str, config: Config) -> int | None:
    try:
        getter = _CATEGORY_GROUP_RETENTION_GETTERS[category_group]
    except KeyError as exc:
        raise ValueError(f"unknown candidate category_group: {category_group!r}") from exc
    return getter(config)


# ADR-0003 addendum: same dict-of-lambdas shape as the two getters above, resolving
# `config.categories.<group>.size_guard_exempt` where that field exists. Most category groups
# have no such field and are hardcoded `False` — never exempt — since the size guard's whole
# point (recovery cost, not category, gates permanence) only has an exception for categories
# whose rebuild cost is genuinely negligible even at large sizes (package manager caches).
_CATEGORY_GROUP_SIZE_GUARD_EXEMPT_GETTERS: dict[str, Callable[[Config], bool]] = {
    _GROUP_DEV_ARTIFACTS: lambda c: False,
    _GROUP_PACKAGE_CACHES: lambda c: c.categories.package_caches.size_guard_exempt,
    _GROUP_MODEL_CACHES: lambda c: False,
    _GROUP_TEMP_AND_BROWSER_CACHES: lambda c: False,
    _GROUP_CRASH_DUMPS: lambda c: False,
    _GROUP_OLD_INSTALLERS: lambda c: False,
    _GROUP_ARCHIVE_PAIRS: lambda c: False,
    _GROUP_LARGE_LOGS: lambda c: False,
}


def _category_size_guard_exempt(category_group: str, config: Config) -> bool:
    try:
        getter = _CATEGORY_GROUP_SIZE_GUARD_EXEMPT_GETTERS[category_group]
    except KeyError as exc:
        raise ValueError(f"unknown candidate category_group: {category_group!r}") from exc
    return getter(config)


def _run_all_detectors(index: ScanIndex, config: Config, now: float) -> list[RawCandidate]:
    categories = config.categories
    raw: list[RawCandidate] = []
    raw.extend(detect_dev_artifacts(index))
    raw.extend(detect_package_caches(index, categories.package_caches.paths))
    raw.extend(
        detect_model_caches(
            index, categories.model_caches.paths, categories.model_caches.model_extensions
        )
    )
    raw.extend(
        detect_temp_and_browser_caches(
            index,
            categories.temp_and_browser_caches.cache_paths,
            categories.temp_and_browser_caches.temp_roots,
        )
    )
    raw.extend(detect_crash_dumps(index, categories.crash_dumps.paths))
    raw.extend(
        detect_old_installers(index, max_age_days=categories.old_installers.max_age_days, now=now)
    )
    raw.extend(detect_archive_pairs(index))
    raw.extend(
        detect_large_logs(
            index,
            min_size_bytes=categories.large_logs.min_size_bytes,
            stale_days=categories.large_logs.stale_days,
            now=now,
        )
    )
    return raw


def generate_candidates(
    index: ScanIndex, config: Config, safety: SafetyValidator, *, now: float | None = None
) -> list[Candidate]:
    """Runs every rule detector against `index`, then routes every raw proposal through
    `safety.evaluate()` before it is ever tagged Tier A or Tier B — the boundary the spec means
    by "SafetyValidator filters files before they enter the candidate pipeline" (design
    principle 3).

    `Verdict.BLOCKED` -> excluded entirely, does not appear anywhere (not even Tier B).
    `Verdict.REVIEW_ONLY` -> forced into Tier B regardless of what the detector suggested.
    `Verdict.ELIGIBLE` -> the detector's suggested tier, but only if the matching
    `config.categories.*` group is enabled; otherwise it degrades to Tier B. Nothing
    non-blocked is ever silently dropped — every surviving candidate lands in Tier A or B.

    Stage 2 safety boundary: when `config.mode` is `Mode.SAFE`, every surviving candidate is
    forced to Tier B unconditionally — checked ahead of, and independent of, the `Verdict`/
    category-enabled logic above, so safe mode's "no auto-delete, no batch-auto for ANY
    category" guarantee does not depend on every detector's `suggested_tier` staying correct.

    Never materializes the whole inventory: each detector queries `index` directly for only
    the rows it needs, and the one `index.get_record()` call per surviving raw candidate below
    is a single indexed point lookup, not a re-scan.
    """
    now_ts = now if now is not None else time.time()
    raw = _run_all_detectors(index, config, now_ts)
    raw = _dedupe_by_path(raw)
    raw = _drop_nested_candidates(raw)

    candidates: list[Candidate] = []
    for rc in raw:
        record = index.get_record(rc.path)
        if record is None:
            # Defensive: every raw candidate is sourced directly from an `index` query by
            # construction, so this should be unreachable — logged and skipped rather than
            # crashing the whole run on a future detector bug.
            logger.warning("candidates.raw_path_not_in_inventory", path=str(rc.path))
            continue

        result = safety.evaluate(record)
        if result.verdict == Verdict.BLOCKED:
            continue
        if config.mode == Mode.SAFE or result.verdict == Verdict.REVIEW_ONLY:
            tier = Tier.B
        else:
            tier = rc.suggested_tier if _category_enabled(rc.category_group, config) else Tier.B

        size_bytes = index.subtree_size_bytes(rc.path) if rc.is_dir else record.size_bytes
        candidates.append(
            Candidate(
                path=rc.path,
                is_dir=rc.is_dir,
                category=rc.category,
                category_group=rc.category_group,
                size_bytes=size_bytes,
                tier=tier,
                rationale=rc.rationale,
                rebuild_instruction=rc.rebuild_instruction,
                safety_verdict=result.verdict,
                safety_reason_code=result.reason_code,
                retention_days=_category_retention_days(rc.category_group, config),
                recovery_cost_note=rc.recovery_cost_note,
                size_guard_exempt=_category_size_guard_exempt(rc.category_group, config),
                rebuildable=rc.category_group in REBUILDABLE_CATEGORY_GROUPS,
            )
        )
    return candidates
