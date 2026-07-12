from __future__ import annotations

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

logger = structlog.get_logger(__name__)

# Design principle 4 / spec Executor section offers two quarantine methods. `vault` (move into
# `data/quarantine/<batch_id>/` + manifest JSONL) is the default because it is the only method
# this tool can honestly guarantee restore for: `send2trash` moves a file into the Windows
# Recycle Bin but returns no programmatic handle back to it, so there is no reliable,
# dependency-free way to implement automated batch undo for a Recycle-Bin-quarantined file.
# `recycle_bin` is still offered (spec explicitly lists it), but `restore_batch` refuses to
# fabricate a restore capability it cannot deliver for those entries — see
# `RecycleBinRestoreUnsupportedError`.
QuarantineMethod = Literal["vault", "recycle_bin"]

_DEFAULT_VAULT_DIR = Path("data/quarantine")
_DEFAULT_MANIFEST_PATH = _DEFAULT_VAULT_DIR / "manifest.jsonl"
# Spec: "Retention default 30 days." Stored on every manifest entry as metadata only — no code
# path in this module (or anywhere else in v1) ever reads `retention_until` to purge a file.
# Spec, twice: "No permanent delete in v1" / "No Tier for silent permanent deletion. It does
# not exist in v1."
_RETENTION_DAYS = 30
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


class QuarantineManifestEntry(BaseModel):
    """One line in the append-only `data/quarantine/manifest.jsonl` log.

    The manifest is an event log, not a snapshot table: `apply_batch` appends one entry per
    quarantined item, and `restore_batch` appends a second entry per restored item (same
    `batch_id`/`original_path` key, `restored=True`/`restored_at` set) rather than rewriting
    history in place. Readers fold to "current state" by taking the last entry per
    `(batch_id, original_path)` key — see `_latest_entries_for_batch`.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    original_path: Path
    size_bytes: int
    category: str
    category_group: str
    rationale: str
    tier: Tier
    method: QuarantineMethod
    vault_path: Path | None
    quarantined_at: float
    retention_until: float
    restored: bool = False
    restored_at: float | None = None


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


def _append_manifest_entries(
    manifest_path: Path, entries: Iterable[QuarantineManifestEntry]
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(entry.model_dump_json())
            fh.write("\n")


def _read_all_manifest_entries(manifest_path: Path) -> list[QuarantineManifestEntry]:
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


def _latest_entries_for_batch(manifest_path: Path, batch_id: str) -> list[QuarantineManifestEntry]:
    """Folds the append-only event log to current state per `(batch_id, original_path)` — a
    later line (e.g. a restore update) supersedes an earlier one for the same key — then
    returns only the entries belonging to `batch_id`.
    """
    latest: dict[tuple[str, str], QuarantineManifestEntry] = {}
    for entry in _read_all_manifest_entries(manifest_path):
        latest[(entry.batch_id, entry.original_path.as_posix())] = entry
    return [entry for entry in latest.values() if entry.batch_id == batch_id]


def apply_batch(
    candidates: list[Candidate],
    *,
    apply: bool = False,
    method: QuarantineMethod = "vault",
    vault_dir: Path | None = None,
    manifest_path: Path | None = None,
    now: float | None = None,
) -> BatchApplyReport:
    """Quarantines every candidate in one batch.

    Dry-run is the default (`apply=False`, spec design principle 5): makes zero filesystem
    calls — no moves, no `send2trash` calls, no manifest writes, no disk-usage measurement —
    and returns a report with the same shape as a real run, every item simulated as
    "would succeed", clearly labeled `apply=False`.

    `apply=True` actually moves/trashes each file and appends one manifest entry per
    successfully-quarantined item. A single item's failure (file already gone, permission
    error, ...) is caught, recorded, and does not abort the rest of the batch (house rule 104:
    errors are part of the API, not silent).

    Defense in depth: raises `SafetyInvariantError` and refuses the *entire* batch if any
    candidate's `safety_verdict` is `Verdict.BLOCKED` — every candidate reaching this function
    should already have passed `SafetyValidator` upstream, so this should never trigger in
    practice.
    """
    blocked = [c for c in candidates if c.safety_verdict == Verdict.BLOCKED]
    if blocked:
        raise SafetyInvariantError(
            f"apply_batch received {len(blocked)} BLOCKED candidate(s) — refusing the entire "
            "batch. SafetyValidator should have excluded these before they ever reached the "
            f"executor: {[str(c.path) for c in blocked[:5]]}"
        )

    resolved_vault_dir = vault_dir if vault_dir is not None else _DEFAULT_VAULT_DIR
    resolved_manifest_path = manifest_path if manifest_path is not None else _DEFAULT_MANIFEST_PATH
    now_ts = now if now is not None else time.time()
    batch_id = f"batch_{int(now_ts)}_{uuid.uuid4().hex[:8]}"
    retention_until = now_ts + _RETENTION_DAYS * _SECONDS_PER_DAY

    disk_free_before = (
        _measure_disk_free(_disk_usage_anchor(resolved_vault_dir, candidates)) if apply else None
    )

    items: list[ItemApplyResult] = []
    manifest_entries: list[QuarantineManifestEntry] = []
    for candidate in candidates:
        if not apply:
            vault_path = (
                _compute_vault_path(resolved_vault_dir, batch_id, candidate.path)
                if method == "vault"
                else None
            )
            items.append(
                ItemApplyResult(
                    path=candidate.path,
                    category=candidate.category,
                    category_group=candidate.category_group,
                    size_bytes=candidate.size_bytes,
                    tier=candidate.tier,
                    method=method,
                    succeeded=True,
                    error=None,
                    vault_path=vault_path,
                )
            )
            continue

        try:
            if method == "vault":
                vault_path = _compute_vault_path(resolved_vault_dir, batch_id, candidate.path)
                vault_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(candidate.path), str(vault_path))
            else:
                send2trash.send2trash(str(candidate.path))
                vault_path = None
        except Exception as exc:  # broad on purpose: isolates one item's failure from the batch
            logger.warning(
                "executor.apply_item_failed",
                path=str(candidate.path),
                method=method,
                error=str(exc),
            )
            items.append(
                ItemApplyResult(
                    path=candidate.path,
                    category=candidate.category,
                    category_group=candidate.category_group,
                    size_bytes=candidate.size_bytes,
                    tier=candidate.tier,
                    method=method,
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
                method=method,
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
                category=candidate.category,
                category_group=candidate.category_group,
                rationale=candidate.rationale,
                tier=candidate.tier,
                method=method,
                vault_path=vault_path,
                quarantined_at=now_ts,
                retention_until=retention_until,
            )
        )

    if manifest_entries:
        _append_manifest_entries(resolved_manifest_path, manifest_entries)

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
    batch loudly (`RecycleBinRestoreUnsupportedError`) if any entry in it was quarantined via
    `send2trash` — there is no programmatic handle back to a Recycle-Bin item, so this never
    fabricates a restore capability it cannot deliver. A batch's entries always share one
    `method` (set once per `apply_batch` call), so in practice this means "all or nothing" per
    batch, not a silent partial restore.

    Never overwrites an existing file at the destination — an item whose original path is
    occupied by something else now fails loudly (recorded in the report) rather than silently
    clobbering it. Idempotent: an item already marked `restored=True` is reported as
    `already_restored` and left untouched, so restoring the same batch twice is safe.
    """
    resolved_manifest_path = manifest_path if manifest_path is not None else _DEFAULT_MANIFEST_PATH
    now_ts = now if now is not None else time.time()

    entries = _latest_entries_for_batch(resolved_manifest_path, batch_id)
    if not entries:
        raise BatchNotFoundError(f"no manifest entries found for batch_id={batch_id!r}")

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

        if entry.original_path.exists():
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
            entry.original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry.vault_path), str(entry.original_path))
        except OSError as exc:
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
        _append_manifest_entries(resolved_manifest_path, updated_entries)

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
