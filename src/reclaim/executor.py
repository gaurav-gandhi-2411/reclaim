from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import send2trash
import structlog
from pydantic import BaseModel, ConfigDict

from reclaim.models import Candidate, Tier, Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import GitRepoCache, build_record_for_path

logger = structlog.get_logger(__name__)

# Design principle 4 / spec Executor section offers two *quarantine* methods (real recoverability
# via a vault or the Recycle Bin); ADR-0001 adds a third, non-quarantine outcome, `direct_delete`
# (permanent, no vault, no Recycle Bin), assigned per-candidate from `Candidate.retention_days`
# rather than requested for a whole batch — see `_effective_method`. `vault` (move into
# `data/quarantine/<batch_id>/` + manifest JSONL) is the default because it is the only method
# this tool can honestly guarantee restore for: `send2trash` moves a file into the Windows
# Recycle Bin but returns no programmatic handle back to it, so there is no reliable,
# dependency-free way to implement automated batch undo for a Recycle-Bin-quarantined file.
# `recycle_bin` is still offered (spec explicitly lists it), but `restore_batch` refuses to
# fabricate a restore capability it cannot deliver for those entries — see
# `RecycleBinRestoreUnsupportedError`. `restore_batch` refuses `direct_delete` entries too, for
# the stronger reason that no bytes survive anywhere to restore — see
# `DirectDeleteRestoreImpossibleError`.
QuarantineMethod = Literal["vault", "recycle_bin", "direct_delete"]

DEFAULT_VAULT_DIR = Path("data/quarantine")
DEFAULT_MANIFEST_PATH = DEFAULT_VAULT_DIR / "manifest.jsonl"
_SECONDS_PER_DAY = 86400.0


class SafetyInvariantError(RuntimeError):
    """Raised by `apply_batch` when it is handed a BLOCKED candidate.

    Every `Candidate` reaching this module should already have passed `SafetyValidator` in
    Stage 3/4's `generate_candidates`/`generate_duplicate_candidates` — this is a last line of
    defense, not redundant paranoia. Hitting it means an invariant was violated upstream, so the
    whole batch is refused rather than silently dropping the offending item and continuing.
    """


class BatchNotFoundError(RuntimeError):
    """Raised by `restore_batch` when the manifest has no entries for the given `batch_id`."""


class RecycleBinRestoreUnsupportedError(RuntimeError):
    """Raised by `restore_batch` when any entry in the batch was quarantined via `send2trash`.

    There is no programmatic handle back to a Recycle-Bin item, so automated restore cannot be
    honestly offered for it (same "no fabricated confidence" principle the spec applies to
    detection scores, applied here to recoverability claims).
    """


class DirectDeleteRestoreImpossibleError(RuntimeError):
    """Raised by `restore_batch` when any entry in the batch was permanently deleted via
    `retention_days=None` (ADR-0001).

    Distinct from `RecycleBinRestoreUnsupportedError`: a Recycle-Bin item is still recoverable
    by hand via Windows Explorer, and merely unsupported *by this tool*. A `direct_delete`
    entry has no surviving bytes anywhere — restoring it isn't unsupported, it's impossible by
    construction, and the message says so plainly rather than reusing the Recycle-Bin wording.
    """


class VaultIntegrityError(RuntimeError):
    """Raised by `_atomic_move` when a copy-based vault/restore move's destination doesn't
    verify byte/file-count parity with its source (ADR-0004).

    Caught by the same broad per-item `except Exception` in `apply_batch`/`restore_batch` as any
    other filesystem error — one item's integrity failure is recorded as a failed item, it never
    aborts the rest of the batch.
    """


class QuarantineManifestEntry(BaseModel):
    """One line in the append-only `data/quarantine/manifest.jsonl` log.

    The manifest is an event log, not a snapshot table: `apply_batch` appends one entry per
    quarantined item, and `restore_batch`/`purge_expired` append a second entry per updated
    item (same `batch_id`/`original_path` key, `restored`/`purged` fields set) rather than
    rewriting history in place. Readers fold to "current state" by taking the last entry per
    `(batch_id, original_path)` key — see `fold_latest_manifest_entries`.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    original_path: Path
    size_bytes: int
    # ADR-0001: `purge_expired` needs to know whether a purge target is a file or a directory
    # (`Path.unlink()` vs `shutil.rmtree()`) without re-stat'ing `original_path`, which by the
    # time an entry is purge-eligible no longer exists.
    is_dir: bool
    category: str
    category_group: str
    rationale: str
    # ADR-0001: the only "recovery" a direct-deleted (or later-purged) item has — recorded for
    # every entry, not just direct-delete ones, so the manifest stays one uniform shape.
    rebuild_instruction: str | None
    tier: Tier
    method: QuarantineMethod
    vault_path: Path | None
    # ADR-0001: resolved from `Candidate.retention_days` at quarantine time. `None` for a
    # `direct_delete` entry (there is no retention window; nothing was vaulted).
    retention_days: int | None
    quarantined_at: float
    # ADR-0001: `None` for a `direct_delete` entry (no retention window applies) — was
    # previously always populated from a single project-wide 30-day default; now derived
    # per-entry from `retention_days` at quarantine time.
    retention_until: float | None
    restored: bool = False
    restored_at: float | None = None
    # ADR-0001: `purge_expired` marks a vaulted entry purged once its vault copy is permanently
    # deleted past its retention window — same append-only-event-log pattern as `restored`.
    purged: bool = False
    purged_at: float | None = None


@dataclass(frozen=True, slots=True)
class ItemApplyResult:
    """Per-candidate outcome of one `apply_batch` call, real or simulated (dry-run)."""

    path: Path
    category: str
    category_group: str
    size_bytes: int
    tier: Tier
    method: QuarantineMethod
    succeeded: bool
    error: str | None
    vault_path: Path | None


@dataclass(frozen=True, slots=True)
class CategoryBreakdown:
    count: int
    bytes_freed: int


@dataclass(frozen=True, slots=True)
class BatchApplyReport:
    """Post-apply report. Every count/byte number is derived from real per-item results (or,
    for a dry-run, the simulated-as-if-succeeded shape of the same report) — never an estimate,
    per house rule 65b (metric provenance).
    """

    batch_id: str
    apply: bool  # False => dry-run; nothing in this batch touched the filesystem.
    method: QuarantineMethod
    started_at: float
    finished_at: float
    items: tuple[ItemApplyResult, ...]
    files_processed: int
    files_succeeded: int
    files_failed: int
    # Sum of `Candidate.size_bytes` (the size Stage 2's scanner recorded for that specific file)
    # across successfully-quarantined items — a real measured value, not an estimate.
    bytes_freed: int
    category_breakdown: dict[str, CategoryBreakdown]
    # Real `shutil.disk_usage()` free-space measurements, taken immediately before/after an
    # `apply=True` run. Deliberately `None` for a dry-run: no filesystem mutation happened, so
    # there is nothing real to measure, and recording a before==after pair would fabricate a
    # precision this report never actually observed. Deliberately kept separate from
    # `bytes_freed`: the two can legitimately differ (hardlinks, filesystem block rounding) and
    # conflating them would claim false precision.
    disk_free_before_bytes: int | None
    disk_free_after_bytes: int | None
    disk_free_delta_bytes: int | None


@dataclass(frozen=True, slots=True)
class RestoreItemResult:
    original_path: Path
    size_bytes: int
    succeeded: bool
    already_restored: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class RestoreReport:
    batch_id: str
    started_at: float
    finished_at: float
    items: tuple[RestoreItemResult, ...]
    files_processed: int
    files_succeeded: int
    files_failed: int
    bytes_restored: int


def _compute_vault_path(vault_dir: Path, batch_id: str, original_path: Path) -> Path:
    """Unique-per-item vault location. `restore_batch` always moves a file back using the
    manifest's stored `original_path`, so the vault side never needs to mirror the original
    directory structure — a flat, collision-proof name (random prefix + original filename) is
    simpler and sufficient.
    """
    return vault_dir / batch_id / f"{uuid.uuid4().hex}_{original_path.name}"


def _disk_usage_anchor(vault_dir: Path, candidates: Sequence[Candidate]) -> Path | None:
    """Picks the Windows drive root to measure `shutil.disk_usage` on.

    A drive root always exists, unlike any specific candidate path (which this same batch may
    move away between the "before" and "after" measurement). Uses the first candidate's own
    drive so the measurement reflects the drive space is actually being reclaimed from, not
    wherever the vault happens to live; falls back to the vault directory's drive only when no
    candidate carries one (e.g. a relative path in a test fixture).
    """
    for candidate in candidates:
        if candidate.path.drive:
            return Path(f"{candidate.path.drive}\\")
    vault_drive = vault_dir.resolve().drive
    return Path(f"{vault_drive}\\") if vault_drive else None


def _measure_disk_free(anchor: Path | None) -> int | None:
    if anchor is None:
        return None
    try:
        return shutil.disk_usage(anchor).free
    except OSError:
        return None


# --- ADR-0004: long-path-safe, atomic-or-nothing vault/restore moves -------------------------
#
# A real-disk run vaulted a deeply-nested directory (a chat-session scratch tree, thousands of
# short UUID-named subdirectories) and hit Windows' legacy 260-character MAX_PATH limit partway
# through the move: `shutil.move` fell back to `copytree`+`rmtree` (its behavior whenever a
# same-volume `os.rename` isn't usable), `copytree` failed on one over-length nested path, and
# the failure left an orphaned PARTIAL copy sitting in the vault with the original untouched —
# no data was lost, but the vault directory silently held incomplete, unreferenced bytes with no
# manifest entry pointing at them, and the size guard that routes the largest/deepest items to
# `vault` (ADR-0003) systematically makes this MORE likely to recur, not less: the vault
# destination path (`<vault_dir>/<batch_id>/<uuid32>_<name>/...`) is always longer than the
# source, so exactly the highest-value guard-routed targets are the ones most likely to already
# be close to the limit. Empirically confirmed on this system (see PLAN.md's 2026-07-17
# checkpoint): a >260-char path fails even a bare `os.makedirs`/`open()` without the `\\?\`
# extended-length prefix, and succeeds with it — this system has no `LongPathsEnabled` opt-in.
_LONG_PATH_PREFIX = "\\\\?\\"


def _long_path(path: Path) -> str:
    r"""Returns an absolute, `\\?\`-prefixed path string so the Win32 APIs behind `os`/`shutil`
    bypass the legacy 260-character MAX_PATH limit (this tool targets Windows/NTFS exclusively —
    see `pytestmark` in the test suite).

    `\\?\` disables the normal path parser's `.`/`..` and forward-slash handling entirely, so the
    string must already be a fully-normalized, all-backslash absolute path before the prefix is
    added — `str(Path(...))` (not raw string concatenation) guarantees that on Windows. Idempotent:
    a path already carrying the prefix is returned unchanged. UNC paths get the `\\?\UNC\` form;
    drive-letter paths get a plain `\\?\` prefix.
    """
    raw = str(Path(path).absolute())
    if raw.startswith(_LONG_PATH_PREFIX):
        return raw
    if raw.startswith("\\\\"):  # UNC: \\server\share\... -> \\?\UNC\server\share\...
        return _LONG_PATH_PREFIX + "UNC\\" + raw[2:]
    return _LONG_PATH_PREFIX + raw


def _tree_stats(long_path_root: str) -> tuple[int, int]:
    r"""(file_count, total_bytes) for a directory tree, walked via a `\\?\`-prefixed root so it
    works past MAX_PATH. Used by `_atomic_move` to verify a copied vault/restore destination is
    byte-for-byte complete before the source is ever removed.

    Deliberately `os.*`/string paths throughout this function and `_atomic_move` below, never
    `pathlib.Path` — `Path` doesn't reliably round-trip a `\\?\`-prefixed string (it tries to
    parse it as a UNC-style root and mishandles the literal `?` segment), so every PTH-rule
    finding in this section is an intentional, necessary exception, not an oversight.
    """
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(long_path_root):
        for name in filenames:
            total += os.path.getsize(os.path.join(dirpath, name))  # noqa: PTH202, PTH118
            count += 1
    return count, total


def _atomic_move(src: Path, dst: Path, *, is_dir: bool) -> None:
    r"""Moves `src` to `dst` with an "either fully succeeds, or `src` is left completely
    untouched with zero orphaned debris at `dst`" guarantee — never a partial state, and never
    even an empty leftover directory shell (ADR-0004).

    Tries an atomic `os.rename` first: a single filesystem metadata operation that either fully
    succeeds or raises with nothing changed, and — now that both paths are always `\\?\`-prefixed
    — succeeds for same-volume moves regardless of path depth, which is the common case here and
    means the risky fallback below is rarely even reached anymore.

    Only falls back to a manual copy-verify-delete sequence if rename raises `OSError` (e.g. a
    cross-volume `vault_dir`). Even then, `src` is removed ONLY after `dst` is verified to have
    the same file count and total bytes as `src` — an interrupted or partially-failed copy never
    loses data, and a copy that fails partway (the exact failure this ADR responds to) has its
    partial `dst` cleaned up immediately rather than left as orphaned vault debris.

    Owns creating `dst`'s parent directory (rather than requiring the caller to `mkdir` it
    first): if this call is the one that speculatively created that parent and the move then
    fails, the empty parent is removed too — a batch subdirectory made just for one item
    shouldn't outlive that item's failure as debris, but a parent shared with other already-
    vaulted siblings in the same batch is left alone (only removed if it's actually empty).
    """
    long_src = _long_path(src)
    long_dst = _long_path(dst)
    dst_parent = os.path.dirname(long_dst)  # noqa: PTH120 -- str, not Path; see module note above
    parent_already_existed = os.path.isdir(dst_parent)  # noqa: PTH112
    os.makedirs(dst_parent, exist_ok=True)  # noqa: PTH103

    def _cleanup_dst_and_empty_parent() -> None:
        if os.path.exists(long_dst):  # noqa: PTH110
            if os.path.isdir(long_dst):  # noqa: PTH112
                shutil.rmtree(long_dst, ignore_errors=True)
            else:
                os.unlink(long_dst)  # noqa: PTH108
        if (
            not parent_already_existed
            and os.path.isdir(dst_parent)  # noqa: PTH112
            and not os.listdir(dst_parent)  # noqa: PTH208
        ):
            os.rmdir(dst_parent)  # noqa: PTH106

    try:
        os.rename(long_src, long_dst)  # noqa: PTH104
    except OSError:
        pass
    else:
        return

    if is_dir:
        pre_stats = _tree_stats(long_src)
        try:
            shutil.copytree(long_src, long_dst)
        except Exception:
            _cleanup_dst_and_empty_parent()
            raise
        post_stats = _tree_stats(long_dst)
        if post_stats != pre_stats:
            _cleanup_dst_and_empty_parent()
            raise VaultIntegrityError(
                f"copy parity mismatch moving {src} -> {dst}: source had "
                f"{pre_stats[0]} files/{pre_stats[1]} bytes, destination has "
                f"{post_stats[0]} files/{post_stats[1]} bytes"
            )
        shutil.rmtree(long_src)
    else:
        pre_size = os.path.getsize(long_src)  # noqa: PTH202
        try:
            shutil.copy2(long_src, long_dst)
        except Exception:
            _cleanup_dst_and_empty_parent()
            raise
        post_size = os.path.getsize(long_dst)  # noqa: PTH202
        if post_size != pre_size:
            _cleanup_dst_and_empty_parent()
            raise VaultIntegrityError(
                f"copy size mismatch moving {src} -> {dst}: source {pre_size} bytes, "
                f"destination {post_size} bytes"
            )
        os.unlink(long_src)  # noqa: PTH108


def _category_breakdown(items: Sequence[ItemApplyResult]) -> dict[str, CategoryBreakdown]:
    breakdown: dict[str, CategoryBreakdown] = {}
    for item in items:
        if not item.succeeded:
            continue
        existing = breakdown.get(item.category)
        if existing is None:
            breakdown[item.category] = CategoryBreakdown(count=1, bytes_freed=item.size_bytes)
        else:
            breakdown[item.category] = CategoryBreakdown(
                count=existing.count + 1, bytes_freed=existing.bytes_freed + item.size_bytes
            )
    return breakdown


def append_manifest_entries(
    manifest_path: Path, entries: Iterable[QuarantineManifestEntry]
) -> None:
    """Public: reused by `purge.py`, which appends `purged=True` update entries the same
    append-only way `apply_batch`/`restore_batch` already append `restored=True` ones."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(entry.model_dump_json())
            fh.write("\n")


def read_manifest_entries(manifest_path: Path) -> list[QuarantineManifestEntry]:
    """Public: reused by `purge.py` (via `fold_latest_manifest_entries`) and the API layer."""
    if not manifest_path.exists():
        return []
    entries: list[QuarantineManifestEntry] = []
    with manifest_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            entries.append(QuarantineManifestEntry.model_validate_json(stripped))
    return entries


def fold_latest_manifest_entries(manifest_path: Path) -> list[QuarantineManifestEntry]:
    """Folds the append-only event log to current state per `(batch_id, original_path)` — a
    later line (e.g. a restore or purge update) supersedes an earlier one for the same key.
    Public: `purge_expired` reuses this exact fold rule across the *whole* manifest (every
    batch), not just one `batch_id` — see `_latest_entries_for_batch` for the batch-scoped use.
    """
    latest: dict[tuple[str, str], QuarantineManifestEntry] = {}
    for entry in read_manifest_entries(manifest_path):
        latest[(entry.batch_id, entry.original_path.as_posix())] = entry
    return list(latest.values())


def _latest_entries_for_batch(manifest_path: Path, batch_id: str) -> list[QuarantineManifestEntry]:
    return [
        entry for entry in fold_latest_manifest_entries(manifest_path) if entry.batch_id == batch_id
    ]


def _effective_method_and_retention_days(
    candidate: Candidate,
    method: QuarantineMethod,
    *,
    size_guard_bytes: int,
    size_guard_retention_days: int,
) -> tuple[QuarantineMethod, int | None]:
    """A batch's `method` parameter only governs candidates whose category has a real
    retention window; permanent deletion is a property of the *category* (ADR-0001), not a
    per-run choice, so a `retention_days is None` candidate normally always direct-deletes
    regardless of what the caller requested for the rest of the batch.

    ADR-0003: recovery cost, not category, is what should gate permanent deletion. A
    `retention_days is None` candidate at or above `size_guard_bytes` is forced to `vault`
    instead, with its own `size_guard_retention_days` window — independent of the category's
    own (`None`) setting, and regardless of `method`. This is a general safety net (not specific
    to model caches, which already default to vaulted retention and so rarely reach this
    branch) protecting against any category whose direct-delete default turns out, on a given
    disk, to hit an unboundedly expensive-to-redo item.

    ADR-0003 addendum: `candidate.size_guard_exempt` (resolved from
    `config.categories.<group>.size_guard_exempt` where that field exists — package caches only,
    today) skips this guard entirely regardless of size. The guard protects *expensive-to-
    recover* items; a large pip/uv/npm/gradle/yarn cache is exactly as cheap to rebuild at 20GB
    as it is at 20MB (re-fetch public artifacts on the next build), so gating its permanence on
    size alone was penalizing the wrong axis.
    """
    if candidate.retention_days is None:
        if candidate.size_bytes >= size_guard_bytes and not candidate.size_guard_exempt:
            logger.info(
                "executor.retention_size_guard_downgrade",
                path=str(candidate.path),
                size_bytes=candidate.size_bytes,
                category=candidate.category,
                size_guard_bytes=size_guard_bytes,
            )
            return "vault", size_guard_retention_days
        return "direct_delete", None
    return method, candidate.retention_days


def _reverify_direct_delete_candidates(
    candidates: Sequence[Candidate], safety: SafetyValidator
) -> None:
    """ADR-0001's mandatory pre-delete safety re-check: before anything in the batch is
    permanently deleted, every `retention_days is None` candidate is re-evaluated against a
    *freshly reconstructed* `FileRecord` — real current stat + git-repo state via
    `scanner.build_record_for_path`, not whatever the possibly-stale `Candidate` carried from
    whenever candidate generation ran (a bug, a tampered config, or a time-of-check-to-time-of-
    use change like the file having moved into a git repo since it was scanned).

    Any single fresh `Verdict.BLOCKED` aborts the *entire* batch (not just the offending item),
    mirroring the existing BLOCKED-batch-refusal philosophy above: something is fundamentally
    wrong, and the correct response is "stop everything, delete nothing" — not "skip the one
    bad item and proceed with the rest."

    A candidate whose path can no longer be found on disk (already deleted by something else
    between candidate generation and apply) is *not* treated as a safety failure — that's an
    unrelated, already-handled race the per-item try/except in `apply_batch`'s second pass
    naturally reports as a failed item, not a reason to abort every other item in the batch.
    """
    direct_delete = [c for c in candidates if c.retention_days is None]
    if not direct_delete:
        return

    git_cache = GitRepoCache()
    blocked: list[str] = []
    for candidate in direct_delete:
        fresh_record = build_record_for_path(candidate.path, git_cache)
        if fresh_record is None:
            logger.warning("executor.direct_delete_recheck_path_missing", path=str(candidate.path))
            continue
        result = safety.evaluate(fresh_record)
        if result.verdict == Verdict.BLOCKED:
            blocked.append(f"{candidate.path} ({result.reason_code})")

    if blocked:
        raise SafetyInvariantError(
            f"apply_batch's pre-delete safety re-check found {len(blocked)} direct-delete "
            "candidate(s) that fail a FRESH SafetyValidator evaluation against the live "
            f"config — refusing the entire batch, deleting nothing: {blocked[:5]}"
        )


_DEFAULT_DIRECT_DELETE_SIZE_GUARD_BYTES = 1024 * 1024 * 1024
_DEFAULT_DIRECT_DELETE_SIZE_GUARD_RETENTION_DAYS = 30


def apply_batch(
    candidates: list[Candidate],
    *,
    safety: SafetyValidator,
    apply: bool = False,
    method: QuarantineMethod = "vault",
    vault_dir: Path | None = None,
    manifest_path: Path | None = None,
    now: float | None = None,
    direct_delete_size_guard_bytes: int = _DEFAULT_DIRECT_DELETE_SIZE_GUARD_BYTES,
    direct_delete_size_guard_retention_days: int = _DEFAULT_DIRECT_DELETE_SIZE_GUARD_RETENTION_DAYS,
) -> BatchApplyReport:
    """Quarantines (or, for `retention_days=None` candidates, permanently deletes) every
    candidate in one batch.

    Dry-run is the default (`apply=False`, spec design principle 5): makes zero mutating
    filesystem calls — no moves, no `send2trash`/`unlink`/`rmtree` calls, no manifest writes,
    no disk-usage measurement — and returns a report with the same shape as a real run, every
    item simulated as "would succeed", clearly labeled `apply=False`.

    `apply=True` actually moves/trashes/permanently-deletes each file and appends one manifest
    entry per successfully-processed item. A single item's failure (file already gone,
    permission error, ...) is caught, recorded, and does not abort the rest of the batch (house
    rule 104: errors are part of the API, not silent) — *except* for the pre-delete safety
    re-check below, which is a whole-batch abort by design.

    `method` (`"vault"`/`"recycle_bin"`) only governs candidates whose category has a real
    retention window; a candidate with `retention_days is None` always direct-deletes
    regardless of `method` (ADR-0001) — see `_effective_method_and_retention_days`. A single
    batch may therefore mix direct-delete and vaulted/recycle-binned items; `item.method` on
    each `ItemApplyResult` records which one actually applied to that item, not just the
    batch-level `method` param.

    ADR-0003: a `retention_days is None` candidate at or above `direct_delete_size_guard_bytes`
    is forced to `vault` instead of `direct_delete`, with `direct_delete_size_guard_retention_days`
    as its retention window — recovery cost, not category, gates permanent deletion.

    Defense in depth, in two layers:
    1. Raises `SafetyInvariantError` and refuses the *entire* batch if any candidate's
       `safety_verdict` is `Verdict.BLOCKED` — every candidate reaching this function should
       already have passed `SafetyValidator` upstream, so this should never trigger in practice.
    2. ADR-0001, permanent-delete-specific: before deleting anything, every `retention_days is
       None` candidate is re-verified against a *freshly reconstructed* `FileRecord` (real
       current stat + git-repo state, not the possibly-stale `Candidate` fields from whenever
       candidate generation ran) using `safety` (which must be built from the *live* config —
       there is no default, a stale/default validator would make this check meaningless). Any
       single BLOCKED re-verification aborts the whole batch immediately, deleting nothing —
       not even the items that passed — because a bug or a tampered config letting one
       protected file slip through means everything else in the batch is now suspect too.
    """
    if method not in ("vault", "recycle_bin"):
        raise ValueError(
            "apply_batch's method parameter must be 'vault' or 'recycle_bin' — "
            "'direct_delete' is only ever derived per-candidate from Candidate.retention_days, "
            f"never requested for a whole batch: got {method!r}"
        )

    blocked = [c for c in candidates if c.safety_verdict == Verdict.BLOCKED]
    if blocked:
        raise SafetyInvariantError(
            f"apply_batch received {len(blocked)} BLOCKED candidate(s) — refusing the entire "
            "batch. SafetyValidator should have excluded these before they ever reached the "
            f"executor: {[str(c.path) for c in blocked[:5]]}"
        )

    resolved_vault_dir = vault_dir if vault_dir is not None else DEFAULT_VAULT_DIR
    resolved_manifest_path = manifest_path if manifest_path is not None else DEFAULT_MANIFEST_PATH
    now_ts = now if now is not None else time.time()
    batch_id = f"batch_{int(now_ts)}_{uuid.uuid4().hex[:8]}"

    if apply:
        _reverify_direct_delete_candidates(candidates, safety)

    disk_free_before = (
        _measure_disk_free(_disk_usage_anchor(resolved_vault_dir, candidates)) if apply else None
    )

    items: list[ItemApplyResult] = []
    manifest_entries: list[QuarantineManifestEntry] = []
    for candidate in candidates:
        item_method, item_retention_days = _effective_method_and_retention_days(
            candidate,
            method,
            size_guard_bytes=direct_delete_size_guard_bytes,
            size_guard_retention_days=direct_delete_size_guard_retention_days,
        )
        item_retention_until = (
            now_ts + item_retention_days * _SECONDS_PER_DAY
            if item_retention_days is not None
            else None
        )

        if not apply:
            vault_path = (
                _compute_vault_path(resolved_vault_dir, batch_id, candidate.path)
                if item_method == "vault"
                else None
            )
            items.append(
                ItemApplyResult(
                    path=candidate.path,
                    category=candidate.category,
                    category_group=candidate.category_group,
                    size_bytes=candidate.size_bytes,
                    tier=candidate.tier,
                    method=item_method,
                    succeeded=True,
                    error=None,
                    vault_path=vault_path,
                )
            )
            continue

        try:
            if item_method == "vault":
                vault_path = _compute_vault_path(resolved_vault_dir, batch_id, candidate.path)
                _atomic_move(candidate.path, vault_path, is_dir=candidate.is_dir)
            elif item_method == "recycle_bin":
                send2trash.send2trash(str(candidate.path))
                vault_path = None
            else:  # direct_delete: permanent, no vault, no Recycle Bin (ADR-0001)
                if candidate.is_dir:
                    shutil.rmtree(_long_path(candidate.path))
                else:
                    os.unlink(_long_path(candidate.path))  # noqa: PTH108 -- \\?\ str, not Path
                vault_path = None
        except Exception as exc:  # broad on purpose: isolates one item's failure from the batch
            logger.warning(
                "executor.apply_item_failed",
                path=str(candidate.path),
                method=item_method,
                error=str(exc),
            )
            items.append(
                ItemApplyResult(
                    path=candidate.path,
                    category=candidate.category,
                    category_group=candidate.category_group,
                    size_bytes=candidate.size_bytes,
                    tier=candidate.tier,
                    method=item_method,
                    succeeded=False,
                    error=str(exc),
                    vault_path=None,
                )
            )
            continue

        items.append(
            ItemApplyResult(
                path=candidate.path,
                category=candidate.category,
                category_group=candidate.category_group,
                size_bytes=candidate.size_bytes,
                tier=candidate.tier,
                method=item_method,
                succeeded=True,
                error=None,
                vault_path=vault_path,
            )
        )
        manifest_entries.append(
            QuarantineManifestEntry(
                batch_id=batch_id,
                original_path=candidate.path,
                size_bytes=candidate.size_bytes,
                is_dir=candidate.is_dir,
                category=candidate.category,
                category_group=candidate.category_group,
                rationale=candidate.rationale,
                rebuild_instruction=candidate.rebuild_instruction,
                tier=candidate.tier,
                method=item_method,
                vault_path=vault_path,
                retention_days=item_retention_days,
                quarantined_at=now_ts,
                retention_until=item_retention_until,
            )
        )

    if manifest_entries:
        append_manifest_entries(resolved_manifest_path, manifest_entries)

    disk_free_after = (
        _measure_disk_free(_disk_usage_anchor(resolved_vault_dir, candidates)) if apply else None
    )
    disk_free_delta = (
        disk_free_after - disk_free_before
        if disk_free_before is not None and disk_free_after is not None
        else None
    )

    succeeded_items = [item for item in items if item.succeeded]
    failed_items = [item for item in items if not item.succeeded]
    return BatchApplyReport(
        batch_id=batch_id,
        apply=apply,
        method=method,
        started_at=now_ts,
        finished_at=time.time(),
        items=tuple(items),
        files_processed=len(items),
        files_succeeded=len(succeeded_items),
        files_failed=len(failed_items),
        bytes_freed=sum(item.size_bytes for item in succeeded_items),
        category_breakdown=_category_breakdown(items),
        disk_free_before_bytes=disk_free_before,
        disk_free_after_bytes=disk_free_after,
        disk_free_delta_bytes=disk_free_delta,
    )


def restore_batch(
    batch_id: str,
    *,
    manifest_path: Path | None = None,
    now: float | None = None,
) -> RestoreReport:
    """Restores every item in `batch_id` back to its exact original path.

    Reads current state from the manifest (see `_latest_entries_for_batch`). Refuses the whole
    batch loudly if any entry in it cannot honestly be restored:
    - `DirectDeleteRestoreImpossibleError` for `direct_delete` entries (ADR-0001) — no bytes
      survive anywhere for these, restoring them isn't merely unsupported, it's impossible.
    - `RecycleBinRestoreUnsupportedError` for `recycle_bin` entries — there is no programmatic
      handle back to a Recycle-Bin item, so this never fabricates a restore capability it
      cannot deliver.
    A batch's entries always share one requested `method` per `apply_batch` call, but ADR-0001
    means a single batch can still mix `vault`/`recycle_bin` entries with `direct_delete` ones
    (candidates with `retention_days is None` always direct-delete regardless of the batch's
    requested method) — either refusal check can therefore fire independently of the other.

    Never overwrites an existing file at the destination — an item whose original path is
    occupied by something else now fails loudly (recorded in the report) rather than silently
    clobbering it. Idempotent: an item already marked `restored=True` is reported as
    `already_restored` and left untouched, so restoring the same batch twice is safe.
    """
    resolved_manifest_path = manifest_path if manifest_path is not None else DEFAULT_MANIFEST_PATH
    now_ts = now if now is not None else time.time()

    entries = _latest_entries_for_batch(resolved_manifest_path, batch_id)
    if not entries:
        raise BatchNotFoundError(f"no manifest entries found for batch_id={batch_id!r}")

    direct_delete_entries = [entry for entry in entries if entry.method == "direct_delete"]
    if direct_delete_entries:
        raise DirectDeleteRestoreImpossibleError(
            f"this batch contains {len(direct_delete_entries)} permanently-deleted file(s) "
            "(retention=none for their category) — there is nothing to restore, they were "
            "not quarantined"
        )

    recycle_bin_entries = [entry for entry in entries if entry.method == "recycle_bin"]
    if recycle_bin_entries:
        raise RecycleBinRestoreUnsupportedError(
            f"this batch contains {len(recycle_bin_entries)} Recycle-Bin-quarantined file(s); "
            "restore them manually via Windows Explorer's Recycle Bin — automated restore "
            "isn't supported for this method"
        )

    items: list[RestoreItemResult] = []
    updated_entries: list[QuarantineManifestEntry] = []
    for entry in entries:
        if entry.restored:
            items.append(
                RestoreItemResult(
                    original_path=entry.original_path,
                    size_bytes=entry.size_bytes,
                    succeeded=True,
                    already_restored=True,
                    error=None,
                )
            )
            continue

        if entry.vault_path is None:
            # Unreachable in practice: the recycle_bin check above already excludes every
            # method that can reach this point without a vault_path. Guards mypy's None
            # narrowing and, if manifest data were ever corrupted, fails loudly per-item rather
            # than crashing the whole restore.
            items.append(
                RestoreItemResult(
                    original_path=entry.original_path,
                    size_bytes=entry.size_bytes,
                    succeeded=False,
                    already_restored=False,
                    error="manifest entry has method=vault but no vault_path recorded",
                )
            )
            continue

        if os.path.exists(_long_path(entry.original_path)):  # noqa: PTH110 -- \\?\ str, not Path
            items.append(
                RestoreItemResult(
                    original_path=entry.original_path,
                    size_bytes=entry.size_bytes,
                    succeeded=False,
                    already_restored=False,
                    error=(
                        f"destination already exists, refusing to overwrite: {entry.original_path}"
                    ),
                )
            )
            continue

        try:
            _atomic_move(entry.vault_path, entry.original_path, is_dir=entry.is_dir)
        except (OSError, VaultIntegrityError) as exc:
            logger.warning(
                "executor.restore_item_failed",
                path=str(entry.original_path),
                error=str(exc),
            )
            items.append(
                RestoreItemResult(
                    original_path=entry.original_path,
                    size_bytes=entry.size_bytes,
                    succeeded=False,
                    already_restored=False,
                    error=str(exc),
                )
            )
            continue

        items.append(
            RestoreItemResult(
                original_path=entry.original_path,
                size_bytes=entry.size_bytes,
                succeeded=True,
                already_restored=False,
                error=None,
            )
        )
        updated_entries.append(entry.model_copy(update={"restored": True, "restored_at": now_ts}))

    if updated_entries:
        append_manifest_entries(resolved_manifest_path, updated_entries)

    succeeded_items = [item for item in items if item.succeeded]
    failed_items = [item for item in items if not item.succeeded]
    return RestoreReport(
        batch_id=batch_id,
        started_at=now_ts,
        finished_at=time.time(),
        items=tuple(items),
        files_processed=len(items),
        files_succeeded=len(succeeded_items),
        files_failed=len(failed_items),
        bytes_restored=sum(
            item.size_bytes for item in succeeded_items if not item.already_restored
        ),
    )
