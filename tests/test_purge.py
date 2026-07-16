from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.config import Config, SafetyConfig
from reclaim.executor import (
    QuarantineManifestEntry,
    SafetyInvariantError,
    append_manifest_entries,
    fold_latest_manifest_entries,
)
from reclaim.models import Tier
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
    restored: bool = False,
    purged: bool = False,
    size_bytes: int = 100,
    is_dir: bool = False,
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
        category="old_installer",
        category_group="old_installers",
        rationale="test rationale",
        rebuild_instruction="Re-download from the original source if needed again.",
        tier=Tier.A,
        method="vault",
        vault_path=resolved_vault_path,
        retention_days=30,
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
