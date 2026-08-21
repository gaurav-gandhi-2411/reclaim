from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reclaim.config import Config
from reclaim.executor import (
    ItemApplyResult,
    RestoreItemResult,
    apply_batch,
    fold_latest_manifest_entries,
    read_manifest_entries,
    restore_batch,
)
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Tier, Verdict
from reclaim.recovery import compute_reconciliation, reconcile_manifest
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

# P0-K1a (this session, live-reproduced finding): `apply_batch` acted on stale DB-index data
# with zero re-verification against live filesystem state at mutation time -- swapping content
# at a candidate's path between scan and apply caused the swapped content to be permanently
# deleted or misrouted into the vault. This file is the safety-named home for the six teeth-
# proofs the fix's design brief requires (M4), plus the "still works normally" regression tests
# (M4 (vi)) -- registered in `scripts/verify.py`'s `_SAFETY_GATE_FILES` tuple, same "no excuse to
# skip it" reasoning as every other file in that tuple.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner/executor target Windows/NTFS only")

_NOW = 1_700_000_000.0


# --- Shared test helpers (duplicated from evals/test_apply_safety_preflight.py -- see that
# file's own comment for why these are duplicated, not imported, across evals/tests files) -----


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


def _candidate(
    path: Path,
    *,
    is_dir: bool = False,
    size_bytes: int = 100,
    category: str = "test_category",
    category_group: str = "test_group",
    tier: Tier = Tier.A,
    retention_days: int | None = 30,
    dev: int = 0,
    ino: int = 0,
    mtime: float = 0.0,
    rebuildable: bool = False,
) -> Candidate:
    return Candidate(
        path=path,
        is_dir=is_dir,
        category=category,
        category_group=category_group,
        size_bytes=size_bytes,
        tier=tier,
        rationale="test rationale",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="TEST_REASON",
        retention_days=retention_days,
        dev=dev,
        ino=ino,
        mtime=mtime,
        rebuildable=rebuildable,
    )


def _identity_candidate_for(path: Path, index: ScanIndex, **kwargs: object) -> Candidate:
    """Builds a `Candidate` for `path` using the exact scan-time `(dev, ino, mtime)` baseline
    `index` recorded for it -- the same wiring `detectors.generate_candidates` does for real
    (`models.Candidate`'s own field comment documents the three real construction sites)."""
    record = index.get_record(path)
    assert record is not None, f"no scan record for {path}"
    return _candidate(path, dev=record.dev, ino=record.ino, mtime=record.mtime, **kwargs)  # type: ignore[arg-type]


def _apply_result_for(report_items: tuple[ItemApplyResult, ...], path: Path) -> ItemApplyResult:
    for item in report_items:
        if item.path == path:
            return item
    raise AssertionError(f"no ItemApplyResult for {path} in {report_items}")


def _restore_result_for(
    report_items: tuple[RestoreItemResult, ...], path: Path
) -> RestoreItemResult:
    for item in report_items:
        if item.original_path == path:
            return item
    raise AssertionError(f"no RestoreItemResult for {path} in {report_items}")


def _make_junction(link: Path, target: Path) -> None:
    """Real NTFS junction via `mklink /J` -- same technique `tests/test_scanner.py`/
    `tests/test_safety.py`/`evals/test_safety_adversarial.py` already use. Skips (not fails) if
    this machine can't create one, same convention those files use."""
    result = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 -- cmd is a builtin
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create NTFS junction: {result.stderr or result.stdout}")


def _build_nested_cache_tree(root: Path) -> Path:
    """`root/cache_dir/level1/level2/file.bin` -- a depth-2 nested structure, the shape M1's
    full-subtree re-walk exists to protect (a top-level identity check alone can't see a change
    this deep)."""
    cache_dir = root / "cache_dir"
    nested_file = cache_dir / "level1" / "level2" / "file.bin"
    nested_file.parent.mkdir(parents=True)
    nested_file.write_bytes(b"nested-cache-content")
    return cache_dir


# --- (i) delete-and-recreate at the same path -> must skip -------------------------------------


def test_delete_and_recreate_at_same_path_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"original-content")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(target, index, size_bytes=17, retention_days=30)

    target.unlink()
    target.write_bytes(b"SWAPPED-CONTENT!!")  # new inode at the exact same path

    report = apply_batch(
        [candidate],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
    )
    result = _apply_result_for(report.items, target)
    assert result.succeeded is False
    assert result.skip_reason == "identity_changed_since_scan"
    assert result.error is None  # never attempted -- not an OS error
    assert target.exists()
    assert target.read_bytes() == b"SWAPPED-CONTENT!!"  # untouched, never vaulted


# --- (ii) junction repoint -> must skip ---------------------------------------------------------


def test_junction_repoint_is_skipped(tmp_path: Path) -> None:
    """Confirms `follow_symlinks=False` gives the equivalent protection
    `FILE_FLAG_OPEN_REPARSE_POINT` would -- a real `mklink /J` junction, repointed (deleted and
    recreated pointing somewhere else) between scan and apply, not mocked."""
    target_a = tmp_path / "target_a"
    target_a.mkdir()
    (target_a / "a.txt").write_text("from target A", encoding="utf-8")
    target_b = tmp_path / "target_b"
    target_b.mkdir()
    (target_b / "b.txt").write_text("from target B", encoding="utf-8")

    link = tmp_path / "cache_link"
    _make_junction(link, target_a)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            link, index, is_dir=True, size_bytes=0, retention_days=30
        )

    link.rmdir()  # removes the reparse point only -- target_a's own contents are untouched
    _make_junction(link, target_b)

    report = apply_batch(
        [candidate],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
    )
    result = _apply_result_for(report.items, link)
    assert result.succeeded is False
    assert result.skip_reason == "identity_changed_since_scan"
    assert link.exists()
    assert (target_a / "a.txt").exists()  # original target: untouched
    assert (target_b / "b.txt").exists()  # new target: untouched, never vaulted either


# --- (iii) directory contents changed at depth >= 2, direct-delete category -> must skip -------


def test_direct_delete_directory_content_changed_at_depth_two_is_skipped(tmp_path: Path) -> None:
    cache_dir = _build_nested_cache_tree(tmp_path)
    nested_file = cache_dir / "level1" / "level2" / "file.bin"

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=21, retention_days=None
        )

        nested_file.unlink()
        nested_file.write_bytes(b"SWAPPED-NESTED!!")  # new inode, two levels deep

        report = apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",  # irrelevant: retention_days=None forces direct_delete (ADR-0001)
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=index,
        )

    result = _apply_result_for(report.items, cache_dir)
    assert result.succeeded is False
    assert result.skip_reason == "identity_changed_since_scan"
    assert result.method == "direct_delete"
    assert cache_dir.exists()  # untouched -- the whole candidate was skipped, not just the file
    assert nested_file.read_bytes() == b"SWAPPED-NESTED!!"


# --- (iv) directory contents changed at depth >= 2, VAULTED category -> documented gap ---------


def test_vaulted_directory_content_changed_at_depth_two_is_not_caught_by_design(
    tmp_path: Path,
) -> None:
    """M1's tiered-by-recoverability design: only `retention_days is None` (direct-delete,
    irreversible) directory candidates get the full subtree re-walk. A vaulted (recoverable)
    directory candidate only ever gets the cheaper top-level `(dev, ino)` check -- unchanged
    here, since only a NESTED file changed -- so this apply proceeds normally despite the depth-2
    content change. This is the accepted, disclosed trade-off (M1/M5), not a bug: unlike a
    direct-delete, a mistaken vault move is still recoverable via `restore_batch`."""
    cache_dir = _build_nested_cache_tree(tmp_path)
    nested_file = cache_dir / "level1" / "level2" / "file.bin"

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=21, retention_days=30
        )

        nested_file.unlink()
        nested_file.write_bytes(b"SWAPPED-NESTED!!")  # new inode, two levels deep

        report = apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=index,
        )

    result = _apply_result_for(report.items, cache_dir)
    assert result.succeeded is True  # NOT caught -- documented gap, top-level identity unchanged
    assert result.skip_reason is None
    assert result.method == "vault"
    assert not cache_dir.exists()  # genuinely vaulted, swapped nested content and all
    assert result.vault_path is not None
    assert (result.vault_path / "level1" / "level2" / "file.bin").read_bytes() == (
        b"SWAPPED-NESTED!!"
    )


# --- (v) restore_batch into an occupied destination -> must skip, not overwrite ----------------


def test_restore_into_occupied_destination_is_skipped_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    original_content = b"vaulted-original-content"
    target.write_bytes(original_content)

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"

    apply_report = apply_batch(
        [_candidate(target, size_bytes=len(original_content))],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    assert apply_report.files_succeeded == 1
    vault_path = apply_report.items[0].vault_path
    assert vault_path is not None
    assert not target.exists()

    occupier_content = b"UNRELATED-FILE-CREATED-AFTER-QUARANTINE"
    target.write_bytes(occupier_content)

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 10,
    )
    result = _restore_result_for(restore_report.items, target)
    assert result.succeeded is False
    assert result.already_restored is False
    assert result.error is not None
    assert "already exists" in result.error

    assert target.read_bytes() == occupier_content  # occupying content: completely untouched
    assert vault_path.exists()  # vault copy: never consumed, still recoverable
    assert vault_path.read_bytes() == original_content


# --- (vi) unchanged path, all categories -> still deletes/restores normally --------------------


def test_identity_unchanged_direct_delete_still_deletes(tmp_path: Path) -> None:
    target = tmp_path / "orphaned_cache.bin"
    target.write_bytes(b"delete-me")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(target, index, size_bytes=9, retention_days=None)

        report = apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=index,
        )

    result = _apply_result_for(report.items, target)
    assert result.succeeded is True
    assert result.skip_reason is None
    assert result.method == "direct_delete"
    assert not target.exists()


def test_identity_unchanged_vault_still_vaults(tmp_path: Path) -> None:
    cache_dir = _build_nested_cache_tree(tmp_path)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=21, retention_days=30
        )

        report = apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=index,
        )

    result = _apply_result_for(report.items, cache_dir)
    assert result.succeeded is True
    assert result.skip_reason is None
    assert not cache_dir.exists()


def test_identity_unchanged_restore_still_restores(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    original_content = b"restore-me-back"
    target.write_bytes(original_content)

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"

    apply_report = apply_batch(
        [_candidate(target, size_bytes=len(original_content))],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    assert apply_report.files_succeeded == 1
    assert not target.exists()

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 10,
    )
    result = _restore_result_for(restore_report.items, target)
    assert result.succeeded is True
    assert target.exists()
    assert target.read_bytes() == original_content


# --- ADR-0032 (P0-K1a/M1 cost-budget follow-up, P5 teeth-proofs): entry-count-guard downgrade
# + synchronous purge ----------------------------------------------------------------------------
#
# `direct_delete_entry_count_guard` is overridden to a small integer in every test below (the
# established pattern for this class of threshold test -- see `tests/test_executor.py`'s own
# `direct_delete_size_guard_bytes=100` overrides) rather than building a real 87,882-entry
# fixture: the guard's OWN threshold-crossing arithmetic is proven separately (PLAN.md's real
# npm-cache-shaped measurement, cited alongside `executor._DEFAULT_DIRECT_DELETE_ENTRY_COUNT_
# GUARD`) -- what these tests prove is that the DOWNGRADE + SYNCHRONOUS-PURGE mechanism itself is
# correct once the guard fires, independent of what number makes it fire.


def _build_flat_cache_tree(root: Path, *, file_count: int = 5) -> Path:
    """`root/big_cache/file_0.bin` .. `file_{file_count-1}.bin` -- a flat directory with enough
    real entries that `ScanIndex.subtree_entry_count` reports well above a small overridden
    `direct_delete_entry_count_guard`, regardless of whether the directory row itself is counted."""
    cache_dir = root / "big_cache"
    cache_dir.mkdir()
    for i in range(file_count):
        (cache_dir / f"file_{i}.bin").write_bytes(f"payload-{i}".encode())
    return cache_dir


def test_entry_count_guard_downgraded_candidate_with_unchanged_identity_is_not_skipped(
    tmp_path: Path,
) -> None:
    """P5(i): a guard-downgraded (entry-count axis), rebuildable candidate whose identity is
    unchanged since scan passes the SAME cheap top-level `(dev, ino)` check every other vaulted
    candidate gets -- proceeds to a real vault move, then the synchronous purge fires."""
    cache_dir = _build_flat_cache_tree(tmp_path)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=1, retention_days=None, rebuildable=True
        )

        report = apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=index,
            direct_delete_entry_count_guard=2,  # real tree has 5 files -- well above this
        )

    result = _apply_result_for(report.items, cache_dir)
    assert result.method == "vault"  # downgraded from direct_delete by the entry-count guard
    assert result.succeeded is True
    assert result.skip_reason is None
    assert result.synchronously_purged is True
    assert not cache_dir.exists()  # moved into the vault...
    assert result.vault_path is not None
    assert not result.vault_path.exists()  # ...then immediately purged back out again
    assert report.synchronously_purged_count == 1
    assert report.bytes_synchronously_purged == 1
    entries = fold_latest_manifest_entries(tmp_path / "manifest.jsonl")
    matching = [e for e in entries if e.original_path == cache_dir]
    assert len(matching) == 1
    assert matching[0].purged is True
    assert matching[0].retention_days == 0


def test_entry_count_guard_downgraded_candidate_with_identity_mismatch_is_skipped(
    tmp_path: Path,
) -> None:
    """P5(ii): a top-level identity mismatch skips a guard-downgraded candidate exactly the same
    way it skips any other candidate -- never vaulted, never purged, same skip-and-continue
    pattern as `test_delete_and_recreate_at_same_path_is_skipped` above."""
    cache_dir = _build_flat_cache_tree(tmp_path)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=1, retention_days=None, rebuildable=True
        )

        shutil.rmtree(cache_dir)
        cache_dir.mkdir()  # new inode at the exact same top-level path
        (cache_dir / "different_file.bin").write_bytes(b"post-scan-content")

        report = apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=index,
            direct_delete_entry_count_guard=2,
        )

    result = _apply_result_for(report.items, cache_dir)
    assert result.succeeded is False
    assert result.skip_reason == "identity_changed_since_scan"
    assert cache_dir.exists()  # untouched -- never vaulted, never purged
    assert (cache_dir / "different_file.bin").exists()
    assert report.synchronously_purged_count == 0
    assert report.bytes_synchronously_purged == 0


def test_entry_count_guard_downgraded_candidate_bytes_actually_freed(tmp_path: Path) -> None:
    """P5(iii): P1's real mechanism, verified two ways -- the vault copy is gone (not merely
    reported as freed) AND the original location is gone, both genuinely absent from disk, not
    just absent from the report. A real `shutil.disk_usage()` before/after delta on the same
    disposable fixture is measured separately (see this PR's body / PLAN.md's P1 checkpoint);
    this test is the permanent regression guard for the two on-disk existence facts."""
    cache_dir = _build_flat_cache_tree(tmp_path, file_count=8)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=1, retention_days=None, rebuildable=True
        )

        report = apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=index,
            direct_delete_entry_count_guard=2,
        )

    result = _apply_result_for(report.items, cache_dir)
    assert result.synchronously_purged is True
    assert not cache_dir.exists()  # gone from the original location
    assert result.vault_path is not None
    assert not result.vault_path.exists()  # gone from the vault too -- not merely "eligible"


def test_entry_count_guard_crash_between_vault_done_and_purge_intent_is_still_restorable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5(iv), option (a): there IS a real, testable window. A crash (simulated the same way
    `tests/test_recovery.py`'s own ADR-0026 `KeyboardInterrupt` tests do -- monkeypatching the
    real call to raise at a precise point) between the vault "done" manifest write and the purge
    "intent" write leaves the item as a completely ordinary, valid, restorable vault entry --
    `reclaim.recovery` finds nothing to reconcile for it at all (no purge intent was ever
    written), and `restore_batch` restores it exactly as if this fix's synchronous purge never
    existed. This is a durable window, not a brief one: it lasts until whatever *next* runs
    `apply_batch`/`reclaim purge` against this manifest -- indistinguishable from an ordinary
    `retention_days=0` vault entry that simply hasn't been purged yet."""
    import reclaim.executor as executor_module

    cache_dir = _build_flat_cache_tree(tmp_path)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    real_append_and_sync = executor_module._append_and_sync

    def _crash_before_purge_intent(fh: object, entry: object) -> None:
        if getattr(entry, "operation", None) == "purge":
            raise KeyboardInterrupt("simulated crash before the purge intent write lands")
        real_append_and_sync(fh, entry)  # type: ignore[arg-type]

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=1, retention_days=None, rebuildable=True
        )

        monkeypatch.setattr(executor_module, "_append_and_sync", _crash_before_purge_intent)
        with pytest.raises(KeyboardInterrupt):
            apply_batch(
                [candidate],
                safety=_safety(),
                apply=True,
                method="vault",
                vault_dir=vault_dir,
                manifest_path=manifest_path,
                now=_NOW,
                scan_index=index,
                direct_delete_entry_count_guard=2,
            )
    monkeypatch.undo()

    # The vault move itself genuinely completed and was durably recorded -- only the purge
    # attempt was interrupted, before it wrote anything at all.
    raw_entries = read_manifest_entries(manifest_path)
    assert all(e.operation != "purge" for e in raw_entries)
    folded = fold_latest_manifest_entries(manifest_path)
    assert len(folded) == 1
    assert folded[0].original_path == cache_dir
    assert folded[0].method == "vault"
    assert folded[0].purged is False
    assert not cache_dir.exists()  # real move happened
    vault_path = folded[0].vault_path
    assert vault_path is not None
    assert vault_path.exists()  # still genuinely in the vault -- nothing purged it

    # `reclaim recover` finds nothing to reconcile -- there is no orphaned intent of any kind
    # for this item (the crash happened before the purge intent write, not after).
    preview = compute_reconciliation(manifest_path, vault_dir)
    assert preview.reconciled == ()

    batch_id = folded[0].batch_id
    restore_report = restore_batch(
        batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 10,
    )
    result = _restore_result_for(restore_report.items, cache_dir)
    assert result.succeeded is True
    assert cache_dir.exists()  # genuinely restored, exactly like any other vault entry


def test_entry_count_guard_crash_between_purge_intent_and_done_reconciles_as_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P5(iv), the second real window: a crash AFTER the purge intent lands but BEFORE the purge
    "done" write. `reclaim.recovery.reconcile_manifest` already handles this generically for any
    `operation="purge"` intent (the exact mechanism `purge_expired`'s own two-phase writes rely
    on -- `recovery._source_and_target` branches on `operation`, not on which code path wrote the
    intent), so this proves the synchronous purge's manifest writes are fully compatible with the
    EXISTING crash-recovery machinery with zero new code needed in `reclaim.recovery`."""
    import reclaim.executor as executor_module

    cache_dir = _build_flat_cache_tree(tmp_path)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    real_append_and_sync = executor_module._append_and_sync
    calls_seen: list[str] = []

    def _crash_before_purge_done(fh: object, entry: object) -> None:
        operation = getattr(entry, "operation", None)
        phase = getattr(entry, "phase", None)
        if operation == "purge" and phase == "done":
            raise KeyboardInterrupt("simulated crash after the real unlink, before done lands")
        calls_seen.append(f"{operation}:{phase}")
        real_append_and_sync(fh, entry)  # type: ignore[arg-type]

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _identity_candidate_for(
            cache_dir, index, is_dir=True, size_bytes=1, retention_days=None, rebuildable=True
        )

        monkeypatch.setattr(executor_module, "_append_and_sync", _crash_before_purge_done)
        with pytest.raises(KeyboardInterrupt):
            apply_batch(
                [candidate],
                safety=_safety(),
                apply=True,
                method="vault",
                vault_dir=vault_dir,
                manifest_path=manifest_path,
                now=_NOW,
                scan_index=index,
                direct_delete_entry_count_guard=2,
            )
    monkeypatch.undo()

    folded_before = fold_latest_manifest_entries(manifest_path)
    assert len(folded_before) == 1
    vault_path = folded_before[0].vault_path
    assert vault_path is not None
    assert not vault_path.exists()  # the real unlink/rmtree already happened before the crash
    assert folded_before[0].purged is False  # ...but the "done" record never landed

    preview = compute_reconciliation(manifest_path, vault_dir)
    assert len(preview.reconciled) == 1
    assert preview.reconciled[0].operation == "purge"
    assert preview.reconciled[0].outcome == "completed"

    reconcile_manifest(manifest_path, vault_dir, now=_NOW + 1)
    folded_after = fold_latest_manifest_entries(manifest_path)
    assert len(folded_after) == 1
    assert folded_after[0].purged is True  # reconciled from real on-disk state, not guessed


# --- K2a/K2b/K2d (this session's follow-up audit finding): `shutil.rmtree` + `long_path()` +
# junction defeats `apply_batch`'s own success reporting -----------------------------------------
#
# Root cause (K2b): every `shutil.rmtree` call site in `executor.py`/`purge.py` passes
# `onexc=rmtree_clear_readonly` (needed for read-only git packfiles -- ADR-0004). When
# `shutil.rmtree` is handed a Windows junction/symlink directly as its own top-level argument, it
# detects this internally and raises -- but the `func` it hands `onexc` for that specific raise is
# `os.path.islink` (a read-only probe), not a delete call. `rmtree_clear_readonly`'s own contract
# ("chmod, then retry `func(path)`") re-invokes `islink`, discards the return value, and
# `shutil.rmtree` returns NORMALLY having deleted nothing. `apply_batch` previously had no way to
# tell this apart from a genuine success -- see `executor.rmtree_reparse_point_safe`'s own module
# comment for the full mechanism and how K2b fixes it.
#
# Two independent teeth-proofs below: the first proves K2b's actual root-cause fix against a real
# `mklink /J` junction through the full `apply_batch` path; the second proves K2a's post-condition
# CONTRACT independently of K2b, by forcing a hypothetical silent no-op via monkeypatch -- so this
# safety net does not depend on K2b's fix being the only bug of this shape that will ever exist.


def test_direct_delete_junction_is_genuinely_removed_not_silently_noop(tmp_path: Path) -> None:
    """K2b/K2d: before this fix, a direct-delete candidate whose path was itself an NTFS junction
    was silently untouched by `apply_batch` while still being reported `succeeded=True` -- see
    this section's module comment for the exact mechanism. No `scan_index` is passed and no
    scan-time `(dev, ino)` baseline is set (`dev=0, ino=0`, `_candidate`'s own default) so this
    test isolates K2b's fix specifically, independent of M1's separate subtree re-walk."""
    target = tmp_path / "target"
    target.mkdir()
    canary_content = b"must survive -- this is the junction's TARGET, never the candidate itself"
    (target / "canary.txt").write_bytes(canary_content)

    link = tmp_path / "cache_link"
    _make_junction(link, target)

    candidate = _candidate(link, is_dir=True, size_bytes=0, retention_days=None, rebuildable=True)

    report = apply_batch(
        [candidate],
        safety=_safety(),
        apply=True,
        method="vault",  # irrelevant: retention_days=None forces direct_delete (ADR-0001)
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
    )

    result = _apply_result_for(report.items, link)
    assert result.method == "direct_delete"
    assert result.skip_reason is None

    # The load-bearing assertion: never silently "succeeded" with the junction physically
    # untouched. Either it was genuinely removed, or it was explicitly rejected -- never silent.
    if result.succeeded:
        assert result.error is None
        assert result.postcondition_verification_failed is False
        assert not link.exists()
        # Prove the directory ENTRY itself (not merely what it resolved to) is gone, not just
        # left dangling: re-creating a junction at the exact same path only succeeds if nothing
        # (reparse point or otherwise) still occupies it.
        _make_junction(link, target)
        assert link.exists()
        link.rmdir()  # cleanup -- removes only this fresh junction entry, not `target`
    else:
        assert result.error is not None
        assert result.postcondition_verification_failed is True

    # Either way, the junction's TARGET -- a completely separate directory this candidate never
    # named -- must never be touched.
    assert target.exists()
    assert (target / "canary.txt").exists()
    assert (target / "canary.txt").read_bytes() == canary_content

    # Manifest never records a "done" phase for this item unless the delete genuinely happened.
    raw_entries = read_manifest_entries(tmp_path / "manifest.jsonl")
    matching = [e for e in raw_entries if e.original_path == link]
    phases = {e.phase for e in matching}
    if result.succeeded:
        assert phases == {"intent", "done"}
    else:
        assert phases == {"intent", "aborted"}
        assert "done" not in phases


def test_postcondition_check_catches_a_hypothetical_silent_noop_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K2a's contract, proven independently of K2b's specific root-cause fix: ANY mutation call
    that returns without raising, but leaves the original path genuinely untouched, must be
    caught by `apply_batch`'s own post-condition verification -- not merely relying on one fixed
    root cause never recurring. Monkeypatches `unlink_clear_readonly` (the direct_delete,
    single-file code path) to a no-op that raises nothing and touches nothing -- simulating
    exactly the failure SHAPE K2b's real bug exhibited (an operation reporting success while
    changing nothing) without depending on the junction reproduction at all."""
    import reclaim.executor as executor_module

    target = tmp_path / "orphaned_cache.bin"
    original_content = b"never actually deleted by the patched no-op"
    target.write_bytes(original_content)

    monkeypatch.setattr(executor_module, "unlink_clear_readonly", lambda path: None)

    candidate = _candidate(target, size_bytes=len(original_content), retention_days=None)
    report = apply_batch(
        [candidate],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
    )

    result = _apply_result_for(report.items, target)
    assert result.succeeded is False
    assert result.postcondition_verification_failed is True
    assert result.error is not None
    assert "silently did not remove it" in result.error
    assert target.exists()  # never actually touched -- the no-op is faithfully simulated
    assert target.read_bytes() == original_content

    raw_entries = read_manifest_entries(tmp_path / "manifest.jsonl")
    matching = [e for e in raw_entries if e.original_path == target]
    assert {e.phase for e in matching} == {"intent", "aborted"}  # never a "done" entry
