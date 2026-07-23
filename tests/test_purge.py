from __future__ import annotations

import stat
from pathlib import Path

import pytest

from reclaim.config import Config, SafetyConfig
from reclaim.executor import (
    QuarantineManifestEntry,
    SafeModeViolationError,
    SafetyInvariantError,
    append_manifest_entries,
    fold_latest_manifest_entries,
)
from reclaim.models import Mode, Tier
from reclaim.purge import purge_expired
from reclaim.safety import SafetyValidator

_NOW = 1_700_000_000.0
_DAY = 86400.0


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


def _vault_entry(
    tmp_path: Path,
    *,
    original_path: Path | None = None,
    vault_path: Path | None = None,
    quarantined_at: float = _NOW - 40 * _DAY,
    retention_until: float | None = _NOW - 10 * _DAY,
    retention_days: int = 30,
    restored: bool = False,
    purged: bool = False,
    size_bytes: int = 100,
    is_dir: bool = False,
    category: str = "old_installer",
    category_group: str = "old_installers",
) -> QuarantineManifestEntry:
    resolved_vault_path = vault_path if vault_path is not None else tmp_path / "vault" / "item.bin"
    if not is_dir:
        resolved_vault_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_vault_path.write_bytes(b"x" * size_bytes)
    else:
        (resolved_vault_path / "inner").mkdir(parents=True, exist_ok=True)
        (resolved_vault_path / "inner" / "f.bin").write_bytes(b"x" * size_bytes)

    return QuarantineManifestEntry(
        batch_id="batch_test",
        original_path=original_path if original_path is not None else tmp_path / "gone.bin",
        size_bytes=size_bytes,
        is_dir=is_dir,
        category=category,
        category_group=category_group,
        rationale="test rationale",
        rebuild_instruction="Re-download from the original source if needed again.",
        tier=Tier.A,
        method="vault",
        vault_path=resolved_vault_path,
        retention_days=retention_days,
        quarantined_at=quarantined_at,
        retention_until=retention_until,
        restored=restored,
        purged=purged,
    )


# --- Dry-run / hard retention_until boundary ------------------------------------------------


def test_purge_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path)
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=False,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.apply is False
    assert report.files_succeeded == 1
    assert entry.vault_path is not None
    assert entry.vault_path.exists()  # dry-run touched nothing


def test_purge_apply_deletes_vault_copy_and_marks_purged(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path, size_bytes=42)
    append_manifest_entries(manifest_path, [entry])
    assert entry.vault_path is not None

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert report.bytes_freed == 42
    assert not entry.vault_path.exists()  # genuinely, permanently gone

    latest = {e.original_path: e for e in fold_latest_manifest_entries(manifest_path)}
    assert latest[entry.original_path].purged is True
    assert latest[entry.original_path].purged_at == _NOW


def test_purge_deletes_readonly_vault_file(tmp_path: Path) -> None:
    """ADR-0004 addendum (2026-07-17): a single vaulted read-only FILE (e.g. a lone git loose
    object, not inside a whole directory) must also purge — the non-directory branch needs the
    same chmod-before-unlink handling as the directory/rmtree branch, not just the directory
    case (`test_purge_deletes_vault_directory_containing_readonly_files`)."""
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path, size_bytes=42)
    assert entry.vault_path is not None
    entry.vault_path.chmod(stat.S_IREAD)
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert report.files_failed == 0
    assert not entry.vault_path.exists()


def test_purge_apply_deletes_vault_directory_via_rmtree(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path, is_dir=True, vault_path=tmp_path / "vault" / "dir_item")
    append_manifest_entries(manifest_path, [entry])
    assert entry.vault_path is not None

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert not entry.vault_path.exists()


def test_purge_deletes_vault_directory_containing_readonly_files(tmp_path: Path) -> None:
    """ADR-0004 addendum (2026-07-17): a vaulted directory containing read-only files (e.g. a
    `.git` directory — git marks packfiles/loose objects read-only by design) must actually
    purge, not fail with a permission error on the first read-only file it hits."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_path = tmp_path / "vault" / "dir_item"
    entry = _vault_entry(tmp_path, is_dir=True, vault_path=vault_path)
    readonly_file = vault_path / "inner" / "packed-object.pack"
    readonly_file.write_bytes(b"git-object-content")
    readonly_file.chmod(stat.S_IREAD)
    append_manifest_entries(manifest_path, [entry])
    assert entry.vault_path is not None

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert report.files_failed == 0
    assert not entry.vault_path.exists()


def test_purge_never_purges_entry_with_future_retention_until_even_with_apply(
    tmp_path: Path,
) -> None:
    """Hard boundary: a vault entry whose retention window has not yet passed must never be
    purged — `--apply` or not."""
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path, retention_until=_NOW + 10 * _DAY)
    append_manifest_entries(manifest_path, [entry])
    assert entry.vault_path is not None

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_processed == 0
    assert entry.vault_path.exists()  # untouched — the retention window hasn't passed


def test_purge_skips_restored_entries(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path, restored=True)
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )
    assert report.files_processed == 0


def test_purge_skips_already_purged_entries(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path, purged=True)
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )
    assert report.files_processed == 0


def test_purge_skips_recycle_bin_and_direct_delete_entries(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    recycle_entry = QuarantineManifestEntry(
        batch_id="batch_test",
        original_path=tmp_path / "trashed.bin",
        size_bytes=10,
        is_dir=False,
        category="old_installer",
        category_group="old_installers",
        rationale="test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="recycle_bin",
        vault_path=None,
        retention_days=30,
        quarantined_at=_NOW - 40 * _DAY,
        retention_until=_NOW - 10 * _DAY,
    )
    direct_delete_entry = QuarantineManifestEntry(
        batch_id="batch_test",
        original_path=tmp_path / "deleted.bin",
        size_bytes=10,
        is_dir=False,
        category="package_cache",
        category_group="package_caches",
        rationale="test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="direct_delete",
        vault_path=None,
        retention_days=None,
        quarantined_at=_NOW - 40 * _DAY,
        retention_until=None,
    )
    append_manifest_entries(manifest_path, [recycle_entry, direct_delete_entry])

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )
    assert report.files_processed == 0


# --- Stage 2 safety boundary: mode=Mode.SAFE forbids purge entirely, unconditionally -----------


def test_purge_expired_raises_safe_mode_violation_before_reading_manifest(tmp_path: Path) -> None:
    """purge_expired's own `if mode == Mode.SAFE` guard -- the "Stage 2 safety boundary" for a
    machine with real vaulted entries from an earlier power-mode session, later switched back to
    safe mode. Must refuse immediately, before any eligibility scan or I/O, even though the
    manifest here genuinely contains a real, purge-eligible vault entry."""
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path)
    append_manifest_entries(manifest_path, [entry])
    assert entry.vault_path is not None

    with pytest.raises(SafeModeViolationError):
        purge_expired(
            apply=True,
            manifest_path=manifest_path,
            vault_dir=tmp_path / "vault",
            safety=_safety(),
            now=_NOW,
            mode=Mode.SAFE,
        )

    assert entry.vault_path.exists()  # untouched -- refused before any filesystem mutation
    latest = fold_latest_manifest_entries(manifest_path)
    assert len(latest) == 1
    assert latest[0].purged is False  # no manifest entry appended for this refusal


def test_purge_expired_safe_mode_refusal_applies_to_dry_run_too(tmp_path: Path) -> None:
    """The safe-mode refusal is unconditional regardless of `apply` -- even a dry-run preview
    must not be allowed to report on vault entries while in safe mode."""
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(tmp_path)
    append_manifest_entries(manifest_path, [entry])

    with pytest.raises(SafeModeViolationError):
        purge_expired(
            apply=False,
            manifest_path=manifest_path,
            vault_dir=tmp_path / "vault",
            safety=_safety(),
            now=_NOW,
            mode=Mode.SAFE,
        )


def test_purge_expired_safe_mode_violation_never_reads_the_manifest_at_all(
    tmp_path: Path,
) -> None:
    """Refuses before ANY manifest I/O: a manifest_path that doesn't even exist on disk still
    raises SafeModeViolationError, never a FileNotFoundError from attempting to read it first."""
    nonexistent_manifest = tmp_path / "does_not_exist.jsonl"

    with pytest.raises(SafeModeViolationError):
        purge_expired(
            apply=True,
            manifest_path=nonexistent_manifest,
            vault_dir=tmp_path / "vault",
            safety=_safety(),
            now=_NOW,
            mode=Mode.SAFE,
        )


# --- Real-delete OSError per-item failure isolation ---------------------------------------------


def test_purge_apply_real_delete_oserror_isolates_one_failed_item_from_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `unlink_clear_readonly`/`shutil.rmtree` OSError path during a REAL purge (unlike
    `apply_batch`, which already has several monkeypatched-failure tests, this path had none)
    must isolate one item's failure -- the rest of the batch still purges successfully."""
    manifest_path = tmp_path / "manifest.jsonl"
    good_entry = _vault_entry(
        tmp_path,
        vault_path=tmp_path / "vault" / "good.bin",
        original_path=tmp_path / "good_gone.bin",
        size_bytes=10,
    )
    bad_entry = _vault_entry(
        tmp_path,
        vault_path=tmp_path / "vault" / "bad.bin",
        original_path=tmp_path / "bad_gone.bin",
        size_bytes=20,
    )
    append_manifest_entries(manifest_path, [good_entry, bad_entry])
    assert good_entry.vault_path is not None
    assert bad_entry.vault_path is not None

    import reclaim.purge as purge_module

    real_unlink = purge_module.unlink_clear_readonly

    def _flaky_unlink(path: str) -> None:
        if "bad.bin" in path:
            raise OSError("simulated: permission denied deleting this one vault item")
        real_unlink(path)

    monkeypatch.setattr(purge_module, "unlink_clear_readonly", _flaky_unlink)

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_processed == 2
    assert report.files_succeeded == 1
    assert report.files_failed == 1
    assert not good_entry.vault_path.exists()  # the succeeding item still actually purged
    assert bad_entry.vault_path.exists()  # untouched -- failure isolated to this one item

    failed_items = [item for item in report.items if not item.succeeded]
    succeeded_items = [item for item in report.items if item.succeeded]
    assert len(failed_items) == 1
    assert failed_items[0].original_path == bad_entry.original_path
    assert failed_items[0].error is not None
    assert len(succeeded_items) == 1
    assert succeeded_items[0].original_path == good_entry.original_path

    latest = {e.original_path: e for e in fold_latest_manifest_entries(manifest_path)}
    assert latest[good_entry.original_path].purged is True
    assert latest[bad_entry.original_path].purged is False  # never marked purged


def test_purge_apply_real_delete_oserror_on_directory_isolates_failed_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same batch-isolation guarantee for the directory (`shutil.rmtree`) branch, not just the
    single-file (`unlink_clear_readonly`) branch above."""
    manifest_path = tmp_path / "manifest.jsonl"
    good_entry = _vault_entry(
        tmp_path,
        is_dir=True,
        vault_path=tmp_path / "vault" / "good_dir",
        original_path=tmp_path / "good_gone",
        size_bytes=10,
    )
    bad_entry = _vault_entry(
        tmp_path,
        is_dir=True,
        vault_path=tmp_path / "vault" / "bad_dir",
        original_path=tmp_path / "bad_gone",
        size_bytes=20,
    )
    append_manifest_entries(manifest_path, [good_entry, bad_entry])

    import shutil as shutil_module

    import reclaim.purge as purge_module

    real_rmtree = shutil_module.rmtree

    def _flaky_rmtree(path: str, onexc: object = None) -> None:
        if "bad_dir" in path:
            raise OSError("simulated: permission denied deleting this one vault directory")
        real_rmtree(path, onexc=onexc)

    monkeypatch.setattr(purge_module.shutil, "rmtree", _flaky_rmtree)

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_processed == 2
    assert report.files_succeeded == 1
    assert report.files_failed == 1
    assert good_entry.vault_path is not None
    assert bad_entry.vault_path is not None
    assert not good_entry.vault_path.exists()
    assert bad_entry.vault_path.exists()  # untouched -- failure isolated to this one item


# --- ADR-0001: mandatory pre-purge safety re-check ---------------------------------------------


def test_purge_refuses_when_fresh_verdict_blocked_and_vault_copy_survives(tmp_path: Path) -> None:
    """A manifest entry whose `original_path` matches a protected pattern, with
    `retention_until` in the past, must not be purged — even though `original_path` itself no
    longer exists, the fresh re-check is run against the manifest's own recorded fields."""
    manifest_path = tmp_path / "manifest.jsonl"
    protected_original_path = tmp_path / "protected" / "secret.bin"
    entry = _vault_entry(tmp_path, original_path=protected_original_path)
    append_manifest_entries(manifest_path, [entry])
    assert entry.vault_path is not None

    safety = SafetyValidator(
        Config(safety=SafetyConfig(deny=[f"{(tmp_path / 'protected').as_posix()}/*"]))
    )

    with pytest.raises(SafetyInvariantError, match="pre-purge safety re-check"):
        purge_expired(
            apply=True,
            manifest_path=manifest_path,
            vault_dir=tmp_path / "vault",
            safety=safety,
            now=_NOW,
        )

    assert entry.vault_path.exists()  # nothing deleted — the whole run was refused


def test_purge_refuses_whole_run_even_in_dry_run_mode(tmp_path: Path) -> None:
    """The safety re-check runs unconditionally (dry-run reports, it never silently omits a
    blocked entry from the abort)."""
    manifest_path = tmp_path / "manifest.jsonl"
    protected_original_path = tmp_path / "protected" / "secret.bin"
    entry = _vault_entry(tmp_path, original_path=protected_original_path)
    append_manifest_entries(manifest_path, [entry])

    safety = SafetyValidator(
        Config(safety=SafetyConfig(deny=[f"{(tmp_path / 'protected').as_posix()}/*"]))
    )

    with pytest.raises(SafetyInvariantError):
        purge_expired(
            apply=False,
            manifest_path=manifest_path,
            vault_dir=tmp_path / "vault",
            safety=safety,
            now=_NOW,
        )


# --- ADR-0005: rebuildable retention_days=0 / stale-original-reoccupied -----------------------


def test_rebuildable_vault_entry_with_zero_retention_is_immediately_purge_eligible(
    tmp_path: Path,
) -> None:
    """A rebuildable-category vault entry quarantined with `retention_days=0` (the
    guard-downgrade override — see test_executor.py's
    `test_rebuildable_guard_downgraded_candidate_gets_zero_retention`) is purge-eligible the
    instant it's quarantined, not after any waiting period."""
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(
        tmp_path,
        category="windows_temp",
        category_group="temp_and_browser_caches",
        retention_days=0,
        quarantined_at=_NOW,
        retention_until=_NOW,  # 0-day window from quarantine time -> already due
        size_bytes=64,
    )
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=False,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,  # same instant as quarantine — no waiting period at all
    )

    assert report.files_processed == 1
    assert report.files_succeeded == 1
    assert report.items[0].stale is False  # eligible via retention, not the stale path


def test_stale_vault_entry_with_reoccupied_original_path_is_flagged_and_purge_eligible(
    tmp_path: Path,
) -> None:
    """ADR-0005: a vault entry whose `original_path` is now occupied again (the real uv/cache
    case — the tool regenerated its own cache at the original location) is purge-eligible
    immediately, regardless of `retention_until` still being far in the future, and is flagged
    `stale=True` so the report never conflates it with a genuinely-expired entry."""
    manifest_path = tmp_path / "manifest.jsonl"
    original_path = tmp_path / "uv_cache_original"
    original_path.mkdir()
    (original_path / "regenerated.bin").write_bytes(b"freshly rebuilt content")

    entry = _vault_entry(
        tmp_path,
        original_path=original_path,
        category="package_cache",
        category_group="package_caches",
        is_dir=True,
        retention_days=30,
        quarantined_at=_NOW - 1 * _DAY,
        retention_until=_NOW + 29 * _DAY,  # nowhere near expired
    )
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=False,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_processed == 1
    assert report.files_succeeded == 1
    assert report.items[0].stale is True
    assert report.stale_count == 1
    assert report.stale_bytes == entry.size_bytes
    assert original_path.exists()  # purge (dry-run) never touches original_path itself


def test_model_cache_vault_entry_not_purge_eligible_before_30_days(tmp_path: Path) -> None:
    """A model_caches vault entry (30-day retention, ADR-0003) with its original path still
    genuinely gone (not stale) and retention_until still in the future must NOT be
    purge-eligible — the rebuildable/stale fast paths never apply to non-rebuildable
    categories with a normal, un-reoccupied original path."""
    manifest_path = tmp_path / "manifest.jsonl"
    entry = _vault_entry(
        tmp_path,
        category="model_cache",
        category_group="model_caches",
        retention_days=30,
        quarantined_at=_NOW - 5 * _DAY,
        retention_until=_NOW + 25 * _DAY,
    )
    append_manifest_entries(manifest_path, [entry])

    report = purge_expired(
        apply=False,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
    )

    assert report.files_processed == 0
    assert entry.vault_path is not None
    assert entry.vault_path.exists()  # untouched


def test_purge_rebuildable_only_skips_non_rebuildable_eligible_entries(tmp_path: Path) -> None:
    """`only_rebuildable=True` scopes purge to REBUILDABLE_CATEGORY_GROUPS even when a
    non-rebuildable entry is ALSO genuinely purge-eligible — a model_caches/duplicates vault
    entry is never touched by a rebuildable-scoped purge run."""
    manifest_path = tmp_path / "manifest.jsonl"
    rebuildable_entry = _vault_entry(
        tmp_path,
        vault_path=tmp_path / "vault" / "rebuildable_item.bin",
        category="windows_temp",
        category_group="temp_and_browser_caches",
    )
    non_rebuildable_entry = _vault_entry(
        tmp_path,
        vault_path=tmp_path / "vault" / "model_item.bin",
        original_path=tmp_path / "model_gone.bin",
        category="model_cache",
        category_group="model_caches",
    )
    append_manifest_entries(manifest_path, [rebuildable_entry, non_rebuildable_entry])

    report = purge_expired(
        apply=True,
        manifest_path=manifest_path,
        vault_dir=tmp_path / "vault",
        safety=_safety(),
        now=_NOW,
        only_rebuildable=True,
    )

    assert report.files_processed == 1
    assert report.files_succeeded == 1
    assert not rebuildable_entry.vault_path.exists()  # type: ignore[union-attr]
    assert non_rebuildable_entry.vault_path.exists()  # type: ignore[union-attr]
