from __future__ import annotations

import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

from reclaim.executor import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_VAULT_DIR,
    CategoryBreakdown,
    QuarantineManifestEntry,
    SafeModeViolationError,
    SafetyInvariantError,
    append_manifest_entries,
    fold_latest_manifest_entries,
    long_path,
    rmtree_clear_readonly,
    unlink_clear_readonly,
)
from reclaim.models import REBUILDABLE_CATEGORY_GROUPS, FileRecord, Mode, Verdict
from reclaim.safety import SafetyValidator

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PurgeItemResult:
    """Per-entry outcome of one `purge_expired` call, real or simulated (dry-run)."""

    original_path: Path
    vault_path: Path | None
    category: str
    category_group: str
    size_bytes: int
    succeeded: bool
    error: str | None
    # ADR-0005: `True` when this entry became purge-eligible because its `original_path` is
    # now re-occupied (a stale vault copy that can never be usefully restored), rather than
    # via genuine `retention_until` expiry. Surfaced so a report never conflates "this expired
    # normally" with "this was dead weight from the moment something else took its place."
    stale: bool = False


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """Mirrors `executor.BatchApplyReport`'s shape/rigor: real per-item results, real
    `shutil.disk_usage()` before/after (this time the delta *should* be non-zero, since purge
    actually removes bytes from the volume the vault lives on — unlike vaulting, which just
    moves bytes to another directory on the same volume), real category breakdown.
    """

    apply: bool  # False => dry-run; nothing touched the filesystem.
    started_at: float
    finished_at: float
    items: tuple[PurgeItemResult, ...]
    files_processed: int
    files_succeeded: int
    files_failed: int
    bytes_freed: int
    # ADR-0005: subset of `files_succeeded`/`bytes_freed` selected via the stale-original-
    # reoccupied path rather than genuine retention expiry — reported separately so "the vault
    # copy was pure dead weight" is visible, not folded silently into the normal purge count.
    stale_count: int
    stale_bytes: int
    category_breakdown: dict[str, CategoryBreakdown]
    disk_free_before_bytes: int | None
    disk_free_after_bytes: int | None
    disk_free_delta_bytes: int | None


def _is_stale_original_reoccupied(entry: QuarantineManifestEntry) -> bool:
    """True if a vaulted entry's `original_path` is occupied again — almost always a
    regenerated cache (uv/pip/npm/gradle redownload their own cache directory automatically,
    exactly as their `rebuild_instruction` already promises), never a human restore (a real
    restore updates this same entry's `restored=True`, already excluded upstream by the
    `entry.restored` check). Restoring a stale entry would refuse — "destination already
    exists" — regardless of `retention_until`, so continuing to hold the vault copy for the
    rest of its retention window serves no purpose (ADR-0005): it can never be usefully
    restored, only ever purged.
    """
    return os.path.exists(long_path(entry.original_path))  # noqa: PTH110 -- \\?\ str, not Path


def _purge_eligible_entries(
    manifest_path: Path, now_ts: float
) -> list[tuple[QuarantineManifestEntry, bool]]:
    """Selects purge-eligible vault entries, paired with whether each is eligible via genuine
    retention expiry (`False`) or ADR-0005's stale-original-reoccupied check (`True`).

    Two independent eligibility paths, either sufficient on its own:
    1. `retention_until <= now` — the original ADR-0001 hard boundary. A vault entry whose
       `retention_until` is still in the future is never selected via this path alone — there
       is no parameter anywhere in this module that can force it early.
    2. `original_path` re-occupied (ADR-0005) — independent of category and of
       `retention_until`, since a stale vault copy can never be usefully restored either way.
    """
    eligible: list[tuple[QuarantineManifestEntry, bool]] = []
    for entry in fold_latest_manifest_entries(manifest_path):
        if entry.method != "vault":
            continue
        if entry.restored or entry.purged:
            continue
        if _is_stale_original_reoccupied(entry):
            eligible.append((entry, True))
            continue
        if entry.retention_until is None:
            # Defensive: every `method="vault"` entry should carry a real `retention_until`
            # (only `direct_delete` entries ever have `None`) — logged and skipped rather than
            # crashing the whole purge run on a future manifest-writing bug.
            logger.warning(
                "purge.vault_entry_missing_retention_until", path=str(entry.original_path)
            )
            continue
        if entry.retention_until > now_ts:
            continue
        eligible.append((entry, False))
    return eligible


def _fresh_record_for_purge(entry: QuarantineManifestEntry) -> FileRecord:
    """Builds a `FileRecord` from the manifest entry's own recorded fields, not a live
    filesystem stat: by the time an entry is purge-eligible, `original_path` no longer exists
    (it was moved into the vault at apply time, days/weeks ago), so there is nothing left to
    re-stat at that path.

    `attributes=0`, `git_repo_root=None`, `git_repo_clean=False` are conservative "unknown"
    defaults — the same fail-closed posture `scanner.py` already uses when a real git check is
    unavailable. This is a real, valuable check: it catches config drift (a tightened
    `[safety] deny` pattern, a newly-protected extension added since the item was originally
    vaulted) purely from `original_path`/`size_bytes`/`is_dir`. What it deliberately CANNOT do,
    unlike the direct-delete re-check in `executor.py` (which re-derives real current git-repo
    membership from a live path), is detect that the original location became a git repo (or
    changed clean/dirty state) since vaulting — that information no longer exists anywhere once
    the path is gone. This limitation is documented here rather than silently pretended away.
    """
    return FileRecord(
        path=entry.original_path,
        is_dir=entry.is_dir,
        size_bytes=entry.size_bytes,
        attributes=0,
        ext=entry.original_path.suffix.lower() if not entry.is_dir else "",
        git_repo_root=None,
        git_repo_clean=False,
    )


def _vault_disk_usage_anchor(vault_dir: Path) -> Path | None:
    """Purge only ever removes bytes from the vault directory's own drive (never the
    original path, which is long gone by the time an entry is purge-eligible) — simpler than
    `executor._disk_usage_anchor`'s candidate-scanning fallback logic, since there is exactly
    one relevant drive here."""
    drive = vault_dir.resolve().drive
    return Path(f"{drive}\\") if drive else None


def _measure_disk_free(anchor: Path | None) -> int | None:
    if anchor is None:
        return None
    try:
        return shutil.disk_usage(anchor).free
    except OSError:
        return None


def _category_breakdown(items: Sequence[PurgeItemResult]) -> dict[str, CategoryBreakdown]:
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


def purge_expired(
    *,
    apply: bool = False,
    manifest_path: Path | None = None,
    vault_dir: Path | None = None,
    safety: SafetyValidator,
    now: float | None = None,
    only_rebuildable: bool = False,
    mode: Mode = Mode.POWER,
) -> PurgeReport:
    """Permanently deletes vaulted items (`method="vault"` manifest entries) whose retention
    window has passed, OR whose `original_path` is now stale (ADR-0005: re-occupied by
    something else, almost always a regenerated cache — see `_is_stale_original_reoccupied`).
    Never touches anything at an entry's `original_path` itself — by the time an entry is
    purge-eligible via either path, that vaulted copy is the only thing left to purge (ADR-0001).

    `mode` defaults to `Mode.POWER` (preserves every existing caller's behavior unchanged);
    real end-user entry points must pass the live mode explicitly. Stage 2 safety boundary:
    when `mode` is `Mode.SAFE`, this function raises `SafeModeViolationError` immediately —
    before reading the manifest, before any eligibility scan, before any I/O — unconditionally,
    regardless of whether the vault entries it would otherwise process came from a prior
    power-mode session. Purge is *always* a permanent-delete operation on whatever it touches
    (there is no "purge to Recycle Bin" — the item is already a vault copy, its only fate here
    is gone or not-yet-eligible), so safe mode forbids it outright rather than trying to make
    it conditionally safe. In practice this is also structurally redundant with `executor.
    apply_batch`'s safe-mode routing (safe-mode applies only ever create `method="recycle_bin"`
    manifest entries, never `"vault"` ones, so a safe-mode-only manifest has nothing this
    function would ever select anyway) — this explicit refusal closes the remaining case: a
    machine with real `vault` entries from an earlier power-mode session, later switched back
    to safe mode.

    Dry-run is the default (`apply=False`): makes zero mutating filesystem calls — no
    `unlink`/`rmtree`, no manifest writes, no disk-usage measurement — and reports what would
    be purged without touching anything.

    Hard boundary for the retention-expiry path, unconditional regardless of `apply`: an entry
    whose `retention_until` is still in the future is never selected via that path alone — there
    is no parameter that can force it. The stale-original-reoccupied path (ADR-0005) is
    independent of `retention_until` by design (see `_purge_eligible_entries`).

    `only_rebuildable=True` further restricts eligibility to entries whose `category_group` is
    in `REBUILDABLE_CATEGORY_GROUPS` (ADR-0005) — a scoped purge that never touches a
    `model_caches`/`duplicates`/other vault entry even if one happened to also be eligible.

    Mandatory pre-purge safety re-check (ADR-0001, same whole-run-abort philosophy as
    `executor.apply_batch`'s direct-delete re-check): every purge-eligible entry is re-evaluated
    against a `FileRecord` reconstructed from the manifest's own recorded fields (see
    `_fresh_record_for_purge` for what this can and can't catch) using `safety` — which must be
    built from the *live* config, there is no default. Any single fresh `Verdict.BLOCKED`
    aborts the *entire* purge run, deleting nothing.
    """
    if mode == Mode.SAFE:
        raise SafeModeViolationError(
            "purge_expired was called with mode=Mode.SAFE — purge is always a permanent-delete "
            "operation and is unconditionally forbidden in safe mode, regardless of manifest "
            "content. Refusing before reading the manifest, deleting nothing."
        )

    resolved_manifest_path = manifest_path if manifest_path is not None else DEFAULT_MANIFEST_PATH
    resolved_vault_dir = vault_dir if vault_dir is not None else DEFAULT_VAULT_DIR
    now_ts = now if now is not None else time.time()

    eligible = _purge_eligible_entries(resolved_manifest_path, now_ts)
    if only_rebuildable:
        eligible = [
            (entry, stale)
            for entry, stale in eligible
            if entry.category_group in REBUILDABLE_CATEGORY_GROUPS
        ]

    blocked: list[str] = []
    for entry, _stale in eligible:
        result = safety.evaluate(_fresh_record_for_purge(entry))
        if result.verdict == Verdict.BLOCKED:
            blocked.append(f"{entry.original_path} ({result.reason_code})")
    if blocked:
        raise SafetyInvariantError(
            f"purge_expired's pre-purge safety re-check found {len(blocked)} purge-eligible "
            "vault entr(y/ies) that fail a FRESH SafetyValidator evaluation against the live "
            f"config — refusing the entire purge run, deleting nothing: {blocked[:5]}"
        )

    disk_free_before = (
        _measure_disk_free(_vault_disk_usage_anchor(resolved_vault_dir)) if apply else None
    )

    items: list[PurgeItemResult] = []
    updated_entries: list[QuarantineManifestEntry] = []
    for entry, stale in eligible:
        if entry.vault_path is None:
            # Unreachable in practice: `_purge_eligible_entries` only ever selects
            # `method="vault"` entries, which always carry a `vault_path`. Guards mypy's None
            # narrowing and, if manifest data were ever corrupted, fails loudly per-item rather
            # than crashing the whole purge run.
            items.append(
                PurgeItemResult(
                    original_path=entry.original_path,
                    vault_path=None,
                    category=entry.category,
                    category_group=entry.category_group,
                    size_bytes=entry.size_bytes,
                    succeeded=False,
                    error="manifest entry has method=vault but no vault_path recorded",
                    stale=stale,
                )
            )
            continue
        vault_path = entry.vault_path

        if not apply:
            items.append(
                PurgeItemResult(
                    original_path=entry.original_path,
                    vault_path=vault_path,
                    category=entry.category,
                    category_group=entry.category_group,
                    size_bytes=entry.size_bytes,
                    succeeded=True,
                    error=None,
                    stale=stale,
                )
            )
            continue

        try:
            if entry.is_dir:
                shutil.rmtree(long_path(vault_path), onexc=rmtree_clear_readonly)
            else:
                unlink_clear_readonly(long_path(vault_path))
        except OSError as exc:
            logger.warning("purge.item_failed", path=str(vault_path), error=str(exc))
            items.append(
                PurgeItemResult(
                    original_path=entry.original_path,
                    vault_path=vault_path,
                    category=entry.category,
                    category_group=entry.category_group,
                    size_bytes=entry.size_bytes,
                    succeeded=False,
                    error=str(exc),
                    stale=stale,
                )
            )
            continue

        items.append(
            PurgeItemResult(
                original_path=entry.original_path,
                vault_path=vault_path,
                category=entry.category,
                category_group=entry.category_group,
                size_bytes=entry.size_bytes,
                succeeded=True,
                error=None,
                stale=stale,
            )
        )
        updated_entries.append(entry.model_copy(update={"purged": True, "purged_at": now_ts}))

    if updated_entries:
        append_manifest_entries(resolved_manifest_path, updated_entries)

    disk_free_after = (
        _measure_disk_free(_vault_disk_usage_anchor(resolved_vault_dir)) if apply else None
    )
    disk_free_delta = (
        disk_free_after - disk_free_before
        if disk_free_before is not None and disk_free_after is not None
        else None
    )

    succeeded_items = [item for item in items if item.succeeded]
    failed_items = [item for item in items if not item.succeeded]
    stale_succeeded_items = [item for item in succeeded_items if item.stale]
    return PurgeReport(
        apply=apply,
        started_at=now_ts,
        finished_at=time.time(),
        items=tuple(items),
        files_processed=len(items),
        files_succeeded=len(succeeded_items),
        files_failed=len(failed_items),
        stale_count=len(stale_succeeded_items),
        stale_bytes=sum(item.size_bytes for item in stale_succeeded_items),
        bytes_freed=sum(item.size_bytes for item in succeeded_items),
        category_breakdown=_category_breakdown(items),
        disk_free_before_bytes=disk_free_before,
        disk_free_after_bytes=disk_free_after,
        disk_free_delta_bytes=disk_free_delta,
    )
