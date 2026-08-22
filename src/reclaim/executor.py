from __future__ import annotations

import json
import msvcrt
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TextIO

import send2trash
import structlog
from pydantic import BaseModel, ConfigDict, Field

from reclaim.app_paths import data_root
from reclaim.index import ScanIndex
from reclaim.models import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    Candidate,
    FileRecord,
    Mode,
    Tier,
    Verdict,
)
from reclaim.preflight import PreflightSkipReason as PreflightSkipReason  # re-exported; api/

# schemas.py imports this type from here rather than reaching into `reclaim.preflight` directly,
# same convention as this module's own `long_path` re-export below.
from reclaim.preflight import (
    check_file_in_use,
    check_hardlink_shared_active_install,
    check_identity_unchanged_since_scan,
    check_within_allowed_scope,
    enumerate_directory_identity,
)
from reclaim.safety import SafetyValidator
from reclaim.scanner import GitRepoCache, build_record_for_path
from reclaim.scanner import long_path as long_path  # re-exported; see D12 note below

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

# Anchored via reclaim.app_paths.data_root (see PR #51 for the original confirmed-live crash
# this class of bug caused elsewhere): CWD-independent when compiled -- the frozen build now
# anchors to the real exe's directory instead of an arbitrary launch CWD. Dev/test resolution is
# deliberately UNCHANGED (still lazily CWD-relative, exactly like the original bare
# `Path("data/...")` literal -- data_root()'s own docstring explains why eager `Path.cwd()`
# capture would silently break `monkeypatch.chdir(tmp_path)`-based test isolation). Not yet
# reachable from any working-directory-less invocation today, but "not reachable today" is a
# property of today's call sites, not of the code.
DEFAULT_VAULT_DIR = data_root() / "data" / "quarantine"
DEFAULT_MANIFEST_PATH = DEFAULT_VAULT_DIR / "manifest.jsonl"
_SECONDS_PER_DAY = 86400.0

# --- Progress feedback (fix/apply-progress-feedback) ------------------------------------------
#
# ADR-0026's per-item fsync cost (measured ~9x slower than a flush-only baseline — see that
# ADR's "Measured fsync cost" section) turns a large apply/restore/purge into a multi-minute
# operation with previously zero visible feedback in either the CLI or the dashboard — a real UX
# hazard (looks hung, invites a frustrated mid-batch kill) that this section exists to close.

_HEARTBEAT_INTERVAL_SECONDS = 5.0


def _due(*, last: float, now: float, interval: float) -> bool:
    """Pure predicate behind the progress-heartbeat gate — mirrors `dedup._due`'s exact
    convention (this codebase's established interval-gated-logging pattern). Duplicated here
    (rather than imported) since `dedup.py` doesn't export it and a 2-line pure predicate isn't
    worth a shared-utils module for its second use (house rule: duplicate twice, abstract on the
    third occurrence) — `purge.py` imports THIS copy rather than adding a third."""
    return (now - last) >= interval


ProgressCallback = Callable[[int, int, str], None]
"""`(items_processed, items_total, current_category) -> None`. Optional hook threaded through
`apply_batch`/`restore_batch`/`purge_expired`'s per-item loop, invoked at the SAME interval-gated
cadence as the `executor.*_progress`/`purge.progress` structlog line each function already emits
(never per-item — ADR-0026's fsync cost makes a real per-item callback call needlessly expensive
on top of the fsync itself, and per-item logging would spam a fast batch). Lets a caller (the
CLI's stdout heartbeat printer, the dashboard's background task updating `AppState`) observe the
same real, monotonic-time-gated progress this module already logs, without polling structlog
output itself."""

# ADR-0026's own "Measured fsync cost" table (docs/architecture/adr/0026-crash-safe-two-phase-
# manifest.md), WITH-fsync row, measured 2026-07-24 via scripts/bench_fsync.py against a real
# 23,000-item synthetic vault-apply on this development machine (Windows 11, local NVMe SSD) —
# not a guessed constant (house rule 65b: metric provenance). Re-derive this from a fresh
# `scripts/bench_fsync.py` run (and update the ADR) rather than hand-tuning it if it goes stale.
_MEASURED_MS_PER_ITEM_WITH_FSYNC = 8.088

# Below this, an apply/restore/purge feels close to instant regardless of item count; above it, a
# user watching a blank terminal or an unresponsive-looking dashboard could plausibly conclude the
# process hung — exactly the UX hazard the pre-apply time estimate (CLI/dashboard) exists to head
# off before that user kills the process mid-batch.
_LARGE_BATCH_WARNING_SECONDS = 10.0


def estimate_batch_seconds(item_count: int) -> float:
    """Real, measured estimate (never a guess) of how long an `apply_batch`/`restore_batch`/
    `purge_expired` run of `item_count` items will take, derived from ADR-0026's measured
    per-item fsync cost."""
    return item_count * _MEASURED_MS_PER_ITEM_WITH_FSYNC / 1000.0


def should_warn_about_batch_duration(item_count: int) -> bool:
    """True once `estimate_batch_seconds` crosses `_LARGE_BATCH_WARNING_SECONDS` — the threshold
    the CLI/dashboard use to decide whether a pre-apply time estimate is worth showing at all (a
    5-item apply doesn't need one)."""
    return estimate_batch_seconds(item_count) >= _LARGE_BATCH_WARNING_SECONDS


class SafetyInvariantError(RuntimeError):
    """Raised by `apply_batch` when it is handed a BLOCKED candidate.

    Every `Candidate` reaching this module should already have passed `SafetyValidator` in
    Stage 3/4's `generate_candidates`/`generate_duplicate_candidates` — this is a last line of
    defense, not redundant paranoia. Hitting it means an invariant was violated upstream, so the
    whole batch is refused rather than silently dropping the offending item and continuing.
    """


class SafeModeViolationError(RuntimeError):
    """Raised by `apply_batch`/`purge.purge_expired` when the live mode is `Mode.SAFE` and the
    call would otherwise reach a permanent-delete or non-Recycle-Bin code path.

    Stage 2's safety boundary: unlike `SafetyInvariantError` (a BLOCKED candidate slipped
    through upstream filtering — should never happen, defense in depth) this is the EXPECTED,
    routine outcome of a caller misusing the API while safe mode is active — e.g. requesting
    `method="vault"` — never a sign of a bug. Refuses the entire call before any filesystem
    mutation, same as every other whole-call refusal in this module.
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


class RestoreIntegrityError(RuntimeError):
    """Raised by `restore_batch` before it moves anything, if any vault entry's manifest data
    fails a structural integrity check: its recorded `vault_path` doesn't resolve inside the
    configured vault directory, or its `original_path` matches a protected system root.

    Neither of these should ever happen from this tool's own normal operation — `apply_batch`
    always computes `vault_path` under `vault_dir` (see `_compute_vault_path`) and never vaults
    a `SafetyValidator`-BLOCKED candidate in the first place. Either condition strongly suggests
    a corrupted or hand-edited `manifest.jsonl`, so this is the restore-side equivalent of a
    zip-slip guard: `manifest.jsonl` is this tool's own append-only "archive," `vault_path` is
    the analog of a zip member's path, and `original_path` is the analog of the extraction
    target — the same "never trust the archive's own member paths, always re-verify against the
    intended root" principle applies here. Refuses the ENTIRE restore call rather than skipping
    just the offending entry, mirroring `SafetyInvariantError`'s "something is fundamentally
    wrong, do nothing" philosophy.
    """


ManifestPhase = Literal["intent", "done", "aborted", "needs_review"]
ManifestOperation = Literal["apply", "restore", "purge"]

# ADR-0027 (schema versioning), corrected 2026-08-20 audit P0-4: version 1 was originally defined
# as "every field this class has today, including ADR-0026's `phase`/`intent_id`/`operation`" —
# which was meant to include the still-older ADR-0001 fields (`is_dir`, `rebuild_instruction`,
# `retention_days`) too, but those three were left *required* with no default, so a manifest line
# genuinely older than all of them (predating ADR-0001, not just ADR-0026/ADR-0027) hard-crashed
# `read_manifest_entries` instead of parsing as "version 1" the way the ADR promised. Version 2
# is the fix: those three fields now have real defaults (see the class body), so a pre-ADR-0001
# line finally parses the way ADR-0027 always intended pre-versioning data to. Bump this whenever
# a field is added/removed/changed and update `read_manifest_entries` if a migration step is ever
# needed; the one existing fresh-construction call site (`apply_batch`'s intent-entry) passes this
# constant explicitly rather than relying on the field's own default, precisely so the two can
# diverge (see `QuarantineManifestEntry.schema_version`'s own comment for why).
QUARANTINE_MANIFEST_SCHEMA_VERSION = 2


class QuarantineManifestEntry(BaseModel):
    """One line in the append-only `data/quarantine/manifest.jsonl` log.

    The manifest is an event log, not a snapshot table: `apply_batch` appends one entry per
    quarantined item, and `restore_batch`/`purge_expired` append a second entry per updated
    item (same `batch_id`/`original_path` key, `restored`/`purged` fields set) rather than
    rewriting history in place. Readers fold to "current state" by taking the last `phase="done"`
    entry per `(batch_id, original_path)` key — see `fold_latest_manifest_entries`.

    ADR-0026 (crash-safe two-phase manifest): every filesystem-mutating action (`apply_batch`'s
    quarantine, `restore_batch`'s restore, `purge_expired`'s permanent delete) writes an
    `phase="intent"` entry (fsynced) BEFORE touching the filesystem, then a `phase="done"` entry
    (fsynced) after — or `phase="aborted"` if the action raised and was caught. `intent_id` pairs
    an intent with its eventual done/aborted/needs_review entry across the two writes (never
    reused across different operations, even for the same `(batch_id, original_path)` key).
    `phase` defaults to `"done"` and `intent_id`/`operation` are optional, so every manifest line
    written before this ADR (which had no intent/done split at all — the whole batch's action
    already completed by the time anything was written) parses and folds exactly as before: an
    old-format line has no way to be an orphaned intent, so defaulting it to `"done"` is not an
    approximation, it's the literal truth for that line. A kill between the intent write and the
    done/aborted write leaves an intent with no matching resolution — `reclaim.recovery` finds
    these by replaying the raw manifest and reconciles each one against real on-disk state.

    ADR-0027 (schema versioning): `extra="allow"` (not `"forbid"`/`"ignore"`) is deliberate —
    entries are re-serialized after being read (`restore_batch`/`purge_expired`/`reclaim.recovery`
    all call `entry.model_copy(update=...)` then re-`model_dump_json()` the result), so a field
    this version of the code doesn't recognize (written by a newer release) must round-trip
    through that read-modify-write cycle unchanged rather than being silently dropped —
    `extra="ignore"` would discard it right there, a real data-loss bug distinct from the crash
    this ADR primarily fixes. `read_manifest_entries` logs (never raises) when it sees a
    `schema_version` newer than `QUARANTINE_MANIFEST_SCHEMA_VERSION`.

    Schema v2 (audit P0-4, 2026-08-20): the backward direction — an entry OLDER than this code,
    missing `is_dir`/`rebuild_instruction`/`retention_days` — is handled the same way: real
    per-field defaults (see each field's own comment for why the chosen default is safe) plus an
    explicit, loggable migration event from `read_manifest_entries`, never a silent default alone.
    """

    model_config = ConfigDict(extra="allow")

    batch_id: str
    original_path: Path
    size_bytes: int
    # ADR-0001: `purge_expired` needs to know whether a purge target is a file or a directory
    # (`Path.unlink()` vs `shutil.rmtree()`) without re-stat'ing `original_path`, which by the
    # time an entry is purge-eligible no longer exists.
    #
    # Schema v2 (audit P0-4, 2026-08-20): defaulted to `False` for a manifest line written before
    # this field existed. This is a deliberate fail-*safe*, not merely a guess: `purge_expired`
    # (`purge.py`) branches on this bit to choose `unlink()` vs `rmtree()` on the *vault* copy. A
    # wrong `True`-should-be-`False` default just means an already-empty rmtree call on a file,
    # which errors loudly; a wrong `False`-should-be-`True` default means `unlink()` is attempted
    # on a real directory, which Windows also refuses with a loud `OSError` — caught, logged as
    # `purge.item_failed`, the entry stays un-purged for a human to look at again next run. Neither
    # wrong-default path silently deletes the wrong thing or corrupts data; both fail loud and
    # non-destructively, which is why a static default (rather than re-deriving it from the vault
    # path's live `is_dir()`, itself unreliable once an entry has since been restored/purged) is
    # an acceptable, simple choice here — see `read_manifest_entries` for the explicit, per-entry
    # migration log this default triggers.
    is_dir: bool = False
    category: str
    category_group: str
    rationale: str
    # ADR-0001: the only "recovery" a direct-deleted (or later-purged) item has — recorded for
    # every entry, not just direct-delete ones, so the manifest stays one uniform shape.
    #
    # Schema v2 (audit P0-4): defaulted to `None` for a pre-existing line — this is the literal
    # truth, not a guess: no rebuild instruction was ever recorded for this entry, and `None` is
    # exactly what a `direct_delete` entry already uses today to mean "no instruction applies."
    # Consumers (`cli.py`, `app.js`) already treat `None` as "nothing to show," so this default is
    # inert, never a fabricated recovery instruction presented as if it were real.
    rebuild_instruction: str | None = None
    tier: Tier
    method: QuarantineMethod
    vault_path: Path | None
    # ADR-0001: resolved from `Candidate.retention_days` at quarantine time. `None` for a
    # `direct_delete` entry (there is no retention window; nothing was vaulted).
    #
    # Schema v2 (audit P0-4): defaulted to `None` for a pre-existing line. Verified this is inert,
    # not silently wrong: no code path reads `QuarantineManifestEntry.retention_days` after
    # quarantine time (grep-confirmed) — only `retention_until` (a separate, always-populated
    # field even on old entries, since the pre-this-field code populated it from a project-wide
    # 30-day default) governs purge eligibility. `retention_days` on an already-written entry is
    # purely informational.
    retention_days: int | None = None
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
    # ADR-0026: see the class docstring. `phase="done"` is the only phase that participates in
    # `fold_latest_manifest_entries`'s "current state" view (see that function) — intent/aborted/
    # needs_review entries are visible only to `reclaim.recovery`'s raw-manifest replay.
    phase: ManifestPhase = "done"
    intent_id: str | None = None
    operation: ManifestOperation = "apply"
    # ADR-0027: absent (pre-versioning) entries validate with this field defaulting to `1` —
    # the literal truth, not an approximation, since `1` is the version every field ADR-0027 knew
    # about at the time belonged to.
    #
    # Schema v2 correction (audit P0-4): this default is deliberately the literal `1`, NOT
    # `QUARANTINE_MANIFEST_SCHEMA_VERSION` (now `2`) — the two must be allowed to diverge. "No
    # `schema_version` key in the source data" is a fact about the *data* (it predates versioning,
    # so `1` is its honest historical version), while `QUARANTINE_MANIFEST_SCHEMA_VERSION` is a
    # fact about *this code's* current known version. Before this correction the two constants
    # were the same value, which accidentally made this field's default double as "current version
    # for a freshly-constructed entry" too — that stops being safe the first time the constant
    # is ever bumped, so `apply_batch`'s one fresh-construction call site now passes
    # `schema_version=QUARANTINE_MANIFEST_SCHEMA_VERSION` explicitly instead of relying on this
    # default. A future field addition bumps `QUARANTINE_MANIFEST_SCHEMA_VERSION` (not this
    # literal) and `read_manifest_entries` warns (never raises) on any entry whose recorded
    # version is newer than the code knows.
    schema_version: int = Field(default=1)


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
    # Audit P0-1 (docs/AUDIT-2026-08.md): `None` for every existing outcome shape (a genuine
    # success, or a failure where `error` carries the real exception message from an ATTEMPTED
    # mutation). Set to a `PreflightSkipReason` only when `apply_batch`'s pre-flight probe found
    # a reason to skip this item WITHOUT ever attempting the move/delete at all -- distinct from
    # `error`, which always means "we tried and the OS/filesystem rejected it". `succeeded` is
    # still `False` for a skipped item (it did not happen), but callers that need to tell "never
    # attempted" apart from "attempted and failed" should check this field, not just `error`.
    skip_reason: PreflightSkipReason | None = None
    # ADR-0032: `True` only for a guard-downgraded (entry-count or byte-size), rebuildable,
    # `retention_days=0` candidate whose vault copy was ALSO successfully purged back out again,
    # synchronously, within this same `apply_batch` call — see that function's docstring. `False`
    # (the default) for every other outcome, including a `retention_days=0` item whose
    # synchronous purge attempt itself failed (it stays validly vaulted, `succeeded` is still
    # `True` for the underlying vault move, just not yet purged — a future `reclaim purge` run
    # will pick it up like any other purge-eligible entry).
    synchronously_purged: bool = False
    # K2a (audit finding): `True` when this item's underlying move/delete call raised NO
    # exception at all, but `_verify_apply_postcondition`'s real, fresh on-disk check afterward
    # found the mutation didn't actually happen (or only partially happened) — distinct from
    # `error` carrying a message from an ATTEMPTED mutation the OS/filesystem itself rejected.
    # `error` is still populated (with `_verify_apply_postcondition`'s own message) for this case
    # too, so any caller that only ever checked `error is not None` for "something's wrong" keeps
    # working — this field exists so a caller that specifically needs to tell "the OS said no"
    # apart from "the OS silently did nothing" (K2b's reproduced `shutil.rmtree`/junction case is
    # the motivating real-world example, but this check is deliberately root-cause-independent)
    # can do so. Always `False` when `succeeded` is `True`.
    postcondition_verification_failed: bool = False


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
    # ADR-0032: subset of `files_succeeded`/`bytes_freed` that was ALSO synchronously purged
    # back out of the vault within this same call — see `ItemApplyResult.synchronously_purged`.
    # Reported separately, mirroring `purge.PurgeReport.stale_count`/`stale_bytes`'s pattern, so
    # a caller never has to infer "were these bytes actually freed yet" from `disk_free_delta_
    # bytes` alone. `0`/`0` for every existing caller/batch that never triggers a guard downgrade.
    synchronously_purged_count: int = 0
    bytes_synchronously_purged: int = 0


@dataclass(frozen=True, slots=True)
class RestoreItemResult:
    original_path: Path
    size_bytes: int
    succeeded: bool
    already_restored: bool
    error: str | None
    # True for a `direct_delete`/`recycle_bin` entry sharing a batch_id with at least one
    # restorable `vault` entry — this item was never going to be restorable regardless of what
    # else happens in the batch, distinct from a genuine operational failure (a permission
    # error, a destination collision) that's actually worth investigating. Always `False` when
    # `succeeded` is `True`.
    restore_unsupported: bool = False


@dataclass(frozen=True, slots=True)
class RestoreReport:
    batch_id: str
    started_at: float
    finished_at: float
    items: tuple[RestoreItemResult, ...]
    files_processed: int
    files_succeeded: int
    # Count of entries where restore was attempted and genuinely failed (a real operational
    # problem) — deliberately excludes `restore_unsupported` items, which never had a restore
    # attempted at all. See `files_unsupported`.
    files_failed: int
    files_unsupported: int
    bytes_restored: int


def _compute_vault_path(vault_dir: Path, batch_id: str, original_path: Path) -> Path:
    """Unique-per-item vault location. `restore_batch` always moves a file back using the
    manifest's stored `original_path`, so the vault side never needs to mirror the original
    directory structure — a flat, collision-proof name (random prefix + original filename) is
    simpler and sufficient.
    """
    return vault_dir / batch_id / f"{uuid.uuid4().hex}_{original_path.name}"


def _require_vault_path(vault_path: Path | None) -> Path:
    """Narrows `vault_path` for the `method=="vault"` branch of `apply_batch`'s per-item loop,
    where it is always already computed — unreachable in practice, but a real `raise` (not an
    `assert`, which strips under `-O`) rather than trusting the None-check silently."""
    if vault_path is None:
        raise RuntimeError("apply_batch: vault method with no vault_path computed")
    return vault_path


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
#
# D12: `long_path` itself now lives in `reclaim.scanner` (re-exported here for backward
# compatibility, since `purge.py`/this module's own tests import it as `reclaim.executor.
# long_path`) — the scan walk hit the exact same MAX_PATH gap this ADR describes for the vault
# path, and needed the identical primitive. `scanner.py` doesn't import from this module (this
# module already imports FROM `scanner.py`, for `GitRepoCache`/`build_record_for_path`), so
# moving the shared helper to the lower-level module avoids a circular import.


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


def rmtree_clear_readonly(func: Callable[[str], object], path: str, exc: BaseException) -> None:
    """`shutil.rmtree`'s `onexc` callback: clears the read-only attribute on `path` and retries
    the operation that failed (ADR-0004 addendum, discovered in production).

    Git deliberately marks every packfile/loose-object file read-only on disk — a real vaulted
    directory containing so much as one `.git` directory (the 2026-07-17 re-apply's `Temp\\
    claude` scratch tree, itself full of cloned repos, is exactly this shape) hits Windows'
    "Access is denied" when `shutil.rmtree` tries to `os.unlink`/`os.rmdir` a read-only file
    without this handler — a well-known Python stdlib gotcha on Windows, not exotic to this
    codebase. Silently swallowing this with `ignore_errors=True` (the first version of this
    fix) left up to dozens of read-only git-object files behind as genuinely orphaned vault
    debris after a real production run — exactly the failure mode ADR-0004 exists to prevent.
    Every `shutil.rmtree` call in this module (and `purge.py`'s) uses this `onexc` handler.
    """
    os.chmod(path, stat.S_IWRITE)  # noqa: PTH101 -- \\?\ str, not Path; see module note above
    func(path)


def unlink_clear_readonly(path: str) -> None:
    """Deletes a single file, clearing the read-only attribute first on retry if needed (ADR-0004
    addendum) — the same read-only-file gotcha `rmtree_clear_readonly` handles for directory
    trees (git packfiles/loose objects, but any read-only file hits this identically), just for
    a standalone `os.unlink` call, which has no built-in `onexc`/retry hook of its own to hang a
    handler off of the way `shutil.rmtree` does — so this wraps the retry manually instead.
    """
    try:
        os.unlink(path)  # noqa: PTH108 -- \\?\ str, not Path; see module note above
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)  # noqa: PTH101
        os.unlink(path)  # noqa: PTH108


# --- K2b: shutil.rmtree's own junction-attack guard is silently defeated by this module's own
# long_path()/onexc conventions ------------------------------------------------------------------
#
# `shutil.rmtree` refuses to recurse into a symlink/junction handed to it as its OWN top-level
# argument -- it detects this internally (an `os.path.islink`-based check) and raises before doing
# anything. But every `shutil.rmtree` call in this module (and `purge.py`'s) is made with
# `onexc=rmtree_clear_readonly` (ADR-0004's read-only-file retry handler, needed for git
# packfiles/loose objects) -- and `rmtree_clear_readonly`'s own contract is "chmod the path, then
# retry whatever `func` `shutil.rmtree` handed us". For the junction-guard's own internal raise,
# the `func` `shutil.rmtree` hands `onexc` is `os.path.islink` itself (a read-only probe, not a
# delete) -- so `rmtree_clear_readonly` chmods a reparse point (a harmless no-op) and re-invokes
# `islink`, discards its boolean return value, and `shutil.rmtree` returns NORMALLY having deleted
# nothing at all. No exception ever reaches the caller. Reproduced directly against this
# interpreter's real `shutil.rmtree` (not merely inferred): confirmed live against a real `mklink
# /J` junction, both with and without the `\\?\` long-path prefix -- the long-path prefix is not
# actually load-bearing to the bug (this interpreter's `shutil.rmtree` already detects a bare-path
# junction as a symlink too), but IS load-bearing to why `onexc=rmtree_clear_readonly` swallows it:
# without an `onexc` handler at all, the same call raises loudly instead of returning silently.
# See `evals/test_apply_identity_reverify.py`'s K2d teeth-proof for the full reproduction.
#
# Nested reparse points (a junction somewhere INSIDE a real directory `shutil.rmtree` is walking,
# rather than the top-level argument itself) are unaffected by this bug -- verified empirically:
# `shutil.rmtree` correctly removes only the junction's own directory entry when it encounters one
# mid-walk, never recursing into what it points at, and correctly removes the rest of the real
# tree around it. The blind spot is exclusively the TOP-LEVEL argument's own identity.


def _is_reparse_point(path: str) -> bool:
    r"""True if `path` (already `\\?\`-prefixed) is itself a reparse point (an NTFS junction or a
    directory/file symlink) rather than a real file or directory -- the exact distinction K2b's
    fix needs before ever calling `shutil.rmtree` on a top-level path. Same
    `os.stat(..., follow_symlinks=False)` + `FILE_ATTRIBUTE_REPARSE_POINT` pattern already
    established in `scanner.build_record`/`preflight.py` -- reused here, not reinvented. A path
    that can no longer be stat'd at all (already gone) is reported `False`, not raised -- callers
    that care about existence separately use `_path_exists_no_follow`.
    """
    try:
        st = os.stat(path, follow_symlinks=False)  # noqa: PTH116 -- \\?\ str, not Path; see above
    except OSError:
        return False
    return bool(st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _path_exists_no_follow(path: str) -> bool:
    r"""True if a filesystem entry exists AT `path` itself (already `\\?\`-prefixed) -- a reparse
    point counts as existing even if its target is missing (a "dangling" junction/symlink).

    Deliberately NOT `os.path.exists`, which follows a reparse point through to its target and
    reports a dangling junction as absent even though its own directory entry is still physically
    present on disk (verified empirically) -- exactly the wrong answer for K2a's post-condition
    check, whose entire job is "did the delete/move actually remove the entry that was here",
    not "does whatever this entry currently resolves to still exist". Same
    `os.stat(..., follow_symlinks=False)` primitive as `_is_reparse_point`, for the same reason.
    """
    try:
        os.stat(path, follow_symlinks=False)  # noqa: PTH116 -- \\?\ str, not Path; see above
    except OSError:
        return False
    return True


def rmtree_reparse_point_safe(path: str) -> None:
    """K2b: the fix for `shutil.rmtree`'s silently-defeated junction guard (see this section's
    module comment above) -- every direct-delete/permanent-removal call site in this module (and
    `purge.py`'s) that could be handed a path that is ITSELF a reparse point must call this
    instead of `shutil.rmtree` directly.

    Checks `_is_reparse_point` FIRST: a reparse point is removed as a single directory-entry
    operation via `os.rmdir()` -- never recursed into, never touching whatever it points at. This
    is the correct, safe removal call for a junction/symlink itself (as opposed to recursively
    deleting into its target, which `shutil.rmtree` would do if it were ever tricked into treating
    the reparse point as an ordinary directory). Clears the read-only attribute and retries once
    on `PermissionError`, mirroring `unlink_clear_readonly`'s own retry shape -- a reparse point
    is not normally read-only, but nothing rules it out on a real, arbitrary user filesystem.

    Falls through to the original `shutil.rmtree(path, onexc=rmtree_clear_readonly)` recursive
    removal only once `_is_reparse_point` has confirmed `path` is a REAL directory, not a reparse
    point -- nested reparse points inside that real tree are unaffected by this fix (already
    handled correctly by `shutil.rmtree` on its own; see this section's module comment).
    """
    if _is_reparse_point(path):
        try:
            os.rmdir(path)  # noqa: PTH106 -- \\?\ str, not Path; see module note above
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)  # noqa: PTH101
            os.rmdir(path)  # noqa: PTH106
        return
    shutil.rmtree(path, onexc=rmtree_clear_readonly)


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
    long_src = long_path(src)
    long_dst = long_path(dst)
    dst_parent = os.path.dirname(long_dst)  # noqa: PTH120 -- str, not Path; see module note above
    parent_already_existed = os.path.isdir(dst_parent)  # noqa: PTH112
    os.makedirs(dst_parent, exist_ok=True)  # noqa: PTH103

    def _cleanup_dst_and_empty_parent() -> None:
        try:
            if os.path.exists(long_dst):  # noqa: PTH110
                if os.path.isdir(long_dst):  # noqa: PTH112
                    shutil.rmtree(long_dst, onexc=rmtree_clear_readonly)
                else:
                    unlink_clear_readonly(long_dst)
            if (
                not parent_already_existed
                and os.path.isdir(dst_parent)  # noqa: PTH112
                and not os.listdir(dst_parent)  # noqa: PTH208
            ):
                os.rmdir(dst_parent)  # noqa: PTH106
        except OSError as cleanup_exc:
            # Cleanup best-effort beyond the read-only-file retry above: a file genuinely
            # locked by another live process (rather than merely read-only) can still make
            # cleanup incomplete. Logged loudly rather than silently swallowed (the original
            # `ignore_errors=True` design this replaces) so leftover vault debris is at least
            # discoverable, never silent.
            logger.warning(
                "executor.vault_cleanup_incomplete", path=long_dst, error=str(cleanup_exc)
            )

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
        # K2b: `long_src` is the caller's own top-level path (`candidate.path`/a manifest entry's
        # `original_path`) -- exactly the shape that can itself be a reparse point (a directory
        # junction candidate routed to `vault` rather than `direct_delete`, then hitting this
        # cross-volume copy fallback). Plain `shutil.rmtree` would silently no-op on that case
        # (see this module's K2b section comment above `rmtree_reparse_point_safe`).
        rmtree_reparse_point_safe(long_src)
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
        unlink_clear_readonly(long_src)


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
    """Public: reused by `reclaim.recovery`'s reconciliation writes and by any remaining
    non-per-item batch append. Does NOT fsync — callers on the crash-safety-critical path
    (`apply_batch`/`restore_batch`/`purge_expired`'s per-item loops) use `_open_manifest_for_sync`
    and `_append_and_sync` instead; see ADR-0026."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(entry.model_dump_json())
            fh.write("\n")


_MANIFEST_LOCK_NBYTES = 1  # a single sentinel byte at a fixed offset (0) is locked as a pure
# mutual-exclusion token -- the manifest's real content at that offset is irrelevant. Same
# "lock byte" convention used by common Windows file-locking libraries (e.g. `portalocker`).
# Locking a FIXED offset (not "wherever the fd happens to be") means every opener -- regardless
# of how large the file already is when it opens -- contends for exactly the same byte.
_MANIFEST_LOCK_POLL_SECONDS = 0.25
_MANIFEST_LOCK_TIMEOUT_SECONDS = 1800.0  # audit C10 (second pass), point 5: bounds the wait
# instead of hanging forever if a lock is somehow never released (Windows releases byte-range
# locks automatically when the owning handle closes or the process exits, so this should only
# ever fire as a deep defensive bound, not an expected path) -- but the original 600s value was
# under-justified: ADR-0026's 186.02s/23,000-item measurement is the WORST case *recorded*, not
# a ceiling on how large or slow a legitimate real batch can get. A real production run already
# hit 23,565 items in one batch (see `restore_batch`'s docstring); a machine with several times
# that much accumulated clutter is a plausible, not hypothetical, next data point, and real-world
# antivirus interception of every per-item file-open/fsync/delete syscall (well documented on
# Windows) can multiply the measured 8.088ms/item several-fold without anything actually being
# stuck. At the measured per-item rate, 1800s covers roughly 220,000 items (~9.5x the largest
# recorded real batch) -- no fixed bound can cover an unbounded batch size times an unbounded
# per-item slowdown, so this is a deliberately generous, evidence-anchored policy choice, not a
# guarantee that no legitimate batch will ever exceed it; see the softened error message below,
# which no longer asserts the holder must be stuck.


class ManifestLockTimeoutError(RuntimeError):
    """Raised by `_open_manifest_for_sync` when another thread or process has held the
    `manifest.jsonl` write lock for longer than `_MANIFEST_LOCK_TIMEOUT_SECONDS`. Audit finding
    C10 (unsynchronized concurrent writes to `manifest.jsonl` across the dashboard's threadpool-
    dispatched background tasks and/or a CLI invocation racing a dashboard-triggered batch)."""


def _acquire_manifest_lock(fh: TextIO) -> None:
    """Audit finding C10: blocks (own bounded retry loop, not `msvcrt.locking`'s own hardcoded
    10-attempt/10-second `LK_LOCK` ceiling -- too short for the multi-minute batches ADR-0026
    measures) until an OS-level, cross-thread AND cross-process exclusive lock on
    `manifest.jsonl` is held by THIS file handle.

    `apply_batch`/`restore_batch`/`purge_expired` each hold this lock for their whole batch
    (acquired once here in `_open_manifest_for_sync`, released once when the handle closes via
    `_close_manifest_for_sync`) -- this is what serializes two batches (same-process background-
    task threads dispatched via FastAPI `BackgroundTasks`/`run_in_threadpool`, or two separate OS
    processes such as a CLI invocation racing the dashboard) writing to the same manifest file,
    closing the interleaved-partial-line corruption window that unsynchronized concurrent
    `_append_and_sync` calls would otherwise open. Windows byte-range locks (what `msvcrt.locking`
    wraps) are associated with the file HANDLE, not the process, so two handles to the same file
    genuinely contend even when opened by the same process on different threads -- a plain
    in-process `threading.Lock` alone would not cover the cross-process half of this race.

    `os.lseek` (not `fh.seek`) is used because `msvcrt.locking` locks bytes starting at the
    underlying OS file descriptor's *current position*, not a position passed as an argument;
    this must happen before anything is written through `fh`. Safe under 'a' (append) mode:
    append-mode writes always target end-of-file on Windows regardless of the fd's seek position
    (verified empirically against this exact open()/lseek()/write() sequence -- see the C10 fix
    commit), so seeking to byte 0 here to acquire the lock never affects where a later
    `_append_and_sync` write lands.
    """
    fd = fh.fileno()
    os.lseek(fd, 0, os.SEEK_SET)
    deadline = time.monotonic() + _MANIFEST_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _MANIFEST_LOCK_NBYTES)
        except OSError:
            if time.monotonic() >= deadline:
                raise ManifestLockTimeoutError(
                    "could not acquire the manifest.jsonl write lock within "
                    f"{_MANIFEST_LOCK_TIMEOUT_SECONDS:.0f}s -- another apply/restore/purge batch "
                    "is either still legitimately running (an exceptionally large batch, a slow "
                    "disk, or antivirus scanning every file operation can all push a real run "
                    "past this bound) or a process holding the lock is stuck. Check Task Manager "
                    "for a running reclaim/python process before assuming anything is wrong, and "
                    "retry once it has finished or been closed; audit finding C10"
                ) from None
            time.sleep(_MANIFEST_LOCK_POLL_SECONDS)
        else:
            return


def _release_manifest_lock(fh: TextIO) -> None:
    """Pair of `_acquire_manifest_lock` -- called only from `_close_manifest_for_sync`, never
    directly, so every `_open_manifest_for_sync` caller's matching close automatically releases
    the same byte range that was locked. Audit finding C10."""
    fd = fh.fileno()
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, _MANIFEST_LOCK_NBYTES)


def _open_manifest_for_sync(manifest_path: Path) -> TextIO:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fh = manifest_path.open("a", encoding="utf-8")
    _acquire_manifest_lock(fh)
    return fh


def _close_manifest_for_sync(fh: TextIO) -> None:
    """Pairs with `_open_manifest_for_sync` -- releases the OS-level write lock acquired there,
    then closes the handle. `apply_batch`/`restore_batch`/`purge_expired` all call this from
    their `finally:` blocks instead of `fh.close()` directly, so releasing the lock is never
    something a call site can forget. Audit finding C10."""
    _release_manifest_lock(fh)
    fh.close()


def _append_and_sync(fh: TextIO, entry: QuarantineManifestEntry) -> None:
    """Writes one manifest line and forces it to durable storage before returning (ADR-0026).

    `flush()` alone only moves the write from Python's buffer into the OS page cache — still
    lost on a power failure (though survives a plain process kill, since the OS keeps the page
    cache). `os.fsync(fh.fileno())` additionally forces the OS to write the page cache to the
    physical device, which is what makes the intent/done ordering below meaningful across a
    real crash, not just a caught exception. This is the per-item cost measured and reported in
    `docs/architecture/adr/0026-crash-safe-manifest.md` — see that ADR before changing this.
    """
    fh.write(entry.model_dump_json())
    fh.write("\n")
    fh.flush()
    os.fsync(fh.fileno())


# Schema v2 (audit P0-4): the fields that were, in practice, required-with-no-default from
# `QuarantineManifestEntry`'s introduction until this fix — a manifest line missing any of these
# predates all of ADR-0001/0026/0027 and is the oldest possible shape this codebase can still
# encounter on a real machine (`packaging/reclaim.iss` preserves `data/` across upgrades). Kept as
# a module-level tuple (not inlined in `read_manifest_entries`) so it's the one place to update if
# a future schema bump ever adds another field to this same "backward-defaulted" category.
_SCHEMA_V2_BACKWARD_DEFAULTED_FIELDS: tuple[str, ...] = (
    "is_dir",
    "rebuild_instruction",
    "retention_days",
)


def read_manifest_entries(manifest_path: Path) -> list[QuarantineManifestEntry]:
    """Public: reused by `purge.py` (via `fold_latest_manifest_entries`) and the API layer.

    ADR-0027: never raises on an entry written by a newer release — `QuarantineManifestEntry`'s
    `extra="allow"` already guarantees a field this version doesn't recognize parses fine (and
    round-trips if the entry is later re-serialized); this additionally logs a warning (once per
    call, listing every newer version actually seen) so a genuinely newer schema is visible in
    logs rather than silently absorbed.

    Schema v2 (audit P0-4): the mirror-image direction — an entry OLDER than this code, missing
    one or more of `_SCHEMA_V2_BACKWARD_DEFAULTED_FIELDS` — also never raises (the fields now have
    real defaults; see `QuarantineManifestEntry`), but unlike the pydantic-level default alone,
    this is explicit and loggable: each such entry gets its own `structlog.info` line naming which
    fields were missing and what they were defaulted to, so a support/debug session can see
    exactly which manifest lines were migrated and from what, rather than the defaulting happening
    silently. This is deliberately `.info`, not `.warning` — a routine, expected, non-destructive
    migration on read, not an anomaly.
    """
    if not manifest_path.exists():
        return []
    entries: list[QuarantineManifestEntry] = []
    newer_versions: set[int] = set()
    migrated_count = 0
    with manifest_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            raw = json.loads(stripped)
            missing_fields = [
                field for field in _SCHEMA_V2_BACKWARD_DEFAULTED_FIELDS if field not in raw
            ]
            entry = QuarantineManifestEntry.model_validate_json(stripped)
            if missing_fields:
                migrated_count += 1
                logger.info(
                    "executor.manifest_entry_migrated_from_older_schema",
                    manifest_path=str(manifest_path),
                    batch_id=entry.batch_id,
                    original_path=str(entry.original_path),
                    recorded_schema_version=entry.schema_version,
                    current_schema_version=QUARANTINE_MANIFEST_SCHEMA_VERSION,
                    missing_fields=missing_fields,
                    defaults_applied={field: getattr(entry, field) for field in missing_fields},
                )
            if entry.schema_version > QUARANTINE_MANIFEST_SCHEMA_VERSION:
                newer_versions.add(entry.schema_version)
            entries.append(entry)
    if newer_versions:
        logger.warning(
            "executor.manifest_newer_schema_version_detected",
            manifest_path=str(manifest_path),
            known_schema_version=QUARANTINE_MANIFEST_SCHEMA_VERSION,
            encountered_schema_versions=sorted(newer_versions),
        )
    if migrated_count:
        logger.info(
            "executor.manifest_migration_summary",
            manifest_path=str(manifest_path),
            migrated_entry_count=migrated_count,
            total_entry_count=len(entries),
        )
    return entries


def fold_latest_manifest_entries(manifest_path: Path) -> list[QuarantineManifestEntry]:
    """Folds the append-only event log to current state per `(batch_id, original_path)` — a
    later `phase="done"` line (e.g. a restore or purge update) supersedes an earlier one for the
    same key. Public: `purge_expired` reuses this exact fold rule across the *whole* manifest
    (every batch), not just one `batch_id` — see `_latest_entries_for_batch` for the
    batch-scoped use.

    ADR-0026: entries with `phase != "done"` (an intent not yet resolved, an aborted attempt, or
    an item flagged `needs_review` by `reclaim.recovery`) never enter this fold at all — an
    unresolved intent must never be mistaken for a completed quarantine/restore/purge just
    because it's the last line written for its key. This is what makes an orphaned intent
    invisible to `purge_expired`/`restore_batch`/the dashboard until `reclaim recover` reconciles
    it (or confirms it needs manual review) — silently absent is the safe failure mode here,
    never silently trusted.
    """
    latest: dict[tuple[str, str], QuarantineManifestEntry] = {}
    for entry in read_manifest_entries(manifest_path):
        if entry.phase != "done":
            continue
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
    mode: Mode,
    size_guard_bytes: int,
    size_guard_retention_days: int,
    entry_count_guard: int,
    subtree_entry_count: int | None,
) -> tuple[QuarantineMethod, int | None]:
    """Stage 2 safety boundary, checked FIRST, before any other branch in this function: when
    `mode` is `Mode.SAFE`, the result is unconditionally `("recycle_bin", candidate.
    retention_days)` — regardless of the candidate's own `retention_days`, regardless of
    `method`, regardless of every other rule below. This is what makes `apply_batch`'s
    `vault`/`direct_delete` branches structurally unreachable in safe mode: `item_method` can
    only ever be `"recycle_bin"` when this function is ever called with `mode=Mode.SAFE`, by
    construction, not by a value that merely happens to always come out that way today. See
    `tests/test_executor.py::test_safe_mode_never_produces_vault_or_direct_delete_method` for
    the exhaustive proof (every retention_days value, every requested method).

    A batch's `method` parameter only governs candidates whose category has a real
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

    ADR-0005: a guard-downgraded candidate that IS `rebuildable` (`category_group in
    REBUILDABLE_CATEGORY_GROUPS` — dev_artifacts/package_caches/temp_and_browser_caches/
    crash_dumps, the same categories `retention_days=None` already exists for) gets
    `retention_days=0` instead of `size_guard_retention_days` — immediately purge-eligible, not
    held for the normal 30-day window. Regret is impossible for these categories: their only
    recovery path was always "rebuild it," which the vault copy adds nothing to. Every other
    guard-downgraded candidate (a hypothetical misconfigured category with `retention_days=None`
    that isn't one of the four known-rebuildable groups) keeps the safer `size_guard_retention_
    days` default.

    P0-K1a/M1 cost-budget follow-up (ADR-0032): a SECOND, independent guard axis, keyed on entry
    COUNT rather than bytes. M1's full-subtree re-walk (`_direct_delete_directory_mismatch`,
    reached only for a directory candidate whose resolved method is `"direct_delete"`) has a
    real, measured per-entry wall-clock cost with no further batching available for its hardlink
    pass (see PLAN.md's 2026-08-21 checkpoints) — a candidate can be small in `size_bytes` (or
    outright `size_guard_exempt`, like `package_caches`) while still containing enough entries to
    make that re-walk take longer than a human waiting on `apply` should ever have to (the
    `%LOCALAPPDATA%\\npm-cache` real worst case: 88,864 files, package_caches, exempt from the
    size guard, yet the single most expensive candidate this fix has actually measured). This
    guard fires independently of `size_guard_exempt` — that flag only ever meant "this category's
    RECOVERY cost doesn't scale with size," never "this category's RE-WALK cost doesn't scale
    with entry count," and the two are unrelated axes. `subtree_entry_count` is `None` whenever
    the caller has no `ScanIndex` to cheaply count against (mirrors M1's own `scan_index is None`
    fallback) or the candidate isn't a directory (an entry-count guard is meaningless for a single
    file) — this guard simply never fires in that case, same fail-open-on-this-axis-only posture
    M1's own `scan_index is None` fallback already established (the size guard and the top-level
    identity check still apply regardless).
    """
    if mode == Mode.SAFE:
        return "recycle_bin", candidate.retention_days

    if candidate.retention_days is None:
        size_guard_hit = (
            candidate.size_bytes >= size_guard_bytes and not candidate.size_guard_exempt
        )
        entry_count_guard_hit = (
            subtree_entry_count is not None and subtree_entry_count >= entry_count_guard
        )
        if size_guard_hit or entry_count_guard_hit:
            retention_days = 0 if candidate.rebuildable else size_guard_retention_days
            logger.info(
                "executor.retention_size_guard_downgrade",
                path=str(candidate.path),
                size_bytes=candidate.size_bytes,
                subtree_entry_count=subtree_entry_count,
                category=candidate.category,
                size_guard_bytes=size_guard_bytes,
                entry_count_guard=entry_count_guard,
                size_guard_hit=size_guard_hit,
                entry_count_guard_hit=entry_count_guard_hit,
                retention_days=retention_days,
            )
            return "vault", retention_days
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

# P0-K1a/M1 cost-budget follow-up (ADR-0032): real, measured crossing point, not a round number
# picked without data. Basis: PLAN.md's 2026-08-21 "batched GetFileInformationByHandleEx re-walk"
# checkpoint measured `_direct_delete_directory_mismatch`'s full cost (walk + the unbatchable
# per-file hardlink/nlink pass) against a disposable mirror of this machine's real
# `%LOCALAPPDATA%\npm-cache` (88,864 files + 11,205 dirs = 100,069 entries): 5 interleaved reps,
# NEW median 12.30s, range 9.30s-17.08s. Using the WORST observed rep (not the median) as the
# per-entry rate basis is a deliberate safety margin, not an oversight: the median alone was
# already flagged in that checkpoint as "NOT reliably under budget on a per-run basis" (one of
# five reps exceeded the ~15s absolute ceiling even though the median did not) -- a human waiting
# on `apply` cares about the run they actually get, not the average of five they didn't.
# worst_rate = 17.08s / 100,069 entries = 170.68us/entry; threshold = 15s / worst_rate =
# 87,882.6 entries, floored to the largest entry count whose worst-observed-rate-scaled estimate
# still lands at or under the 15s ceiling. Recorded here, not derived at import time, so this
# constant's value is legible from the source alone without re-running the division.
_DEFAULT_DIRECT_DELETE_ENTRY_COUNT_GUARD = 87_882


def _top_level_identity_mismatch(candidate: Candidate) -> str | None:
    """P0-K1a: re-verifies `candidate.path`'s live `(dev, ino)` against the scan-time baseline
    carried on `candidate` itself (`Candidate.dev`/`.ino`, populated at candidate-generation
    time -- see `models.Candidate`'s own field comment for the three real construction sites).
    Returns a short, loggable description of the mismatch, or `None` if the identity still
    matches (or no real baseline was ever wired through for this candidate -- see below).

    `candidate.dev == 0 and candidate.ino == 0` is treated as "no scan-time baseline available"
    (a not-yet-updated test/eval fixture; on a real NTFS volume device 0 / inode 0 never occurs
    for an actual file), NOT as a confirmed match or a confirmed mismatch -- silently treating an
    absent baseline as a match would be permissive-by-accident, and treating it as a mismatch
    would make every legacy-constructed `Candidate` skip unconditionally. This is the one place
    that distinction is made; `preflight.check_identity_unchanged_since_scan` itself has no
    opinion on what `0, 0` means, since it only ever sees whatever baseline its caller supplies.
    """
    if candidate.dev == 0 and candidate.ino == 0:
        return None
    check = check_identity_unchanged_since_scan(
        candidate.path,
        recorded_dev=candidate.dev,
        recorded_ino=candidate.ino,
        recorded_mtime=candidate.mtime,
        recorded_size_bytes=candidate.size_bytes,
    )
    if not check.identity_changed:
        return None
    return (
        f"{candidate.path}: live (dev, ino)=({check.live_dev}, {check.live_ino}) no longer "
        f"matches the scan's recorded ({check.recorded_dev}, {check.recorded_ino})"
    )


def _live_subtree_records(root: Path) -> list[FileRecord]:
    """M1: read-only re-walk of `root` and everything reachable under it without crossing a
    reparse point -- mirrors `scanner._walk_subtree`'s exact walk shape (stack-based,
    reparse-point-gated recursion) but writes nothing to any index and is only ever used
    immediately before an irreversible delete, never during a normal scan.

    P0-K1a cost-budget fix (this PR): per-entry identity used to come from `os.stat()` (via
    `scanner.build_record`) -- on Windows, one real `CreateFile`+`GetFileInformationByHandle`+
    `CloseHandle` cycle PER ENTRY, measured at 17-28s added to a real apply against the actual
    worst-case direct-delete candidate reachable on this machine (`%LOCALAPPDATA%\\npm-cache`,
    88,864 files -- see PLAN.md's 2026-08-21 checkpoint). Replaced with
    `preflight.enumerate_directory_identity`'s batched `GetFileInformationByHandleEx`/
    `FileIdBothDirectoryInfo` read -- one open directory handle and a small, bounded number of
    calls per DIRECTORY instead of one real open per ENTRY. `dev` (the volume-identity half of
    `(dev, ino)`) is read once per directory via a single `os.stat()` on the directory itself --
    every child of a directory is, by construction, on the SAME volume as its parent UNLESS it's
    a reparse point (already excluded from this walk's own recursion below), so one `os.stat()`
    per directory (not per entry) is both correct and orders of magnitude cheaper. `git_repo_root`/
    `git_repo_clean`/`mtime`/`ctime` are left at `FileRecord`'s own Stage-1 defaults (`None`/
    `False`/`0.0`/`0.0`) -- this throwaway re-verification pass never reads any of the four, and
    computing git-repo state per entry was a large, avoidable share of the original cost this fix
    removes.

    A directory this walk can't open/enumerate (permission error, vanished mid-walk) is silently
    excluded from the returned list -- the same thing `scanner._walk_subtree` does with a
    `SkippedPath`, minus the reporting (this is a throwaway re-verification pass, not a scan).
    Disclosed gap (unchanged by this fix): an entry excluded this way (and its entire subtree, if
    it was a directory) is invisible to `_direct_delete_directory_mismatch`'s comparisons -- it
    can be neither "unexpectedly new" nor "identity mismatched" if this walk never saw it. This is
    the safe direction for what this function itself can decide (nothing here silently approves a
    mutation), but a directory that became genuinely unreadable between scan and apply still
    degrades this check's coverage for whatever sits below it. See this PR's body.
    """
    records: list[FileRecord] = []
    stack = [root]
    while stack:
        current_dir = stack.pop()
        prefixed = long_path(current_dir)
        try:
            dev = os.stat(prefixed, follow_symlinks=False).st_dev  # noqa: PTH116 -- \\?\ str, not Path
        except OSError:
            continue
        entries = enumerate_directory_identity(prefixed)
        if entries is None:
            continue
        for entry in entries:
            entry_path = current_dir / entry.name
            record = FileRecord(
                path=entry_path,
                is_dir=entry.is_dir,
                size_bytes=entry.size_bytes,
                attributes=entry.attributes,
                ext=Path(entry.name).suffix.lower() if not entry.is_dir else "",
                git_repo_root=None,
                git_repo_clean=False,
                dev=dev,
                ino=entry.ino,
            )
            records.append(record)
            if record.is_dir and not entry.is_reparse_point:
                stack.append(entry_path)
    return records


def _direct_delete_directory_mismatch(candidate: Candidate, scan_index: ScanIndex) -> str | None:
    """M1: full-subtree re-verification for an irreversible (`item_method="direct_delete"`)
    DIRECTORY candidate, run immediately before the real `shutil.rmtree`. Unlike every other
    category (`_top_level_identity_mismatch`'s top-level `(dev, ino)` only), a direct-delete
    directory's own top-level identity staying intact says nothing about what changed INSIDE it
    between scan and apply -- a rename-in-place or a new hardlink several levels deep never
    touches the top directory's own inode. Returns a short, loggable description of the FIRST
    mismatch condition found, or `None` if the live tree still matches everything the scan
    recorded for this subtree.

    Two of the three conditions from this fix's design brief, implemented as written:
    (a) any entry the scan recorded under this subtree (`scan_index.candidate_inventory(under=
        candidate.path)` -- the same query `detectors._reclaimable_bytes_for_candidate` already
        uses) whose live `(dev, ino)` no longer matches what was recorded.
    (b) any LIVE entry (file or directory) under this subtree the scan never recorded at all --
        something was added to this tree after the scan ran and before this apply reached it.

    The third condition ("a NEW hardlink created between scan and apply") is deliberately
    implemented differently than its literal wording, and that substitution is disclosed here
    and in this PR's body, not silently made:
    (c) any live FILE under this subtree that is now hardlink-connected into a DIFFERENT
        recognized live environment than its own -- `check_hardlink_shared_active_install`, the
        SAME check `_preflight_skip_reason` already runs once at the top level for every
        candidate, reused here per live file in the subtree instead of a raw `st_nlink > 1`
        threshold, and reused as an ABSOLUTE "is this true right now" check rather than a
        TEMPORAL "did this become true since scan" comparison. Two reasons:
          1. `FileRecord`/the scan index do not persist an nlink baseline today -- capturing one
             is a new DB column + migration, out of this fix's approved scope.
          2. A raw `nlink > 1` threshold would false-positive constantly on package/model
             caches, whose normal, safe, everyday state IS internal hardlinking (ADR-0006) --
             exactly the legitimate case `check_hardlink_shared_active_install`'s existing
             "different RECOGNIZED environment" semantics already exists to distinguish from
             the unsafe case.
        This substitution is strictly conservative in the SAFE direction: anything the literal
        "new hardlink into a different environment since scan" would have caught, this also
        catches (if it's new AND into a different environment, it is by construction also
        CURRENTLY into a different environment). It can additionally fire on a PRE-EXISTING
        different-environment hardlink the scan simply never evaluated at that depth -- a
        false-positive in the strict "did anything change" sense, never a false negative in the
        safety sense.
    """
    recorded_by_path = {
        record.path.as_posix(): record
        for record in scan_index.candidate_inventory(under=candidate.path)
    }
    live_by_path = {
        record.path.as_posix(): record for record in _live_subtree_records(candidate.path)
    }

    for posix_path in live_by_path:
        if posix_path not in recorded_by_path:
            return f"{posix_path}: present now, was never recorded by the scan"

    for posix_path, recorded_record in recorded_by_path.items():
        live_record = live_by_path.get(posix_path)
        if live_record is None:
            continue  # gone since scan -- nothing left to delete there, not a safety concern
        if live_record.dev != recorded_record.dev or live_record.ino != recorded_record.ino:
            return f"{posix_path}: live (dev, ino) no longer matches the scan's recorded value"

    for posix_path, live_record in live_by_path.items():
        if live_record.is_dir:
            continue
        try:
            nlink = live_record.path.stat(follow_symlinks=False).st_nlink
        except OSError:
            continue
        if nlink <= 1:
            continue
        if check_hardlink_shared_active_install(live_record.path).is_shared_with_other_environment:
            return f"{posix_path}: hardlink-connected into a different live environment"
    return None


def _preflight_skip_reason(
    candidate: Candidate,
    *,
    item_method: QuarantineMethod,
    scan_index: ScanIndex | None,
    allowed_roots: Sequence[Path] | None,
) -> PreflightSkipReason | None:
    """Audit P0-1 (docs/AUDIT-2026-08.md) + P0-K1a (this session) + AE1 (this session): the
    pre-flight safety checks run in order, against one candidate right before `apply_batch` would
    otherwise attempt to move/delete it. Returns the first that fires -- any one alone is
    sufficient reason to skip the item without attempting the real mutation, so there is no need
    to run (or report) more than the first hit.

    AE1's ownership/scope check runs FIRST -- cheapest of all (a pure path comparison, no syscall
    at all, unlike every check below it) and answers a genuinely different question than identity
    re-verification does (see `preflight.check_within_allowed_scope`'s module comment): does the
    invoking user have any legitimate claim on this path AT ALL, independent of whether its
    on-disk identity still matches what a scan recorded. `allowed_roots is None` (a caller that
    hasn't been updated to pass any -- no real production caller should ever be in this state;
    CLI/HTTP/MCP all resolve and pass a real set, see `apply_batch`'s own docstring) means this
    check is SKIPPED entirely for the candidate, same disclosed-narrower-coverage posture
    `scan_index is None` already has below for M1 -- never a silent "treat as in scope".

    `check_hardlink_shared_active_install` is applied to every DIRECT-DELETE and RECYCLE-BIN
    apply (including a human-confirmed, single-item apply from the dashboard), not gated behind
    any "is this an unattended/scheduled run" flag -- `apply_batch` has no such notion today
    (R6's autonomous-cleanup allowlist doesn't exist yet either; see the audit's R6/D3 sections),
    and the check itself is genuinely defense-in-depth (deleting a hardlink-shared path is not
    destructive to its siblings -- see `preflight.check_hardlink_shared_active_install`'s
    docstring) rather than a hard safety violation, so applying it to those two paths is the
    conservative choice: it costs a skip a human can investigate and re-apply after review, never
    a silent unsafe delete. Revisit this call site (not the check itself) if/when a real
    "attended vs. unattended" distinction is ever added to this function's signature.

    P0-K1a/M1 cost-budget follow-up (ADR-0032, P3): SKIPPED for `item_method == "vault"`
    specifically -- verified airtight, not assumed: a vault move is `_atomic_move`'s same-volume
    `os.rename` in the common case (a single metadata operation, doesn't touch the target's bytes
    or its hardlink siblings at all) and, in the rare cross-volume fallback, a copy-then-delete of
    `src` -- and the check's own docstring already establishes that deleting `path` "is NOT
    destructive to any hardlink sibling by construction" (an unlink only decrements the shared
    inode's reference count; every surviving name still resolves to the identical bytes) for
    EITHER case. Vault additionally never destroys anything at all -- the item is fully restorable
    via `reclaim undo` right up until (and, per ADR-0032, briefly past) its retention window --
    which is strictly weaker grounds for pausing an unattended apply than direct-delete's genuine
    permanence. No UI/reporting path was found to depend on `hardlink_shared_active_install` firing
    specifically for a vault-method item (grep-confirmed: the only consumer of this skip_reason
    literal outside this module is the generic `PreflightSkipReason` display, which renders any
    skip reason identically regardless of which one fired) -- so skipping it here costs nothing a
    vaulted candidate's own user was relying on to see. Kept fully, unconditionally, for
    `direct_delete` and `recycle_bin` -- it is load-bearing there, per the original P0-1 finding.

    P0-K1a identity checks, run last (the two checks above are cheaper and were here first):
    `_top_level_identity_mismatch` runs for every candidate; `_direct_delete_directory_mismatch`
    (M1) additionally runs for a directory candidate whose resolved `item_method` is
    `"direct_delete"` (permanent, unrecoverable) -- vaulted/recycle-bin directory candidates are
    recoverable, so only the cheaper top-level check applies to them, matching this fix's
    approved tiered-by-recoverability design. `scan_index is None` (a caller that hasn't been
    updated to pass one -- every real production caller, CLI `apply`/`POST /api/apply`, always
    does) means M1's subtree re-walk cannot run at all for this candidate; this is logged loudly
    and the candidate falls back to the top-level-only check, same protection every vaulted
    candidate already gets -- a disclosed, narrower-coverage fallback, never a silent skip of
    the identity check altogether.
    """
    if allowed_roots is not None and not check_within_allowed_scope(
        candidate.path, allowed_roots=allowed_roots
    ):
        logger.info(
            "executor.outside_user_scope_skipped",
            path=str(candidate.path),
            allowed_roots=[str(root) for root in allowed_roots],
        )
        return "outside_user_scope"

    if check_file_in_use(candidate.path, is_dir=candidate.is_dir):
        return "file_in_use"
    if (
        item_method != "vault"
        and check_hardlink_shared_active_install(candidate.path).is_shared_with_other_environment
    ):
        return "hardlink_shared_active_install"

    top_level_detail = _top_level_identity_mismatch(candidate)
    if top_level_detail is not None:
        logger.info("executor.identity_mismatch_detected", detail=top_level_detail)
        return "identity_changed_since_scan"

    if item_method == "direct_delete" and candidate.is_dir:
        if scan_index is None:
            logger.warning(
                "executor.direct_delete_directory_reverify_unavailable",
                path=str(candidate.path),
            )
        else:
            subtree_detail = _direct_delete_directory_mismatch(candidate, scan_index)
            if subtree_detail is not None:
                logger.info("executor.identity_mismatch_detected", detail=subtree_detail)
                return "identity_changed_since_scan"
    return None


def _verify_apply_postcondition(
    original_path: Path, *, item_method: QuarantineMethod, vault_path: Path | None
) -> str | None:
    """K2a (audit finding): real, fresh post-condition check run immediately after a
    move/delete call in `apply_batch`'s per-item loop returns WITHOUT raising, and BEFORE
    `succeeded=True` is ever recorded for that item. Returns a short, loggable description of
    what didn't actually happen, or `None` if the filesystem genuinely matches what a successful
    `item_method` mutation should have produced.

    The absence of an exception is never sufficient evidence of success on this platform — see
    K2b's `shutil.rmtree`/junction finding (this module's `rmtree_reparse_point_safe` section
    comment) for a reproduced, real case of an operation returning normally having mutated
    nothing at all. This check is deliberately independent of K2b's specific root-cause fix: it
    is the general contract every mutation in this loop must satisfy, not a patch for one bug.

    `_path_exists_no_follow` (not `os.path.exists`/`Path.exists()`) is used throughout — a
    dangling reparse point (target removed, entry itself still present) must still count as
    "still here", which a target-following existence check would wrongly report as gone.
    """
    if _path_exists_no_follow(long_path(original_path)):
        return (
            f"{original_path}: still exists on disk after a {item_method} operation that raised "
            "no error -- the operation silently did not remove it"
        )
    if item_method == "vault":
        if vault_path is None:  # unreachable: apply_batch always computes vault_path for "vault"
            return f"{original_path}: vault method with no vault_path to verify against"
        if not _path_exists_no_follow(long_path(vault_path)):
            return (
                f"{original_path}: original path is gone, but nothing exists at the recorded "
                f"vault_path {vault_path} -- the item is neither at its original location nor "
                "recoverable from the vault"
            )
    return None


def apply_batch(
    candidates: list[Candidate],
    *,
    safety: SafetyValidator,
    apply: bool = False,
    method: QuarantineMethod = "vault",
    mode: Mode = Mode.POWER,
    vault_dir: Path | None = None,
    manifest_path: Path | None = None,
    now: float | None = None,
    direct_delete_size_guard_bytes: int = _DEFAULT_DIRECT_DELETE_SIZE_GUARD_BYTES,
    direct_delete_size_guard_retention_days: int = _DEFAULT_DIRECT_DELETE_SIZE_GUARD_RETENTION_DAYS,
    direct_delete_entry_count_guard: int = _DEFAULT_DIRECT_DELETE_ENTRY_COUNT_GUARD,
    on_progress: ProgressCallback | None = None,
    scan_index: ScanIndex | None = None,
    allowed_roots: Sequence[Path] | None = None,
) -> BatchApplyReport:
    """Quarantines (or, for `retention_days=None` candidates, permanently deletes) every
    candidate in one batch.

    `allowed_roots` (AE1): when provided, every candidate whose path falls outside all of
    `allowed_roots` is skipped (`skip_reason="outside_user_scope"`) before any other pre-flight
    check runs — see `_preflight_skip_reason`'s docstring and `preflight.check_within_allowed_
    scope`'s module comment for why this is a distinct question from identity re-verification.
    `None` (the default) disables the check entirely — same test-compatibility posture `mode`'s
    own default already documents in this exact function: preserves every existing caller's
    exact current behavior (this test suite's ~600+ tests included), and every REAL end-user
    entry point (CLI `apply`, `POST /api/apply`, MCP `delete`) must pass a real value explicitly.
    A caller that omits it gets NO ownership/scope protection, the same way a caller that omits
    a live `mode` gets no live safe-mode enforcement — this is a deliberate, disclosed default,
    never a silent "safe by default" claim this function doesn't actually make for an unspecified
    parameter.

    `on_progress` (fix/apply-progress-feedback): optional interval-gated progress hook — see
    `ProgressCallback`'s docstring. Defaults to `None` so every existing caller (this test
    suite's ~600 tests included) is unaffected.

    `mode` defaults to `Mode.POWER` — this function's own default preserves every existing
    caller's exact current behavior (this test suite's ~600 tests included); real end-user
    entry points (the CLI, the dashboard) must pass the LIVE mode explicitly, sourced from
    `reclaim.mode.current_mode()` via `config.load_effective_config`. When `mode` is
    `Mode.SAFE`: (1) this call refuses immediately, before any I/O, if `method` isn't
    `"recycle_bin"` — see `SafeModeViolationError`; (2) every candidate's effective method is
    unconditionally `"recycle_bin"` regardless of its own `retention_days` (see
    `_effective_method_and_retention_days`) — the `vault` and `direct_delete` branches in the
    per-candidate loop below are structurally unreachable whenever `mode=Mode.SAFE`, not merely
    unreached in practice.

    Dry-run is the default (`apply=False`, spec design principle 5): makes zero mutating
    filesystem calls — no moves, no `send2trash`/`unlink`/`rmtree` calls, no manifest writes,
    no disk-usage measurement — and returns a report with the same shape as a real run, every
    item simulated as "would succeed", clearly labeled `apply=False`.

    `apply=True` actually moves/trashes/permanently-deletes each file and appends one manifest
    entry per successfully-processed item. A single item's failure (file already gone,
    permission error, ...) is caught, recorded, and does not abort the rest of the batch (house
    rule 104: errors are part of the API, not silent) — *except* for the pre-delete safety
    re-check below, which is a whole-batch abort by design.

    Audit P0-1 (docs/AUDIT-2026-08.md) + P0-K1a (this session): immediately before each item's
    real mutation, when `apply=True`, `_preflight_skip_reason` (`reclaim.preflight`) runs every
    pre-flight check — "is this path currently held open by another process", "is this path
    hardlink-connected into a DIFFERENT live Python environment than its own", and "does this
    path's live identity still match what the scan recorded for it" (K1a: a live-reproduced
    finding this session that swapping content at a candidate's path between scan and apply
    caused the swapped content to be permanently deleted or misrouted into the vault — see
    `_top_level_identity_mismatch`/`_direct_delete_directory_mismatch`). Any one check skips
    just that item (never attempted, no manifest intent written, `ItemApplyResult.skip_reason`
    set) and the batch continues — the same "abort the item, not the run" shape every other
    per-item failure in this loop already follows, not a new abort mode.

    `scan_index` (P0-K1a/M1): the SAME `ScanIndex` the candidates were generated against —
    required for the full-subtree re-walk `_direct_delete_directory_mismatch` runs against every
    irreversible (`retention_days is None`, resolved `item_method="direct_delete"`) DIRECTORY
    candidate. `None` (the default, preserving every existing caller's behavior — this test
    suite's ~600 tests included) disables that specific re-walk and logs the gap loudly per
    item (`executor.direct_delete_directory_reverify_unavailable`) rather than silently doing
    less than the caller might expect; the top-level `(dev, ino)` identity check still runs
    regardless. Both real production callers — CLI `reclaim apply` and `POST /api/apply` — pass
    a live `scan_index` opened immediately before this call. Named `scan_index`, not `index`,
    to avoid shadowing this function's own per-item loop counter (`for index, candidate in
    enumerate(candidates, ...)`) a few dozen lines below.

    `method` (`"vault"`/`"recycle_bin"`) only governs candidates whose category has a real
    retention window; a candidate with `retention_days is None` always direct-deletes
    regardless of `method` (ADR-0001) — see `_effective_method_and_retention_days`. A single
    batch may therefore mix direct-delete and vaulted/recycle-binned items; `item.method` on
    each `ItemApplyResult` records which one actually applied to that item, not just the
    batch-level `method` param.

    ADR-0003: a `retention_days is None` candidate at or above `direct_delete_size_guard_bytes`
    is forced to `vault` instead of `direct_delete`, with `direct_delete_size_guard_retention_days`
    as its retention window — recovery cost, not category, gates permanent deletion.

    ADR-0032 (P0-K1a/M1 cost-budget follow-up): a SECOND, independent guard, keyed on entry count
    rather than bytes — a `retention_days is None` DIRECTORY candidate whose scan-recorded subtree
    entry count (`scan_index.subtree_entry_count`, a cheap indexed `COUNT(*)`, never a live walk)
    is at or above `direct_delete_entry_count_guard` is force-downgraded to `vault` the same way
    the byte-size guard is — independent of `size_guard_exempt` (that flag is about RECOVERY cost,
    this guard is about RE-WALK cost; a `package_caches` candidate like a real `npm-cache` root can
    be exempt from the byte guard yet still contain enough entries to make M1's full re-walk take
    longer than a human waiting on `apply` should have to). Requires `scan_index` to be non-`None`
    and the candidate to be a directory; otherwise this guard axis simply never fires (the size
    guard and the top-level identity check still apply regardless) — same disclosed,
    narrower-coverage-not-silent-skip posture `scan_index is None` already has for M1 itself. A
    guard-downgraded candidate this way gets `retention_days=0` under the exact same ADR-0005
    rebuildable-category rule as a byte-size-guard downgrade — and, per ADR-0032, that
    `retention_days=0` result is what makes it eligible for the synchronous purge below.

    ADR-0032, synchronous purge for guard-downgraded zero-retention items: a candidate whose
    CATEGORY resolves to `retention_days=None` (i.e. was always going to be permanently,
    irreversibly deleted with no review checkpoint at all) but gets force-vaulted to
    `retention_days=0` by either guard above is, immediately after the main per-item loop below
    (same batch, same manifest lock, same `apply_batch` call), purged right back out of the vault
    — so `retention_days=0` means what it says: bytes are actually gone from the volume by the
    time this call returns, not merely "eligible for a future `reclaim purge` run" (closing the
    gap ADR-0001's own real-disk-free-delta requirement exists for). Scoped narrowly and
    explicitly to `candidate.retention_days is None` (checked BEFORE guard resolution, not the
    resolved `item_retention_days`) so a category a human explicitly configured with
    `retention_days=0` in `config.toml` is never swept into this — that item never had this fix's
    guard involved at all, so it keeps the ordinary "vault now, `reclaim purge` explicitly later"
    checkpoint unchanged. See `ItemApplyResult.synchronously_purged` for the per-item outcome and
    `docs/architecture/adr/0032-entry-count-guard-and-synchronous-purge.md` for the full
    reasoning, including why this does not reopen ADR-0001's rejected "auto-purge everything on
    every run" alternative.

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

    if mode == Mode.SAFE and method != "recycle_bin":
        raise SafeModeViolationError(
            f"apply_batch was called with mode=Mode.SAFE and method={method!r} — safe mode "
            "only ever allows the Recycle Bin, never vault (this tool's own quarantine "
            "directory) and never direct-delete. Refusing the entire batch, touching nothing."
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
    # ADR-0032: `(index into items, the just-written vault "done" manifest entry)` for every
    # succeeded item this call must synchronously purge right back out again -- see this
    # function's own docstring section on synchronous purge. Collected during the main loop,
    # acted on in a second pass immediately after it (still inside this `try:`, still holding the
    # same manifest lock) so every vault "done" record is durable BEFORE any purge intent for the
    # same item is ever written.
    pending_synchronous_purges: list[tuple[int, QuarantineManifestEntry]] = []
    total_candidates = len(candidates)
    last_heartbeat = time.monotonic()
    # ADR-0026: one manifest file handle held open for the whole batch (not re-opened per item)
    # so the only added per-item filesystem cost is the fsync itself, not repeated `open()`
    # syscalls. `None` in dry-run — dry-run makes zero filesystem calls of any kind, manifest
    # writes included, same guarantee as before this change.
    manifest_fh = _open_manifest_for_sync(resolved_manifest_path) if apply else None
    try:
        for index, candidate in enumerate(candidates, start=1):
            # fix/apply-progress-feedback: interval-gated, not per-item (see `ProgressCallback`).
            # `index - 1` is "items completed before this one", the honest count at this point.
            heartbeat_now = time.monotonic()
            if _due(last=last_heartbeat, now=heartbeat_now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                logger.info(
                    "executor.apply_progress",
                    items_processed=index - 1,
                    items_total=total_candidates,
                    current_category=candidate.category,
                )
                if on_progress is not None:
                    on_progress(index - 1, total_candidates, candidate.category)
                last_heartbeat = heartbeat_now

            # ADR-0032: cheap indexed COUNT, never a live walk -- only worth querying at all for
            # the exact shape the entry-count guard can ever fire on (a directory candidate whose
            # category would otherwise direct-delete it); every other candidate leaves this
            # `None` and the guard axis simply never fires for it, same as `scan_index is None`.
            subtree_entry_count = (
                scan_index.subtree_entry_count(candidate.path)
                if scan_index is not None and candidate.is_dir and candidate.retention_days is None
                else None
            )
            item_method, item_retention_days = _effective_method_and_retention_days(
                candidate,
                method,
                mode=mode,
                size_guard_bytes=direct_delete_size_guard_bytes,
                size_guard_retention_days=direct_delete_size_guard_retention_days,
                entry_count_guard=direct_delete_entry_count_guard,
                subtree_entry_count=subtree_entry_count,
            )
            item_retention_until = (
                now_ts + item_retention_days * _SECONDS_PER_DAY
                if item_retention_days is not None
                else None
            )
            vault_path = (
                _compute_vault_path(resolved_vault_dir, batch_id, candidate.path)
                if item_method == "vault"
                else None
            )

            if not apply:
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

            if manifest_fh is None:  # unreachable: opened above whenever apply=True
                raise RuntimeError("apply_batch: manifest file handle unexpectedly not open")

            # Audit P0-1 (docs/AUDIT-2026-08.md) + P0-K1a: the pre-flight safety checks, run
            # BEFORE anything is written to the manifest at all -- a skipped item was never
            # attempted, so (unlike a caught mutation failure) there is no intent to record and
            # no "aborted" phase to close out; `reclaim.recovery` never needs to know this item
            # existed.
            skip_reason = _preflight_skip_reason(
                candidate,
                item_method=item_method,
                scan_index=scan_index,
                allowed_roots=allowed_roots,
            )
            if skip_reason is not None:
                logger.info(
                    "executor.apply_item_skipped_preflight",
                    path=str(candidate.path),
                    method=item_method,
                    skip_reason=skip_reason,
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
                        error=None,
                        vault_path=None,
                        skip_reason=skip_reason,
                    )
                )
                continue

            # ADR-0026, phase 1: log the intent, fsynced, BEFORE any filesystem mutation. A kill
            # here leaves an intent whose source is untouched — `reclaim.recovery` reconciles it
            # as "aborted" (source still present, action never executed).
            intent_entry = QuarantineManifestEntry(
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
                phase="intent",
                intent_id=uuid.uuid4().hex,
                operation="apply",
                # Explicit, not relying on the field's own default (see the field's comment on
                # `QuarantineManifestEntry` for why the two must never be conflated).
                schema_version=QUARANTINE_MANIFEST_SCHEMA_VERSION,
            )
            _append_and_sync(manifest_fh, intent_entry)

            try:
                if item_method == "vault":
                    resolved_vault_path = _require_vault_path(vault_path)
                    _atomic_move(candidate.path, resolved_vault_path, is_dir=candidate.is_dir)
                elif item_method == "recycle_bin":
                    send2trash.send2trash(str(candidate.path))
                else:  # direct_delete: permanent, no vault, no Recycle Bin (ADR-0001)
                    if candidate.is_dir:
                        # K2b: `candidate.path` is this call's own top-level target -- exactly
                        # the shape that can itself be a reparse point (a junction/symlink
                        # candidate). Plain `shutil.rmtree` here silently no-ops on that case.
                        rmtree_reparse_point_safe(long_path(candidate.path))
                    else:
                        unlink_clear_readonly(long_path(candidate.path))
            except Exception as exc:  # broad: isolates one item's failure from the batch
                logger.warning(
                    "executor.apply_item_failed",
                    path=str(candidate.path),
                    method=item_method,
                    error=str(exc),
                )
                # ADR-0026, phase 2 (failure path): close out the intent explicitly rather than
                # leaving it dangling — a caught, handled failure is not a crash, so there is
                # nothing for `reclaim.recovery` to reconcile about this item.
                _append_and_sync(manifest_fh, intent_entry.model_copy(update={"phase": "aborted"}))
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

            # K2a (audit finding): the mutation call above raised no exception -- but on this
            # platform that is NOT sufficient evidence anything actually happened (K2b's
            # `shutil.rmtree`/junction finding is the reproduced real case). Verify the real,
            # fresh on-disk post-condition BEFORE ever recording `succeeded=True` for this item.
            postcondition_error = _verify_apply_postcondition(
                candidate.path, item_method=item_method, vault_path=vault_path
            )
            if postcondition_error is not None:
                logger.warning(
                    "executor.apply_item_postcondition_failed",
                    path=str(candidate.path),
                    method=item_method,
                    detail=postcondition_error,
                )
                # Same "caught, handled failure -- not a crash" shape as the except block above:
                # close out the intent as aborted, never leave it dangling for `reclaim.recovery`.
                _append_and_sync(manifest_fh, intent_entry.model_copy(update={"phase": "aborted"}))
                items.append(
                    ItemApplyResult(
                        path=candidate.path,
                        category=candidate.category,
                        category_group=candidate.category_group,
                        size_bytes=candidate.size_bytes,
                        tier=candidate.tier,
                        method=item_method,
                        succeeded=False,
                        error=postcondition_error,
                        vault_path=None,
                        postcondition_verification_failed=True,
                    )
                )
                continue

            # ADR-0026, phase 2 (success path): the action is now real on disk; log it done,
            # fsynced. A kill between the two `_append_and_sync` calls above leaves an intent
            # whose target now exists — `reclaim.recovery` reconciles it as "completed".
            done_entry = intent_entry.model_copy(update={"phase": "done"})
            _append_and_sync(manifest_fh, done_entry)
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
            # ADR-0032: scoped to `candidate.retention_days is None` (the CATEGORY's own,
            # pre-guard setting) -- never `item_retention_days == 0` alone, which a human could
            # also reach by explicitly configuring `retention_days: 0` for some category in
            # config.toml. Only a guard-downgrade-from-direct-delete gets synchronously purged;
            # an explicitly-configured zero-retention category keeps the ordinary vault-now,
            # purge-later checkpoint untouched.
            if (
                item_method == "vault"
                and item_retention_days == 0
                and candidate.retention_days is None
            ):
                pending_synchronous_purges.append((len(items) - 1, done_entry))

        # ADR-0032: second pass, same manifest lock, same `try:` -- see docstring. Runs after
        # every item in the batch has had its own apply intent/done pair written, so a purge
        # intent for one item is never interleaved with another item's still-open apply intent.
        # `manifest_fh is not None` is re-checked (not just asserted) here even though it's
        # unreachable for `pending_synchronous_purges` to be non-empty when `manifest_fh is None`
        # (every entry in it came from the `apply=True`-only success path above, which already
        # requires an open handle) -- mypy can't see that invariant across the two loops, and a
        # narrowed local name is clearer than a `# type: ignore`.
        if manifest_fh is not None:
            for item_index, vault_done_entry in pending_synchronous_purges:
                purge_vault_path = vault_done_entry.vault_path
                if purge_vault_path is None:  # unreachable: only "vault" entries were collected
                    continue
                purge_intent = vault_done_entry.model_copy(
                    update={
                        "phase": "intent",
                        "intent_id": uuid.uuid4().hex,
                        "operation": "purge",
                    }
                )
                _append_and_sync(manifest_fh, purge_intent)
                try:
                    if vault_done_entry.is_dir:
                        # K2b: a vaulted candidate whose `os.rename` succeeded (the common,
                        # same-volume case in `_atomic_move`) moves a reparse point AS a reparse
                        # point -- so `purge_vault_path` can itself be a junction here. Plain
                        # `shutil.rmtree` would silently no-op on that case.
                        rmtree_reparse_point_safe(long_path(purge_vault_path))
                    else:
                        unlink_clear_readonly(long_path(purge_vault_path))
                except OSError as exc:
                    # Not a failure of the apply itself -- the item is still validly vaulted and
                    # restorable; it simply stays a normal, retention_days=0 vault entry,
                    # eligible for the next `reclaim purge` run same as before this fix existed.
                    logger.warning(
                        "executor.apply_synchronous_purge_failed",
                        path=str(vault_done_entry.original_path),
                        vault_path=str(purge_vault_path),
                        error=str(exc),
                    )
                    _append_and_sync(
                        manifest_fh, purge_intent.model_copy(update={"phase": "aborted"})
                    )
                    continue
                _append_and_sync(
                    manifest_fh,
                    purge_intent.model_copy(
                        update={"phase": "done", "purged": True, "purged_at": now_ts}
                    ),
                )
                items[item_index] = replace(items[item_index], synchronously_purged=True)
                logger.info(
                    "executor.apply_synchronous_purge_completed",
                    path=str(vault_done_entry.original_path),
                    vault_path=str(purge_vault_path),
                    size_bytes=vault_done_entry.size_bytes,
                )
    finally:
        if manifest_fh is not None:
            _close_manifest_for_sync(manifest_fh)

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
    purged_items = [item for item in succeeded_items if item.synchronously_purged]
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
        synchronously_purged_count=len(purged_items),
        bytes_synchronously_purged=sum(item.size_bytes for item in purged_items),
    )


def _is_contained(path: Path, container: Path) -> bool:
    """True if `path` resolves to `container` itself or a descendant of it.

    `Path.resolve()` normalizes `..`/`.` segments and makes the path absolute without requiring
    it to exist (`strict=False`, the default) — so this catches a manifest `vault_path` that
    escapes the configured vault directory via traversal segments or an outright unrelated
    absolute path, without needing the vault entry to still be on disk. Windows path comparison
    via `Path.__eq__`/`.parents` is already case-insensitive (`os.path.normcase`), matching every
    other path-identity check in this module.
    """
    resolved_path = path.resolve()
    resolved_container = container.resolve()
    return resolved_path == resolved_container or resolved_container in resolved_path.parents


def _restore_integrity_violations(
    vault_entries: Sequence[QuarantineManifestEntry], vault_dir: Path, safety: SafetyValidator
) -> list[str]:
    """Pre-move structural check over every vault entry in the batch — see
    `RestoreIntegrityError` for why this exists and why a violation aborts the whole call rather
    than just the offending item."""
    violations: list[str] = []
    for entry in vault_entries:
        if entry.vault_path is not None and not _is_contained(entry.vault_path, vault_dir):
            violations.append(
                f"{entry.original_path}: recorded vault_path {entry.vault_path} does not "
                f"resolve inside the configured vault directory {vault_dir}"
            )
        if safety.path_is_protected_root(entry.original_path):
            violations.append(
                f"{entry.original_path}: original_path matches a protected system root — "
                "refusing to restore into it"
            )
    return violations


def resolve_restorable_entries(
    batch_id: str,
    *,
    manifest_path: Path,
    vault_dir: Path,
    safety: SafetyValidator,
) -> tuple[
    list[QuarantineManifestEntry], list[QuarantineManifestEntry], list[QuarantineManifestEntry]
]:
    """Every check `restore_batch` performs BEFORE touching anything: batch lookup
    (`BatchNotFoundError`), the manifest-integrity/zip-slip-equivalent guard
    (`RestoreIntegrityError`), and the whole-call refusal when nothing in the batch is restorable
    at all (`DirectDeleteRestoreImpossibleError`/`RecycleBinRestoreUnsupportedError`).

    Factored out of `restore_batch` (fix/apply-progress-feedback) — not merely called
    internally, but exported — so `api.service`'s background-task conversion of `POST
    /api/restore/{batch_id}` can run this EXACT validation synchronously (a cheap manifest read
    + fold, no filesystem mutation, no ADR-0026 fsync cost) before committing to a background
    task for the real, potentially slow restore loop. That keeps the immediate HTTP response for
    a bad batch id (404), an unsupported method (409), or a corrupted manifest (500) exactly as
    fast as before that conversion — only the genuinely slow per-item work moved to the
    background. `restore_batch` itself calls this first, behavior unchanged.

    Returns `(vault_entries, direct_delete_entries, recycle_bin_entries)` — the exact three
    partitions `restore_batch`'s own body needs next.
    """
    entries = _latest_entries_for_batch(manifest_path, batch_id)
    if not entries:
        raise BatchNotFoundError(f"no manifest entries found for batch_id={batch_id!r}")

    vault_entries = [entry for entry in entries if entry.method == "vault"]
    direct_delete_entries = [entry for entry in entries if entry.method == "direct_delete"]
    recycle_bin_entries = [entry for entry in entries if entry.method == "recycle_bin"]

    integrity_violations = _restore_integrity_violations(vault_entries, vault_dir, safety)
    if integrity_violations:
        raise RestoreIntegrityError(
            f"restore_batch's manifest-integrity pre-check found {len(integrity_violations)} "
            "violation(s) in batch_id="
            f"{batch_id!r} — refusing the entire restore, moving nothing: "
            f"{integrity_violations[:5]}"
        )

    if not vault_entries:
        # Nothing restorable at all in this batch — preserve the loud, whole-call refusal
        # rather than silently returning an all-skipped report that looks like it did nothing.
        if direct_delete_entries:
            raise DirectDeleteRestoreImpossibleError(
                f"this batch contains {len(direct_delete_entries)} permanently-deleted file(s) "
                "(retention=none for their category) — there is nothing to restore, they were "
                "not quarantined"
            )
        if recycle_bin_entries:
            raise RecycleBinRestoreUnsupportedError(
                f"this batch contains {len(recycle_bin_entries)} Recycle-Bin-quarantined "
                "file(s); restore them manually via Windows Explorer's Recycle Bin — automated "
                "restore isn't supported for this method"
            )

    return vault_entries, direct_delete_entries, recycle_bin_entries


def restore_batch(
    batch_id: str,
    *,
    manifest_path: Path | None = None,
    vault_dir: Path | None = None,
    safety: SafetyValidator,
    now: float | None = None,
    on_progress: ProgressCallback | None = None,
) -> RestoreReport:
    """Restores every *restorable* item in `batch_id` back to its exact original path.

    `vault_dir`/`safety` are required so every restore runs the `RestoreIntegrityError`
    pre-check below (a manifest-integrity/zip-slip-equivalent guard) before touching anything —
    there is no code path that restores without it. `vault_dir` must be the same directory
    `apply_batch` was configured with (the caller's responsibility, same as `manifest_path`);
    a mismatch here would make the containment check meaningless, not merely lenient.

    Reads current state from the manifest (see `_latest_entries_for_batch`). A single
    `apply_batch` call always shares one requested `method` param, but ADR-0001's per-candidate
    `retention_days` override means a real batch routinely mixes `vault` entries with
    `direct_delete` ones (and, less commonly, `recycle_bin` ones) — the 6-category scoped
    real-disk apply from 2026-07-17 is exactly this shape: 23,565 `direct_delete` entries
    alongside 7 `vault` ones, all sharing one `batch_id`.

    `direct_delete`/`recycle_bin` entries in a batch that also contains at least one restorable
    `vault` entry are reported per-item as `restore_unsupported=True` (see `RestoreItemResult`)
    — never restored, never silently retried, but no longer blocking the vault entries in the
    same batch from restoring. Only when a batch has NO restorable `vault` entry at all (a pure
    `direct_delete` batch, or a pure `recycle_bin` batch) does this still refuse the whole call
    loudly, since a report with everything skipped and nothing attempted would be misleading —
    `DirectDeleteRestoreImpossibleError`/`RecycleBinRestoreUnsupportedError` unchanged for that
    case.

    Never overwrites an existing file at the destination — an item whose original path is
    occupied by something else now fails loudly (recorded in the report) rather than silently
    clobbering it. Idempotent: an item already marked `restored=True` is reported as
    `already_restored` and left untouched, so restoring the same batch twice is safe.

    `on_progress` (fix/apply-progress-feedback): optional interval-gated progress hook over the
    `vault_entries` loop specifically — see `ProgressCallback`'s docstring. `items_total` in the
    callback is `len(vault_entries)`, not the whole batch's item count: `direct_delete`/
    `recycle_bin` entries are classified instantly above (no filesystem I/O, no fsync cost), so
    only the vault-entries loop is ever slow enough to need a progress signal. Defaults to
    `None` so every existing caller is unaffected.
    """
    resolved_manifest_path = manifest_path if manifest_path is not None else DEFAULT_MANIFEST_PATH
    resolved_vault_dir = vault_dir if vault_dir is not None else DEFAULT_VAULT_DIR
    now_ts = now if now is not None else time.time()

    vault_entries, direct_delete_entries, recycle_bin_entries = resolve_restorable_entries(
        batch_id, manifest_path=resolved_manifest_path, vault_dir=resolved_vault_dir, safety=safety
    )

    items: list[RestoreItemResult] = []

    for entry in direct_delete_entries:
        items.append(
            RestoreItemResult(
                original_path=entry.original_path,
                size_bytes=entry.size_bytes,
                succeeded=False,
                already_restored=False,
                error=(
                    "permanently-deleted (retention=none for its category) — there is nothing "
                    "to restore, it was not quarantined"
                ),
                restore_unsupported=True,
            )
        )
    for entry in recycle_bin_entries:
        items.append(
            RestoreItemResult(
                original_path=entry.original_path,
                size_bytes=entry.size_bytes,
                succeeded=False,
                already_restored=False,
                error=(
                    "Recycle-Bin-quarantined; restore manually via Windows Explorer's Recycle "
                    "Bin — automated restore isn't supported for this method"
                ),
                restore_unsupported=True,
            )
        )

    # ADR-0026: opened once for the whole restore call, only if there's a vault entry that might
    # actually attempt a move — mirrors `apply_batch`'s single-handle-per-call approach.
    manifest_fh = _open_manifest_for_sync(resolved_manifest_path) if vault_entries else None
    total_vault_entries = len(vault_entries)
    last_heartbeat = time.monotonic()
    try:
        for index, entry in enumerate(vault_entries, start=1):
            # fix/apply-progress-feedback: interval-gated, not per-item (see `ProgressCallback`).
            heartbeat_now = time.monotonic()
            if _due(last=last_heartbeat, now=heartbeat_now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                logger.info(
                    "executor.restore_progress",
                    items_processed=index - 1,
                    items_total=total_vault_entries,
                    current_category=entry.category,
                )
                if on_progress is not None:
                    on_progress(index - 1, total_vault_entries, entry.category)
                last_heartbeat = heartbeat_now

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
                # Unreachable in practice: this loop only ever iterates `vault_entries`, which by
                # construction always carries a `vault_path` (set by `apply_batch` whenever
                # `method="vault"`). Guards mypy's None narrowing and, if manifest data were ever
                # corrupted, fails loudly per-item rather than crashing the whole restore.
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

            # M2 (P0-K1a follow-up, this session): "recognized vs unrecognized" destination
            # design decision, stated explicitly rather than left implicit in the bare
            # `os.path.exists` check below -- `restore_batch` writes to `entry.original_path`,
            # an ARBITRARY user path, not this tool's own vault directory, so an accidental
            # overwrite here is strictly worse than an accidental overwrite inside the vault
            # (`purge_expired`'s lower-severity, deliberately out-of-scope case for this same
            # fix -- see this PR's body). Considered and REJECTED: treating a manifest-known
            # "partial-quarantine remnant" (an entry this exact restore's own manifest already
            # has a record for) as a recognized, safe-to-overwrite case. Rejected because no such
            # case actually exists in this codebase's real data shapes: by the time this line
            # runs, `entry.restored` has already been checked `True` above (idempotent
            # short-circuit, reported as `already_restored` and never reaches here), and
            # `_atomic_move`'s own ADR-0004 guarantee means a `phase="done"` restore NEVER
            # leaves a partial file at `original_path` for a later restore to legitimately find
            # sitting there. There is therefore no "recognized" case: ANYTHING present at
            # `original_path` right now is unrecognized by construction, and this check
            # unconditionally skips rather than overwrites, full stop -- not a placeholder for a
            # future distinction, the actual, final decision.
            if os.path.exists(long_path(entry.original_path)):  # noqa: PTH110 -- \\?\, not Path
                items.append(
                    RestoreItemResult(
                        original_path=entry.original_path,
                        size_bytes=entry.size_bytes,
                        succeeded=False,
                        already_restored=False,
                        error=(
                            "destination already exists, refusing to overwrite: "
                            f"{entry.original_path}"
                        ),
                    )
                )
                continue

            if manifest_fh is None:  # unreachable: opened above whenever vault_entries non-empty
                raise RuntimeError("restore_batch: manifest file handle unexpectedly not open")

            # ADR-0026, phase 1: log the restore intent, fsynced, before moving anything. A kill
            # here leaves an intent whose source (vault_path) is untouched — reconciled as
            # "aborted" (the restore never executed, entry stays `restored=False`).
            intent_entry = entry.model_copy(
                update={"phase": "intent", "intent_id": uuid.uuid4().hex, "operation": "restore"}
            )
            _append_and_sync(manifest_fh, intent_entry)

            try:
                _atomic_move(entry.vault_path, entry.original_path, is_dir=entry.is_dir)
            except (OSError, VaultIntegrityError) as exc:
                logger.warning(
                    "executor.restore_item_failed",
                    path=str(entry.original_path),
                    error=str(exc),
                )
                _append_and_sync(manifest_fh, intent_entry.model_copy(update={"phase": "aborted"}))
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

            # ADR-0026, phase 2: the file is now back at original_path — log it done, fsynced.
            # A kill between the two `_append_and_sync` calls above leaves an intent whose
            # target (original_path) now exists and whose source (vault_path) is gone —
            # reconciled as "completed" (restored=True is synthesized by `reclaim.recovery`).
            _append_and_sync(
                manifest_fh,
                intent_entry.model_copy(
                    update={"phase": "done", "restored": True, "restored_at": now_ts}
                ),
            )
            items.append(
                RestoreItemResult(
                    original_path=entry.original_path,
                    size_bytes=entry.size_bytes,
                    succeeded=True,
                    already_restored=False,
                    error=None,
                )
            )
    finally:
        if manifest_fh is not None:
            _close_manifest_for_sync(manifest_fh)

    succeeded_items = [item for item in items if item.succeeded]
    unsupported_items = [item for item in items if item.restore_unsupported]
    failed_items = [item for item in items if not item.succeeded and not item.restore_unsupported]
    return RestoreReport(
        batch_id=batch_id,
        started_at=now_ts,
        finished_at=time.time(),
        items=tuple(items),
        files_processed=len(items),
        files_succeeded=len(succeeded_items),
        files_failed=len(failed_items),
        files_unsupported=len(unsupported_items),
        bytes_restored=sum(
            item.size_bytes for item in succeeded_items if not item.already_restored
        ),
    )
