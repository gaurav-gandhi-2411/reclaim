from __future__ import annotations

import fnmatch
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import structlog

from reclaim.config import Config
from reclaim.index import ScanIndex
from reclaim.models import Candidate, FileRecord, RawCandidate, Tier, Verdict
from reclaim.safety import SafetyValidator

logger = structlog.get_logger(__name__)

_SECONDS_PER_DAY = 86400.0

# Config `[categories]` group ids — the coarse identifier every `RawCandidate.category_group`
# must be one of, matched against `_CATEGORY_GROUP_ENABLED_GETTERS` in `_category_enabled`.
_GROUP_DEV_ARTIFACTS = "dev_artifacts"
_GROUP_PACKAGE_CACHES = "package_caches"
_GROUP_TEMP_AND_BROWSER_CACHES = "temp_and_browser_caches"
_GROUP_CRASH_DUMPS = "crash_dumps"
_GROUP_OLD_INSTALLERS = "old_installers"
_GROUP_ARCHIVE_PAIRS = "archive_pairs"
_GROUP_LARGE_LOGS = "large_logs"


@dataclass(frozen=True, slots=True)
class InventoryContext:
    """Detector working set, built once per `generate_candidates` run so every detector
    shares the same in-memory index instead of each re-scanning `ScanIndex` output."""

    by_path: dict[Path, FileRecord]
    children_by_dir: dict[Path, list[FileRecord]]


def build_inventory_context(records: Sequence[FileRecord]) -> InventoryContext:
    by_path: dict[Path, FileRecord] = {record.path: record for record in records}
    children_by_dir: dict[Path, list[FileRecord]] = defaultdict(list)
    for record in records:
        children_by_dir[record.path.parent].append(record)
    return InventoryContext(by_path=by_path, children_by_dir=dict(children_by_dir))


def _matches_any_pattern(path: Path, patterns: Sequence[str]) -> bool:
    candidate = path.as_posix().lower()
    return any(fnmatch.fnmatch(candidate, pattern.lower()) for pattern in patterns)


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


def _has_sibling_manifest(
    ctx: InventoryContext, parent: Path, manifest_names: Sequence[str]
) -> bool:
    return any((parent / name) in ctx.by_path for name in manifest_names)


def detect_dev_artifacts(ctx: InventoryContext) -> list[RawCandidate]:
    """Dev-artifact directories, gated on manifest adjacency: a rebuildable-cache directory is
    only ever proposed when a manifest proving rebuildability sits in its parent directory —
    no manifest adjacent means the path is never proposed, not even as a Tier B candidate
    (spec invariant, absolute). `__pycache__` is the one exception: unconditionally
    regeneratable bytecode, so it needs no manifest check at all.
    """
    candidates: list[RawCandidate] = []
    for record in ctx.by_path.values():
        if not record.is_dir:
            continue
        name_lower = record.path.name.lower()
        if name_lower == "__pycache__":
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
            continue
        if not _has_sibling_manifest(ctx, record.path.parent, spec.manifest_names):
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


def detect_package_caches(ctx: InventoryContext, cache_paths: Sequence[str]) -> list[RawCandidate]:
    """Global package/model download caches (pip, npm, uv, HuggingFace hub, torch hub, conda
    pkgs, .m2/.gradle) — no manifest-adjacency check, matched purely by configured path
    patterns (`config.categories.package_caches.paths`, defaulting to the real Windows
    locations)."""
    candidates: list[RawCandidate] = []
    for record in ctx.by_path.values():
        if not record.is_dir or not _matches_any_pattern(record.path, cache_paths):
            continue
        candidates.append(
            RawCandidate(
                path=record.path,
                is_dir=True,
                category="package_cache",
                category_group=_GROUP_PACKAGE_CACHES,
                suggested_tier=Tier.A,
                rationale=(
                    "Package/model download cache — the owning tool re-downloads artifacts "
                    "into this directory automatically on next use."
                ),
                rebuild_instruction=(
                    "Re-run the package manager or model download; the cache repopulates "
                    "automatically."
                ),
            )
        )
    return candidates


# --- Browser/temp/thumbnail caches ----------------------------------------------------------


def detect_temp_and_browser_caches(
    ctx: InventoryContext, cache_paths: Sequence[str], temp_roots: Sequence[str]
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
    for record in ctx.by_path.values():
        if not _matches_any_pattern(record.path, cache_paths):
            continue
        is_thumbnail = record.path.name.lower().startswith("thumbcache_")
        candidates.append(
            RawCandidate(
                path=record.path,
                is_dir=record.is_dir,
                category="thumbnail_cache" if is_thumbnail else "browser_cache",
                category_group=_GROUP_TEMP_AND_BROWSER_CACHES,
                suggested_tier=Tier.A,
                rationale=(
                    "Windows Explorer thumbnail cache — regenerated automatically as folders "
                    "are browsed."
                    if is_thumbnail
                    else "Browser cache directory — regenerated automatically by the browser."
                ),
                rebuild_instruction="Regenerates automatically on next use; no manual step.",
            )
        )

    for record in ctx.by_path.values():
        if not record.is_dir or not _matches_any_pattern(record.path, temp_roots):
            continue
        for child in ctx.children_by_dir.get(record.path, []):
            candidates.append(
                RawCandidate(
                    path=child.path,
                    is_dir=child.is_dir,
                    category="windows_temp",
                    category_group=_GROUP_TEMP_AND_BROWSER_CACHES,
                    suggested_tier=Tier.A,
                    rationale=(
                        f"Item in the Windows temp directory ('{record.path}') — temp files "
                        "are transient by design and safe to remove."
                    ),
                    rebuild_instruction=None,
                )
            )
    return candidates


# --- Crash dumps ---------------------------------------------------------------------------


def detect_crash_dumps(ctx: InventoryContext, root_paths: Sequence[str]) -> list[RawCandidate]:
    """`.dmp` files anywhere in the inventory, plus every direct child of the configured
    CrashDumps/WER report root directories (never the root directories themselves)."""
    candidates: list[RawCandidate] = []
    for record in ctx.by_path.values():
        if record.is_dir or record.ext != ".dmp":
            continue
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

    for record in ctx.by_path.values():
        if not record.is_dir or not _matches_any_pattern(record.path, root_paths):
            continue
        for child in ctx.children_by_dir.get(record.path, []):
            candidates.append(
                RawCandidate(
                    path=child.path,
                    is_dir=child.is_dir,
                    category="crash_dump_wer_report",
                    category_group=_GROUP_CRASH_DUMPS,
                    suggested_tier=Tier.A,
                    rationale=(
                        f"Windows Error Reporting artifact under '{record.path}' — diagnostic "
                        "data from a past crash, regenerated automatically as needed."
                    ),
                    rebuild_instruction=None,
                )
            )
    return candidates


# --- Old installers --------------------------------------------------------------------------

_INSTALLER_EXTENSIONS = frozenset({".exe", ".msi", ".iso"})


def detect_old_installers(
    ctx: InventoryContext, *, max_age_days: int, now: float
) -> list[RawCandidate]:
    """Installer files (`.exe`/`.msi`/`.iso`) under a `Downloads` directory older than
    `max_age_days` (by mtime). Every detector in this module suggests Tier A uniformly —
    whether an old-installer candidate actually reaches Tier A or degrades to Tier B (spec:
    review-queue by default) is decided once, centrally, in `generate_candidates`, based on
    `config.categories.old_installers.enabled`.
    """
    threshold_seconds = max_age_days * _SECONDS_PER_DAY
    candidates: list[RawCandidate] = []
    for record in ctx.by_path.values():
        if record.is_dir or record.ext not in _INSTALLER_EXTENSIONS:
            continue
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


def _archive_stem(name: str) -> str | None:
    lower = name.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return None


def detect_archive_pairs(ctx: InventoryContext) -> list[RawCandidate]:
    """An archive file with a sibling directory whose name has >=90% overlap with the
    archive's stem (`difflib.SequenceMatcher` ratio, case-insensitive) proposes deleting
    *only* the archive — the extracted directory (and everything inside it) is never proposed
    by this detector, since it's explicitly the copy being kept.
    """
    candidates: list[RawCandidate] = []
    for record in ctx.by_path.values():
        if record.is_dir:
            continue
        stem = _archive_stem(record.path.name)
        if stem is None:
            continue
        best_match: str | None = None
        best_ratio = 0.0
        for sibling in ctx.children_by_dir.get(record.path.parent, []):
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
    ctx: InventoryContext, *, min_size_bytes: int, stale_days: int, now: float
) -> list[RawCandidate]:
    threshold_seconds = stale_days * _SECONDS_PER_DAY
    candidates: list[RawCandidate] = []
    for record in ctx.by_path.values():
        if record.is_dir:
            continue
        name_lower = record.path.name.lower()
        if record.ext != ".log" and "log" not in name_lower:
            continue
        if record.size_bytes < min_size_bytes:
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


# --- Nested-candidate suppression --------------------------------------------------------


def _drop_nested_candidates(raw: Sequence[RawCandidate]) -> list[RawCandidate]:
    """Drops any candidate whose path is a strict descendant of another kept *directory*
    candidate — deleting the ancestor directory already removes everything under it, so a
    nested proposal would double-count size and clutter the review queue with redundant
    entries.
    """
    ordered = sorted(raw, key=lambda c: len(c.path.parts))
    kept: list[RawCandidate] = []
    kept_dirs: list[Path] = []
    for candidate in ordered:
        if any(directory in candidate.path.parents for directory in kept_dirs):
            continue
        kept.append(candidate)
        if candidate.is_dir:
            kept_dirs.append(candidate.path)
    return kept


# --- The SafetyValidator integration boundary ---------------------------------------------

_CATEGORY_GROUP_ENABLED_GETTERS: dict[str, Callable[[Config], bool]] = {
    _GROUP_DEV_ARTIFACTS: lambda c: c.categories.dev_artifacts,
    _GROUP_PACKAGE_CACHES: lambda c: c.categories.package_caches.enabled,
    _GROUP_TEMP_AND_BROWSER_CACHES: lambda c: c.categories.temp_and_browser_caches.enabled,
    _GROUP_CRASH_DUMPS: lambda c: c.categories.crash_dumps.enabled,
    _GROUP_OLD_INSTALLERS: lambda c: c.categories.old_installers.enabled,
    _GROUP_ARCHIVE_PAIRS: lambda c: c.categories.archive_pairs,
    _GROUP_LARGE_LOGS: lambda c: c.categories.large_logs.enabled,
}


def _category_enabled(category_group: str, config: Config) -> bool:
    try:
        getter = _CATEGORY_GROUP_ENABLED_GETTERS[category_group]
    except KeyError as exc:
        raise ValueError(f"unknown candidate category_group: {category_group!r}") from exc
    return getter(config)


def _run_all_detectors(ctx: InventoryContext, config: Config, now: float) -> list[RawCandidate]:
    categories = config.categories
    raw: list[RawCandidate] = []
    raw.extend(detect_dev_artifacts(ctx))
    raw.extend(detect_package_caches(ctx, categories.package_caches.paths))
    raw.extend(
        detect_temp_and_browser_caches(
            ctx,
            categories.temp_and_browser_caches.cache_paths,
            categories.temp_and_browser_caches.temp_roots,
        )
    )
    raw.extend(detect_crash_dumps(ctx, categories.crash_dumps.paths))
    raw.extend(
        detect_old_installers(ctx, max_age_days=categories.old_installers.max_age_days, now=now)
    )
    raw.extend(detect_archive_pairs(ctx))
    raw.extend(
        detect_large_logs(
            ctx,
            min_size_bytes=categories.large_logs.min_size_bytes,
            stale_days=categories.large_logs.stale_days,
            now=now,
        )
    )
    return raw


def generate_candidates(
    index: ScanIndex, config: Config, safety: SafetyValidator, *, now: float | None = None
) -> list[Candidate]:
    """Runs every rule detector against `index`'s candidate-eligible inventory, then routes
    every raw proposal through `safety.evaluate()` before it is ever tagged Tier A or Tier B —
    the boundary the spec means by "SafetyValidator filters files before they enter the
    candidate pipeline" (design principle 3).

    `Verdict.BLOCKED` -> excluded entirely, does not appear anywhere (not even Tier B).
    `Verdict.REVIEW_ONLY` -> forced into Tier B regardless of what the detector suggested.
    `Verdict.ELIGIBLE` -> the detector's suggested tier, but only if the matching
    `config.categories.*` group is enabled; otherwise it degrades to Tier B. Nothing
    non-blocked is ever silently dropped — every surviving candidate lands in Tier A or B.
    """
    now_ts = now if now is not None else time.time()
    records = index.candidate_inventory()
    ctx = build_inventory_context(records)

    raw = _run_all_detectors(ctx, config, now_ts)
    raw = _drop_nested_candidates(raw)

    candidates: list[Candidate] = []
    for rc in raw:
        record = ctx.by_path.get(rc.path)
        if record is None:
            # Defensive: every raw candidate is sourced directly from `ctx.by_path`/
            # `ctx.children_by_dir` entries by construction, so this should be unreachable —
            # logged and skipped rather than crashing the whole run on a future detector bug.
            logger.warning("candidates.raw_path_not_in_inventory", path=str(rc.path))
            continue

        result = safety.evaluate(record)
        if result.verdict == Verdict.BLOCKED:
            continue
        if result.verdict == Verdict.REVIEW_ONLY:
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
            )
        )
    return candidates
