from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from reclaim.executor import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_VAULT_DIR,
    QuarantineManifestEntry,
    append_manifest_entries,
    long_path,
    read_manifest_entries,
)

logger = structlog.get_logger(__name__)

# ADR-0026: an orphaned intent (a `phase="intent"` entry with no later `done`/`aborted`/
# `needs_review` entry sharing its `intent_id`) is reconciled by stating real, freshly-`os.stat`'d
# filesystem truth against the two locations the intent's `operation`/`method` implies — never by
# trusting the manifest's own (unconfirmed) claim about what happened. See `_source_and_target`.
ReconciliationOutcome = Literal["completed", "aborted", "needs_review"]


@dataclass(frozen=True, slots=True)
class ReconciledItem:
    intent_id: str
    operation: str
    batch_id: str
    original_path: Path
    vault_path: Path | None
    outcome: ReconciliationOutcome
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    scanned_intents: int
    already_resolved: int
    reconciled: tuple[ReconciledItem, ...]


def _resolved_intent_ids(entries: list[QuarantineManifestEntry]) -> set[str]:
    """`intent_id`s that already have a later `done`/`aborted`/`needs_review` entry — i.e. every
    intent this function has already been told the outcome of, whether by the operation that
    wrote it (the normal case) or by a prior `reclaim recover` run (the crash-recovery case)."""
    resolved: set[str] = set()
    for entry in entries:
        if entry.phase != "intent" and entry.intent_id is not None:
            resolved.add(entry.intent_id)
    return resolved


def _source_and_target(entry: QuarantineManifestEntry) -> tuple[Path | None, Path | None]:
    """The two real filesystem locations to stat when reconciling an orphaned intent, chosen by
    `operation` (which action this intent was for) and, for `operation="apply"`, `method` (only
    `vault` has a real, checkable target — `recycle_bin`/`direct_delete` leave nothing on disk
    to point at, so `target` is `None` for those; reconciliation then classifies on the source's
    presence/absence alone, the degenerate one-location case of the same logic).

    `apply`: source=original_path (has the action ever touched it?), target=vault_path (did the
    quarantine copy land?). `restore`: inverted — source=vault_path (still there = not yet
    restored), target=original_path (now there = restored). `purge`: source=vault_path (the
    vaulted copy being permanently deleted), no target — a purge has no destination to check.
    """
    if entry.operation == "apply":
        target = entry.vault_path if entry.method == "vault" else None
        return entry.original_path, target
    if entry.operation == "restore":
        return entry.vault_path, entry.original_path
    if entry.operation == "purge":
        return entry.vault_path, None
    return None, None  # pragma: no cover -- Literal["apply","restore","purge"] is exhaustive


def _exists(path: Path | None) -> bool:
    return path is not None and os.path.exists(long_path(path))  # noqa: PTH110 -- \\?\, not Path


def _is_contained(path: Path, container: Path) -> bool:
    """Same zip-slip-equivalent check as `executor._is_contained` (kept as a small local copy
    rather than importing a private helper across modules) — used below exactly the way
    `executor.RestoreIntegrityError`'s pre-restore check uses it: a manifest entry's own claimed
    `vault_path` is untrusted data (this tool's own append-only "archive"), so before treating it
    as ground truth for a `"completed"` classification, verify it actually resolves inside the
    configured vault directory."""
    resolved_path = path.resolve()
    resolved_container = container.resolve()
    return resolved_path == resolved_container or resolved_container in resolved_path.parents


def _classify(entry: QuarantineManifestEntry, vault_dir: Path) -> tuple[ReconciliationOutcome, str]:
    """The four-way stat-based classification ADR-0026 specifies: source-only -> aborted (the
    action never ran); target-only -> completed (it ran, only the DONE record was lost to the
    crash); both or neither -> needs_review, never guessed. For an operation/method pair with no
    real target (`recycle_bin`/`direct_delete` applies, and every purge), there is only one
    location to check, so "target absent" degenerates to source-presence alone: source present
    -> aborted, source absent -> completed — still never a guess, since that IS the only
    externally-observable fact for those cases (see `RecycleBinRestoreUnsupportedError`'s
    docstring in executor.py: this tool has never had a programmatic handle on the Recycle Bin).

    A `vault_path` that doesn't resolve inside `vault_dir` is never trusted enough to synthesize
    a `"completed"`/`"aborted"` verdict from — flagged `needs_review` unconditionally instead,
    same zip-slip-equivalent posture as `executor.RestoreIntegrityError`.
    """
    if entry.vault_path is not None and not _is_contained(entry.vault_path, vault_dir):
        return (
            "needs_review",
            f"recorded vault_path {entry.vault_path} does not resolve inside the configured "
            f"vault directory {vault_dir} — refusing to auto-classify, resolve by hand",
        )

    source, target = _source_and_target(entry)
    source_exists = _exists(source)

    if target is None:
        if source_exists:
            return "aborted", f"source {source} still present — the action never executed"
        return (
            "completed",
            f"source {source} is gone — the action completed before the crash, only the "
            "DONE record was lost",
        )

    target_exists = _exists(target)
    if source_exists and not target_exists:
        return "aborted", f"source {source} still present, target {target} was never created"
    if target_exists and not source_exists:
        return (
            "completed",
            f"source {source} is gone and target {target} exists — the action completed "
            "before the crash, only the DONE record was lost",
        )
    if source_exists and target_exists:
        return (
            "needs_review",
            f"both source {source} and target {target} exist — ambiguous (e.g. a "
            "cross-volume copy-fallback crashed after the copy but before the source was "
            "removed); resolve by hand, do not guess",
        )
    return (
        "needs_review",
        f"neither source {source} nor target {target} exists — cannot determine what "
        "happened to this item; resolve by hand, do not guess",
    )


def _resolving_entry(
    entry: QuarantineManifestEntry, outcome: ReconciliationOutcome, now_ts: float
) -> QuarantineManifestEntry:
    if outcome == "completed":
        if entry.operation == "restore":
            return entry.model_copy(
                update={"phase": "done", "restored": True, "restored_at": now_ts}
            )
        if entry.operation == "purge":
            return entry.model_copy(update={"phase": "done", "purged": True, "purged_at": now_ts})
        return entry.model_copy(update={"phase": "done"})
    if outcome == "aborted":
        return entry.model_copy(update={"phase": "aborted"})
    return entry.model_copy(update={"phase": "needs_review"})


def _scan(
    manifest_path: Path,
) -> tuple[int, int, list[QuarantineManifestEntry]]:
    """Returns (scanned_intents, already_resolved, orphaned_intent_entries)."""
    entries = read_manifest_entries(manifest_path)
    resolved_ids = _resolved_intent_ids(entries)
    intents = [e for e in entries if e.phase == "intent"]
    orphaned = [e for e in intents if e.intent_id is not None and e.intent_id not in resolved_ids]
    already_resolved = len(intents) - len(orphaned)
    return len(intents), already_resolved, orphaned


def compute_reconciliation(
    manifest_path: Path | None = None,
    vault_dir: Path | None = None,
) -> ReconciliationReport:
    """Read-only preview: classifies every orphaned intent against real on-disk state, but
    writes nothing. Safe to call from the dashboard on every page load — pure `os.stat` reads,
    no manifest mutation, so repeated calls are side-effect-free and always reflect current
    reality (an intent one call classified `needs_review` might classify `completed` a moment
    later if the user manually finished the move by hand — this function has no memory of its
    own past calls, only `reconcile_manifest`'s writes do). Classification is purely presence/
    absence-based, so there is no `now` parameter to fake a clock for — nothing here is
    time-dependent."""
    resolved_manifest_path = manifest_path if manifest_path is not None else DEFAULT_MANIFEST_PATH
    resolved_vault_dir = vault_dir if vault_dir is not None else DEFAULT_VAULT_DIR
    scanned, already_resolved, orphaned = _scan(resolved_manifest_path)

    reconciled: list[ReconciledItem] = []
    for entry in orphaned:
        outcome, detail = _classify(entry, resolved_vault_dir)
        if entry.intent_id is None:  # unreachable: _scan only returns entries with an intent_id
            raise RuntimeError("compute_reconciliation: orphaned entry with no intent_id")
        reconciled.append(
            ReconciledItem(
                intent_id=entry.intent_id,
                operation=entry.operation,
                batch_id=entry.batch_id,
                original_path=entry.original_path,
                vault_path=entry.vault_path,
                outcome=outcome,
                detail=detail,
            )
        )
    return ReconciliationReport(
        scanned_intents=scanned, already_resolved=already_resolved, reconciled=tuple(reconciled)
    )


def reconcile_manifest(
    manifest_path: Path | None = None,
    vault_dir: Path | None = None,
    *,
    now: float | None = None,
) -> ReconciliationReport:
    """Same classification as `compute_reconciliation`, but appends a resolving manifest entry
    (fsynced, one per orphaned intent) for every item it reconciles — `phase="done"` for a
    completed action (restoring `restored=True`/`purged=True` as appropriate so it folds back
    into normal current-state reads, see `executor.fold_latest_manifest_entries`), `"aborted"`
    for a never-executed one, `"needs_review"` for an ambiguous one. Idempotent: a second run
    finds these intents already resolved (their `intent_id` now has a matching non-intent entry)
    and reconciles nothing further for them — including `needs_review` ones, which stay flagged
    for a human rather than being re-classified automatically on every run."""
    resolved_manifest_path = manifest_path if manifest_path is not None else DEFAULT_MANIFEST_PATH
    now_ts = now if now is not None else time.time()
    preview = compute_reconciliation(resolved_manifest_path, vault_dir)

    for item in preview.reconciled:
        logger.info(
            "recovery.item_reconciled",
            operation=item.operation,
            batch_id=item.batch_id,
            original_path=str(item.original_path),
            outcome=item.outcome,
        )

    if preview.reconciled:
        # Re-fetch the actual orphaned entries (preview only carries summary fields) to build
        # full resolving records — a second pass over the same manifest, acceptable here since
        # `reclaim recover` is an explicit, infrequent maintenance operation, not the hot path
        # `_append_and_sync`'s per-item fsync cost was measured for.
        entries = read_manifest_entries(resolved_manifest_path)
        by_intent_id = {
            e.intent_id: e for e in entries if e.phase == "intent" and e.intent_id is not None
        }
        resolving = [
            _resolving_entry(by_intent_id[item.intent_id], item.outcome, now_ts)
            for item in preview.reconciled
            if item.intent_id in by_intent_id
        ]
        append_manifest_entries(resolved_manifest_path, resolving)

    return preview
