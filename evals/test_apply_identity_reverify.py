from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reclaim.config import Config
from reclaim.executor import ItemApplyResult, RestoreItemResult, apply_batch, restore_batch
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Tier, Verdict
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
