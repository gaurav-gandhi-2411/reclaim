from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from reclaim.config import Config
from reclaim.executor import (
    QuarantineManifestEntry,
    append_manifest_entries,
    fold_latest_manifest_entries,
    read_manifest_entries,
)
from reclaim.models import Tier
from reclaim.recovery import compute_reconciliation, reconcile_manifest
from reclaim.safety import SafetyValidator

# Mirrors `tests/_recovery_crash_harness.py`'s own constants — kept as a literal here rather
# than importing the harness module (`tests/` carries no `__init__.py`, so it isn't a real
# package; the harness is invoked as a subprocess, never imported in-process).
_CRASH_EXIT_CODE = 9
_NO_CRASH_FIRED_EXIT_CODE = 2

_HARNESS_PATH = Path(__file__).parent / "_recovery_crash_harness.py"
_NOW = 1_700_000_000.0
_DAY = 86400.0


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


def _run_harness(config: dict[str, object], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Runs `tests/_recovery_crash_harness.py` as a genuinely separate child process (never
    in-process) so `os._exit()` inside it is a real simulated hard-crash, not a caught Python
    exception — see the harness module's own header comment for why that distinction matters.
    """
    config_path = tmp_path / "_harness_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, trusted local test fixture
        [sys.executable, str(_HARNESS_PATH), str(config_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _assert_hard_crash(result: subprocess.CompletedProcess[str]) -> None:
    """The one assertion every crash-harness test must make before trusting anything else: the
    child process genuinely hit the installed `os._exit()` hook, not merely returned normally
    (`_NO_CRASH_FIRED_EXIT_CODE`, a harness/test-config bug) or raised an uncaught Python
    exception (any other code, typically `1`) — either of which would mean the rest of the test
    isn't actually exercising a hard-crash scenario at all.
    """
    assert result.returncode == _CRASH_EXIT_CODE, (
        f"expected the crash hook to fire (exit {_CRASH_EXIT_CODE}), got "
        f"{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _vault_entry(
    tmp_path: Path,
    *,
    original_path: Path | None = None,
    vault_path: Path | None = None,
    batch_id: str = "batch_test",
    quarantined_at: float = _NOW - 40 * _DAY,
    retention_until: float | None = _NOW - 10 * _DAY,
    retention_days: int = 30,
    restored: bool = False,
    purged: bool = False,
    size_bytes: int = 64,
    content: bytes | None = None,
) -> QuarantineManifestEntry:
    """Hand-crafts one already-`done` vaulted entry with a real vault-side file on disk — same
    convention `tests/test_purge.py::_vault_entry` uses (simpler and more direct than running a
    real `apply_batch` call just to get vault state set up for a restore/purge test)."""
    resolved_vault_path = (
        vault_path if vault_path is not None else tmp_path / "vault" / f"{batch_id}_item.bin"
    )
    resolved_vault_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_vault_path.write_bytes(content if content is not None else b"x" * size_bytes)
    resolved_size = len(content) if content is not None else size_bytes

    return QuarantineManifestEntry(
        batch_id=batch_id,
        original_path=original_path if original_path is not None else tmp_path / "gone.bin",
        size_bytes=resolved_size,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        rationale="test rationale",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=resolved_vault_path,
        retention_days=retention_days,
        quarantined_at=quarantined_at,
        retention_until=retention_until,
        restored=restored,
        purged=purged,
    )


def _apply_items(tmp_path: Path, count: int) -> list[dict[str, object]]:
    """Real fixture files under `tmp_path`, distinct content per item, for the harness's
    `operation="apply"` config — at least 3-5 items so there's a clear before/at/after
    position to crash at (item index `count // 2`, in practice)."""
    items: list[dict[str, object]] = []
    for i in range(count):
        content = f"item-{i}-payload".encode() * 4
        path = tmp_path / f"source_{i}.bin"
        path.write_bytes(content)
        items.append({"path": str(path), "size_bytes": len(content)})
    return items


# --- apply: crash simulated via a genuine child-process hard kill ------------------------------


def test_apply_crash_after_intent_fsync_before_action_reconciles_as_aborted(
    tmp_path: Path,
) -> None:
    """Kill mid-batch right after item 2's intent is durably written but before its real vault
    move runs. Items 0-1 fully completed (real moves + done records); items 3-4 never started
    at all. Proves: the crashed item's source is untouched, its vault copy was never created,
    recovery classifies it `aborted`, and the fold-based "current state" view never mistakes it
    for a completed quarantine."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    items = _apply_items(tmp_path, 5)
    crash_index = 2

    result = _run_harness(
        {
            "operation": "apply",
            "checkpoint": "after_intent_fsync",
            "crash_index": crash_index,
            "manifest_path": str(manifest_path),
            "vault_dir": str(vault_dir),
            "now": _NOW,
            "items": items,
        },
        tmp_path,
    )
    _assert_hard_crash(result)

    crashed_source = Path(items[crash_index]["path"])  # type: ignore[arg-type]
    assert crashed_source.exists()  # untouched — the move never ran

    raw_entries = read_manifest_entries(manifest_path)
    # items 0-1: intent+done pairs (4 lines); item 2: intent only (1 line) = 5 total.
    assert len(raw_entries) == 5
    crashed_entries = [e for e in raw_entries if e.original_path == crashed_source]
    assert len(crashed_entries) == 1
    assert crashed_entries[0].phase == "intent"
    assert crashed_entries[0].operation == "apply"
    assert crashed_entries[0].intent_id is not None
    assert crashed_entries[0].vault_path is not None
    assert not crashed_entries[0].vault_path.exists()  # never created

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert preview.scanned_intents == 3  # items 0, 1, 2 each wrote one intent entry
    assert preview.already_resolved == 2  # items 0, 1 already have a matching done entry
    assert len(preview.reconciled) == 1
    assert preview.reconciled[0].outcome == "aborted"
    assert preview.reconciled[0].original_path == crashed_source

    report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    assert len(report.reconciled) == 1
    folded = fold_latest_manifest_entries(manifest_path)
    assert len(folded) == 2  # only items 0 and 1 ever completed
    assert crashed_source not in {e.original_path for e in folded}

    second_report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 2)
    assert second_report.reconciled == ()  # idempotent: nothing left to reconcile


def test_apply_crash_after_action_before_done_fsync_reconciles_as_completed(
    tmp_path: Path,
) -> None:
    """Kill mid-batch right after item 2's real vault move genuinely completes, but before the
    `done` record reaches disk. Proves: recovery classifies the crashed item `completed` (not
    `aborted`) purely from real on-disk state, synthesizes the missing `done` record, and the
    fold-based view then reports it exactly as if the crash never happened."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    items = _apply_items(tmp_path, 5)
    crash_index = 2

    result = _run_harness(
        {
            "operation": "apply",
            "checkpoint": "after_action_before_done_fsync",
            "crash_index": crash_index,
            "manifest_path": str(manifest_path),
            "vault_dir": str(vault_dir),
            "now": _NOW,
            "items": items,
        },
        tmp_path,
    )
    _assert_hard_crash(result)

    crashed_source = Path(items[crash_index]["path"])  # type: ignore[arg-type]
    assert not crashed_source.exists()  # the move genuinely happened

    raw_entries = read_manifest_entries(manifest_path)
    assert len(raw_entries) == 5
    crashed_entries = [e for e in raw_entries if e.original_path == crashed_source]
    assert len(crashed_entries) == 1
    assert crashed_entries[0].phase == "intent"
    assert crashed_entries[0].vault_path is not None
    assert crashed_entries[0].vault_path.exists()  # the vault copy is real
    expected_content = f"item-{crash_index}-payload".encode() * 4
    assert crashed_entries[0].vault_path.read_bytes() == expected_content

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert len(preview.reconciled) == 1
    assert preview.reconciled[0].outcome == "completed"

    reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    folded = fold_latest_manifest_entries(manifest_path)
    assert len(folded) == 3  # items 0, 1, and now the recovered item 2
    folded_by_path = {e.original_path: e for e in folded}
    assert folded_by_path[crashed_source].vault_path == crashed_entries[0].vault_path
    assert folded_by_path[crashed_source].phase == "done"

    second_report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 2)
    assert second_report.reconciled == ()


# --- restore: crash simulated via a genuine child-process hard kill -----------------------------


def _setup_restore_batch(tmp_path: Path, count: int) -> tuple[Path, Path, str, list[Path]]:
    """Hand-crafts `count` already-vaulted (`done`, `restored=False`) entries sharing one
    `batch_id`, each with real vault-side bytes and an as-yet-absent `original_path` — the
    exact post-`apply_batch` state `restore_batch` expects to find."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    batch_id = "batch_restore_test"
    original_paths: list[Path] = []
    entries = []
    for i in range(count):
        original_path = tmp_path / f"restore_target_{i}.bin"
        entry = _vault_entry(
            tmp_path,
            original_path=original_path,
            vault_path=vault_dir / batch_id / f"vault_item_{i}.bin",
            batch_id=batch_id,
            content=f"restore-item-{i}-payload".encode() * 4,
        )
        entries.append(entry)
        original_paths.append(original_path)
    append_manifest_entries(manifest_path, entries)
    return manifest_path, vault_dir, batch_id, original_paths


def test_restore_crash_after_intent_fsync_before_action_reconciles_as_aborted(
    tmp_path: Path,
) -> None:
    """Kill mid-restore right after item 2's restore intent is durably written but before the
    file actually moves back. Proves: the vault copy is untouched, the original location stays
    absent, recovery classifies it `aborted`, and the item's fold-based state stays exactly
    "still vaulted, not restored" — never silently marked restored."""
    manifest_path, vault_dir, batch_id, original_paths = _setup_restore_batch(tmp_path, 5)
    crash_index = 2
    crashed_original = original_paths[crash_index]

    result = _run_harness(
        {
            "operation": "restore",
            "checkpoint": "after_intent_fsync",
            "crash_index": crash_index,
            "manifest_path": str(manifest_path),
            "vault_dir": str(vault_dir),
            "now": _NOW,
            "batch_id": batch_id,
        },
        tmp_path,
    )
    _assert_hard_crash(result)

    assert not crashed_original.exists()  # restore never happened

    raw_entries = read_manifest_entries(manifest_path)
    # 5 original apply-done entries + items 0-1 restore intent/done pairs (4) + item 2's
    # restore intent only (1) = 10.
    assert len(raw_entries) == 10
    restore_intents = [
        e for e in raw_entries if e.original_path == crashed_original and e.operation == "restore"
    ]
    assert len(restore_intents) == 1
    assert restore_intents[0].phase == "intent"
    crashed_vault_path = restore_intents[0].vault_path
    assert crashed_vault_path is not None
    assert crashed_vault_path.exists()  # vault copy never moved

    preview = compute_reconciliation(manifest_path, vault_dir)
    reconciled_for_item = [r for r in preview.reconciled if r.original_path == crashed_original]
    assert len(reconciled_for_item) == 1
    assert reconciled_for_item[0].outcome == "aborted"

    reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    folded = fold_latest_manifest_entries(manifest_path)
    folded_by_path = {e.original_path: e for e in folded}
    # The current-state view still resolves to the ORIGINAL apply's done entry (restored=False,
    # vault_path intact) — the aborted restore attempt never overwrote that fact.
    assert folded_by_path[crashed_original].restored is False
    assert folded_by_path[crashed_original].vault_path == crashed_vault_path

    second_report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 2)
    assert second_report.reconciled == ()


def test_restore_crash_after_action_before_done_fsync_reconciles_as_completed(
    tmp_path: Path,
) -> None:
    """Kill mid-restore right after item 2's file genuinely moves back to `original_path`, but
    before the `done`/`restored=True` record reaches disk. Proves: recovery classifies it
    `completed`, synthesizes `restored=True`, and the fold-based view then correctly reports it
    restored."""
    manifest_path, vault_dir, batch_id, original_paths = _setup_restore_batch(tmp_path, 5)
    crash_index = 2
    crashed_original = original_paths[crash_index]

    result = _run_harness(
        {
            "operation": "restore",
            "checkpoint": "after_action_before_done_fsync",
            "crash_index": crash_index,
            "manifest_path": str(manifest_path),
            "vault_dir": str(vault_dir),
            "now": _NOW,
            "batch_id": batch_id,
        },
        tmp_path,
    )
    _assert_hard_crash(result)

    assert crashed_original.exists()  # the restore genuinely happened
    expected_content = f"restore-item-{crash_index}-payload".encode() * 4
    assert crashed_original.read_bytes() == expected_content

    raw_entries = read_manifest_entries(manifest_path)
    restore_intents = [
        e for e in raw_entries if e.original_path == crashed_original and e.operation == "restore"
    ]
    assert len(restore_intents) == 1
    assert restore_intents[0].phase == "intent"
    crashed_vault_path = restore_intents[0].vault_path
    assert crashed_vault_path is not None
    assert not crashed_vault_path.exists()  # moved away for real

    preview = compute_reconciliation(manifest_path, vault_dir)
    reconciled_for_item = [r for r in preview.reconciled if r.original_path == crashed_original]
    assert len(reconciled_for_item) == 1
    assert reconciled_for_item[0].outcome == "completed"

    reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    folded = fold_latest_manifest_entries(manifest_path)
    folded_by_path = {e.original_path: e for e in folded}
    assert folded_by_path[crashed_original].restored is True
    assert folded_by_path[crashed_original].restored_at == _NOW + 1

    second_report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 2)
    assert second_report.reconciled == ()


# --- purge: crash simulated via a genuine child-process hard kill -------------------------------


def _setup_purge_entries(
    tmp_path: Path, count: int
) -> tuple[Path, Path, list[QuarantineManifestEntry]]:
    """Hand-crafts `count` already-vaulted, retention-expired entries (real vault-side bytes,
    each in its own subdirectory so ordering across purge's eligibility scan is deterministic).
    """
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    entries = []
    for i in range(count):
        entry = _vault_entry(
            tmp_path,
            original_path=tmp_path / f"purge_gone_{i}.bin",
            vault_path=vault_dir / f"item_{i}" / "payload.bin",
            batch_id=f"batch_purge_{i}",
            content=f"purge-item-{i}-payload".encode() * 4,
        )
        entries.append(entry)
    append_manifest_entries(manifest_path, entries)
    return manifest_path, vault_dir, entries


def test_purge_crash_after_intent_fsync_before_action_reconciles_as_aborted(
    tmp_path: Path,
) -> None:
    """Kill mid-purge right after item 2's purge intent is durably written but before the vault
    copy is actually deleted. Proves: the vault file survives untouched, recovery classifies it
    `aborted`, and the item stays fully current (still vaulted, `purged=False`, purge-eligible
    again on the next run)."""
    manifest_path, vault_dir, entries = _setup_purge_entries(tmp_path, 5)
    crash_index = 2
    crashed_entry = entries[crash_index]
    assert crashed_entry.vault_path is not None

    result = _run_harness(
        {
            "operation": "purge",
            "checkpoint": "after_intent_fsync",
            "crash_index": crash_index,
            "manifest_path": str(manifest_path),
            "vault_dir": str(vault_dir),
            "now": _NOW,
        },
        tmp_path,
    )
    _assert_hard_crash(result)

    assert crashed_entry.vault_path.exists()  # never deleted

    raw_entries = read_manifest_entries(manifest_path)
    # 5 original done entries + items 0-1 purge intent/done pairs (4) + item 2's intent only
    # (1) = 10.
    assert len(raw_entries) == 10
    purge_intents = [
        e
        for e in raw_entries
        if e.original_path == crashed_entry.original_path and e.operation == "purge"
    ]
    assert len(purge_intents) == 1
    assert purge_intents[0].phase == "intent"

    preview = compute_reconciliation(manifest_path, vault_dir)
    reconciled_for_item = [
        r for r in preview.reconciled if r.original_path == crashed_entry.original_path
    ]
    assert len(reconciled_for_item) == 1
    assert reconciled_for_item[0].outcome == "aborted"

    reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    folded = fold_latest_manifest_entries(manifest_path)
    folded_by_path = {e.original_path: e for e in folded}
    assert folded_by_path[crashed_entry.original_path].purged is False
    assert folded_by_path[crashed_entry.original_path].vault_path == crashed_entry.vault_path

    second_report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 2)
    assert second_report.reconciled == ()


def test_purge_crash_after_action_before_done_fsync_reconciles_as_completed(
    tmp_path: Path,
) -> None:
    """Kill mid-purge right after item 2's vault copy is genuinely, permanently deleted, but
    before the `done`/`purged=True` record reaches disk. Proves: recovery classifies it
    `completed`, synthesizes `purged=True`, and the fold-based view then correctly reports it
    purged (never purge-eligible again, never mistaken for still-vaulted)."""
    manifest_path, vault_dir, entries = _setup_purge_entries(tmp_path, 5)
    crash_index = 2
    crashed_entry = entries[crash_index]
    assert crashed_entry.vault_path is not None

    result = _run_harness(
        {
            "operation": "purge",
            "checkpoint": "after_action_before_done_fsync",
            "crash_index": crash_index,
            "manifest_path": str(manifest_path),
            "vault_dir": str(vault_dir),
            "now": _NOW,
        },
        tmp_path,
    )
    _assert_hard_crash(result)

    assert not crashed_entry.vault_path.exists()  # genuinely, permanently gone

    raw_entries = read_manifest_entries(manifest_path)
    purge_intents = [
        e
        for e in raw_entries
        if e.original_path == crashed_entry.original_path and e.operation == "purge"
    ]
    assert len(purge_intents) == 1
    assert purge_intents[0].phase == "intent"

    preview = compute_reconciliation(manifest_path, vault_dir)
    reconciled_for_item = [
        r for r in preview.reconciled if r.original_path == crashed_entry.original_path
    ]
    assert len(reconciled_for_item) == 1
    assert reconciled_for_item[0].outcome == "completed"

    reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    folded = fold_latest_manifest_entries(manifest_path)
    folded_by_path = {e.original_path: e for e in folded}
    assert folded_by_path[crashed_entry.original_path].purged is True
    assert folded_by_path[crashed_entry.original_path].purged_at == _NOW + 1

    second_report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 2)
    assert second_report.reconciled == ()


# --- a real caught per-item exception self-resolves; recovery has nothing to do ----------------
#
# Distinct from every test above: no crash simulation at all. A missing source/vault-side file
# forces the production code's own EXISTING per-item `except`/abort path to run for real (the
# same path `test_executor.py`/`test_purge.py` already cover for their own error-reporting
# assertions) — used here only to prove the OTHER half of ADR-0026's contract: a caught,
# handled failure closes its own intent immediately, so `reclaim.recovery` finds nothing
# orphaned for it at all. A permission-denied error would be the more obviously "real" failure
# per the task's own example, but this suite's `conftest.py` already documents why permission
# checks are unreliable on this project's CI runners (GitHub Actions' Windows runners execute
# as genuinely elevated Administrators, so a chmod-based read-only guard doesn't reliably
# block anything) — a missing source/vault file forces the identical `except`-and-abort code
# path deterministically, on any runner, without depending on OS permission enforcement.


def test_apply_real_failure_self_resolves_as_aborted_without_recovery(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    missing_source = tmp_path / "does_not_exist.bin"  # never created

    from reclaim.executor import apply_batch
    from reclaim.models import Candidate, Verdict

    candidate = Candidate(
        path=missing_source,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        size_bytes=10,
        tier=Tier.A,
        rationale="test",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="TEST_REASON",
        retention_days=30,
    )
    report = apply_batch(
        [candidate],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    assert report.files_failed == 1

    raw_entries = read_manifest_entries(manifest_path)
    assert len(raw_entries) == 2  # intent + aborted, written synchronously by apply_batch itself
    assert [e.phase for e in raw_entries] == ["intent", "aborted"]
    assert raw_entries[0].intent_id == raw_entries[1].intent_id

    # Nothing for recovery to do: the failure already resolved its own intent.
    preview = compute_reconciliation(manifest_path, vault_dir)
    assert preview.scanned_intents == 1
    assert preview.already_resolved == 1
    assert preview.reconciled == ()
    assert fold_latest_manifest_entries(manifest_path) == []


def test_restore_real_failure_self_resolves_as_aborted_without_recovery(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    batch_id = "batch_missing_vault"
    original_path = tmp_path / "restore_target.bin"
    # A vault entry whose recorded vault_path was never actually written to disk.
    missing_vault_path = vault_dir / batch_id / "ghost.bin"
    entry = QuarantineManifestEntry(
        batch_id=batch_id,
        original_path=original_path,
        size_bytes=10,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        rationale="test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=missing_vault_path,
        retention_days=30,
        quarantined_at=_NOW - 40 * _DAY,
        retention_until=_NOW - 10 * _DAY,
    )
    append_manifest_entries(manifest_path, [entry])

    from reclaim.executor import restore_batch

    report = restore_batch(
        batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW,
    )
    assert report.files_failed == 1

    raw_entries = read_manifest_entries(manifest_path)
    # 1 original apply-done entry + 1 restore intent + 1 restore aborted = 3.
    assert len(raw_entries) == 3
    restore_entries = [e for e in raw_entries if e.operation == "restore"]
    assert [e.phase for e in restore_entries] == ["intent", "aborted"]
    assert restore_entries[0].intent_id == restore_entries[1].intent_id

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert preview.reconciled == ()

    folded = fold_latest_manifest_entries(manifest_path)
    assert len(folded) == 1
    assert folded[0].restored is False  # unaffected by the failed restore attempt


def test_purge_real_failure_self_resolves_as_aborted_without_recovery(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    original_path = tmp_path / "purge_target_gone.bin"
    # A vault entry whose recorded vault_path was never actually written to disk.
    missing_vault_path = vault_dir / "ghost.bin"
    entry = QuarantineManifestEntry(
        batch_id="batch_missing_vault",
        original_path=original_path,
        size_bytes=10,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        rationale="test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=missing_vault_path,
        retention_days=30,
        quarantined_at=_NOW - 40 * _DAY,
        retention_until=_NOW - 10 * _DAY,
    )
    append_manifest_entries(manifest_path, [entry])

    from reclaim.purge import purge_expired

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW,
    )
    assert report.files_failed == 1

    raw_entries = read_manifest_entries(manifest_path)
    assert len(raw_entries) == 3  # original done + purge intent + purge aborted
    purge_entries = [e for e in raw_entries if e.operation == "purge"]
    assert [e.phase for e in purge_entries] == ["intent", "aborted"]
    assert purge_entries[0].intent_id == purge_entries[1].intent_id

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert preview.reconciled == ()

    folded = fold_latest_manifest_entries(manifest_path)
    assert len(folded) == 1
    assert folded[0].purged is False  # unaffected by the failed purge attempt


# --- needs_review: hand-crafted disk state, no crash simulation needed --------------------------


def _orphaned_intent_entry(
    tmp_path: Path,
    *,
    original_path: Path,
    vault_path: Path,
) -> QuarantineManifestEntry:
    return QuarantineManifestEntry(
        batch_id="batch_needs_review",
        original_path=original_path,
        size_bytes=10,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        rationale="test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=vault_path,
        retention_days=30,
        quarantined_at=_NOW - 40 * _DAY,
        retention_until=_NOW - 10 * _DAY,
        phase="intent",
        intent_id="orphan-1",
        operation="apply",
    )


def test_needs_review_when_both_source_and_target_exist(tmp_path: Path) -> None:
    """An orphaned intent whose source AND target both exist is never guessed at (e.g. the
    cross-volume copy-fallback window where a copy can succeed before the source is removed) —
    ADR-0026's "both or neither -> needs_review, never guessed" rule."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    original_path = tmp_path / "ambiguous_source.bin"
    vault_path = vault_dir / "ambiguous_target.bin"
    original_path.write_bytes(b"still here")
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_bytes(b"also here")
    append_manifest_entries(
        manifest_path,
        [_orphaned_intent_entry(tmp_path, original_path=original_path, vault_path=vault_path)],
    )

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert len(preview.reconciled) == 1
    assert preview.reconciled[0].outcome == "needs_review"
    assert "both" in preview.reconciled[0].detail

    reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    assert fold_latest_manifest_entries(manifest_path) == []  # needs_review never folds as done

    # Idempotent, but NOT auto-resolved: a needs_review verdict stays flagged for a human even
    # on a repeat run, it is never silently re-classified.
    second_preview = compute_reconciliation(manifest_path, vault_dir)
    assert second_preview.reconciled == ()
    second_report = reconcile_manifest(manifest_path, vault_dir, now=_NOW + 2)
    assert second_report.reconciled == ()


def test_needs_review_when_neither_source_nor_target_exist(tmp_path: Path) -> None:
    """An orphaned intent whose source AND target are BOTH absent is equally never guessed at —
    there's no way to know whether the action ran, moved something unexpected, or never
    started."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    original_path = tmp_path / "vanished_source.bin"
    vault_path = vault_dir / "vanished_target.bin"
    # Neither path is ever created on disk.
    append_manifest_entries(
        manifest_path,
        [_orphaned_intent_entry(tmp_path, original_path=original_path, vault_path=vault_path)],
    )

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert len(preview.reconciled) == 1
    assert preview.reconciled[0].outcome == "needs_review"
    assert "neither" in preview.reconciled[0].detail


# --- zip-slip-equivalent guard: vault_path outside vault_dir always forces needs_review --------


@pytest.mark.parametrize(
    "make_disk_state",
    [
        pytest.param(
            lambda original, escaped: (original.write_bytes(b"present"), None),
            id="source_exists_only",  # would otherwise classify 'aborted'
        ),
        pytest.param(
            lambda original, escaped: (escaped.write_bytes(b"present"), None),
            id="target_exists_only",  # would otherwise classify 'completed'
        ),
        pytest.param(
            lambda original, escaped: (None, None),
            id="neither_exists",  # would otherwise classify 'needs_review' anyway
        ),
    ],
)
def test_vault_path_outside_vault_dir_always_forces_needs_review(
    tmp_path: Path, make_disk_state: object
) -> None:
    """A recorded `vault_path` that doesn't resolve inside the configured `vault_dir` is never
    trusted enough to synthesize a `completed`/`aborted` verdict from, REGARDLESS of what the
    source/target on-disk state would otherwise indicate — the zip-slip-equivalent guard
    `executor.RestoreIntegrityError` already applies to restore, mirrored here for recovery."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    original_path = tmp_path / "source.bin"
    # Escapes vault_dir entirely — a sibling directory, not a descendant of it.
    escaped_vault_path = tmp_path / "outside_vault" / "escaped.bin"
    escaped_vault_path.parent.mkdir(parents=True, exist_ok=True)

    make_disk_state(original_path, escaped_vault_path)  # type: ignore[operator]

    append_manifest_entries(
        manifest_path,
        [
            _orphaned_intent_entry(
                tmp_path, original_path=original_path, vault_path=escaped_vault_path
            )
        ],
    )

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert len(preview.reconciled) == 1
    assert preview.reconciled[0].outcome == "needs_review"
    assert "does not resolve inside the configured vault directory" in preview.reconciled[0].detail
