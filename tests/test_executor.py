from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reclaim.config import Config, SafetyConfig
from reclaim.executor import (
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    QuarantineManifestEntry,
    RecycleBinRestoreUnsupportedError,
    SafetyInvariantError,
    _latest_entries_for_batch,
    apply_batch,
    restore_batch,
)
from reclaim.models import Candidate, Tier, Verdict
from reclaim.safety import SafetyValidator

_NOW = 1_700_000_000.0


def _safety() -> SafetyValidator:
    """A `SafetyValidator` built from built-in defaults — every test in this file constructs
    `Candidate`s by hand with an already-decided `safety_verdict`, so the only thing this
    validator is actually exercised against is the ADR-0001 direct-delete pre-check's *fresh*
    re-evaluation, not the original candidate-generation gate."""
    return SafetyValidator(Config())


def _candidate(
    path: Path,
    *,
    is_dir: bool = False,
    size_bytes: int = 100,
    category: str = "test_category",
    category_group: str = "test_group",
    tier: Tier = Tier.A,
    safety_verdict: Verdict = Verdict.ELIGIBLE,
    retention_days: int | None = 30,
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
        safety_verdict=safety_verdict,
        safety_reason_code="TEST_REASON",
        retention_days=retention_days,
    )


# --- Dry-run: zero filesystem mutation --------------------------------------------------------


def test_dry_run_leaves_file_byte_unchanged_and_present(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    original_content = b"do-not-touch-me"
    target.write_bytes(original_content)
    original_mtime = target.stat().st_mtime

    manifest_path = tmp_path / "manifest.jsonl"
    report = apply_batch(
        [_candidate(target, size_bytes=len(original_content))],
        safety=_safety(),
        apply=False,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
    )

    assert report.apply is False
    assert target.exists()
    assert target.read_bytes() == original_content
    assert target.stat().st_mtime == original_mtime
    assert not manifest_path.exists()
    assert report.files_succeeded == 1
    assert report.files_failed == 0
    assert report.bytes_freed == len(original_content)
    assert report.disk_free_before_bytes is None
    assert report.disk_free_after_bytes is None
    assert report.disk_free_delta_bytes is None


def test_dry_run_calls_neither_shutil_move_nor_send2trash_nor_disk_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves "zero filesystem calls" for the dry-run path by making every mutating/measuring
    call raise if it is ever invoked, for both quarantine methods."""
    import reclaim.executor as executor_module

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must never call this")

    monkeypatch.setattr(executor_module.shutil, "move", _boom)
    monkeypatch.setattr(executor_module.shutil, "disk_usage", _boom)
    monkeypatch.setattr(executor_module.send2trash, "send2trash", _boom)

    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    for method in ("vault", "recycle_bin"):
        report = apply_batch(
            [_candidate(target)],
            safety=_safety(),
            apply=False,
            method=method,  # type: ignore[arg-type]
            vault_dir=tmp_path / "vault",
            manifest_path=manifest_path,
        )
        assert report.files_succeeded == 1

    assert not manifest_path.exists()
    assert target.exists()


# --- Vault method: real move + restore round-trip ----------------------------------------------


def test_vault_apply_moves_file_and_restore_round_trips_byte_identical(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.bin"
    target.parent.mkdir(parents=True)
    original_content = b"\x00\x01\xffreal-bytes-here" * 100
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

    assert apply_report.apply is True
    assert apply_report.files_succeeded == 1
    assert apply_report.files_failed == 0
    assert apply_report.bytes_freed == len(original_content)
    assert not target.exists()  # genuinely gone from its original location
    assert manifest_path.exists()

    vault_item = apply_report.items[0]
    assert vault_item.vault_path is not None
    assert vault_item.vault_path.exists()
    assert vault_item.vault_path.read_bytes() == original_content

    restore_report = restore_batch(
        apply_report.batch_id, manifest_path=manifest_path, now=_NOW + 10
    )
    assert restore_report.files_succeeded == 1
    assert restore_report.files_failed == 0
    assert restore_report.bytes_restored == len(original_content)
    assert target.exists()
    assert target.read_bytes() == original_content  # byte-identical, read from disk
    assert not vault_item.vault_path.exists()  # moved out of the vault, not copied


def test_vault_restore_is_idempotent_on_second_call(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    apply_report = apply_batch(
        [_candidate(target)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )
    restore_batch(apply_report.batch_id, manifest_path=manifest_path, now=_NOW + 1)

    second = restore_batch(apply_report.batch_id, manifest_path=manifest_path, now=_NOW + 2)
    assert second.files_succeeded == 1
    assert second.files_failed == 0
    assert second.items[0].already_restored is True
    assert second.bytes_restored == 0  # nothing actually moved on the idempotent replay
    assert target.exists()
    assert target.read_bytes() == b"content"


def test_vault_restore_refuses_to_overwrite_existing_destination(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"original")
    manifest_path = tmp_path / "manifest.jsonl"

    apply_report = apply_batch(
        [_candidate(target)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )
    # Something else now occupies the original path.
    target.write_bytes(b"unrelated-new-content")

    restore_report = restore_batch(apply_report.batch_id, manifest_path=manifest_path, now=_NOW)
    assert restore_report.files_failed == 1
    assert restore_report.files_succeeded == 0
    assert restore_report.items[0].error is not None
    assert "already exists" in restore_report.items[0].error
    assert target.read_bytes() == b"unrelated-new-content"  # never clobbered


# --- Recycle-bin method: send2trash called, restore refused ------------------------------------


def test_recycle_bin_apply_calls_send2trash_and_never_shutil_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.executor as executor_module

    calls: list[str] = []
    monkeypatch.setattr(executor_module.send2trash, "send2trash", lambda path: calls.append(path))
    monkeypatch.setattr(
        executor_module.shutil,
        "move",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("recycle_bin must not call move")),
    )

    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target)],
        safety=_safety(),
        apply=True,
        method="recycle_bin",
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert calls == [str(target)]
    assert report.files_succeeded == 1
    assert report.items[0].vault_path is None

    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert len(entries) == 1
    assert entries[0].method == "recycle_bin"
    assert entries[0].vault_path is None


def test_restore_refuses_recycle_bin_batch_with_documented_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.executor as executor_module

    monkeypatch.setattr(executor_module.send2trash, "send2trash", lambda path: None)

    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target)],
        safety=_safety(),
        apply=True,
        method="recycle_bin",
        manifest_path=manifest_path,
        now=_NOW,
    )

    with pytest.raises(RecycleBinRestoreUnsupportedError, match="Recycle Bin"):
        restore_batch(report.batch_id, manifest_path=manifest_path)


def test_restore_batch_not_found_raises() -> None:
    with pytest.raises(BatchNotFoundError):
        restore_batch("nonexistent-batch-id", manifest_path=Path("does_not_exist.jsonl"))


# --- Defense in depth: BLOCKED candidate ------------------------------------------------------


def test_apply_batch_raises_on_blocked_candidate_and_touches_nothing(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"
    blocked = _candidate(target, safety_verdict=Verdict.BLOCKED)

    with pytest.raises(SafetyInvariantError):
        apply_batch(
            [blocked],
            safety=_safety(),
            apply=True,
            vault_dir=tmp_path / "vault",
            manifest_path=manifest_path,
        )

    assert target.exists()
    assert target.read_bytes() == b"content"
    assert not manifest_path.exists()


def test_apply_batch_refuses_whole_batch_even_if_only_one_of_many_is_blocked(
    tmp_path: Path,
) -> None:
    ok_target = tmp_path / "ok.bin"
    ok_target.write_bytes(b"content")
    blocked_target = tmp_path / "blocked.bin"
    blocked_target.write_bytes(b"content")

    with pytest.raises(SafetyInvariantError):
        apply_batch(
            [_candidate(ok_target), _candidate(blocked_target, safety_verdict=Verdict.BLOCKED)],
            safety=_safety(),
            apply=True,
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
        )

    assert ok_target.exists()  # the whole batch was refused, not just the blocked item skipped
    assert blocked_target.exists()


# --- Partial-batch failure handling -------------------------------------------------------------


def test_partial_batch_failure_is_surfaced_and_does_not_abort_other_items(tmp_path: Path) -> None:
    missing = tmp_path / "already_gone.bin"  # never created on disk
    present = tmp_path / "present.bin"
    present.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(missing, size_bytes=50), _candidate(present, size_bytes=7)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_processed == 2
    assert report.files_succeeded == 1
    assert report.files_failed == 1
    assert report.bytes_freed == 7  # only the succeeded item's real size, not both

    failed_items = [item for item in report.items if not item.succeeded]
    succeeded_items = [item for item in report.items if item.succeeded]
    assert len(failed_items) == 1
    assert failed_items[0].path == missing
    assert failed_items[0].error is not None
    assert len(succeeded_items) == 1
    assert succeeded_items[0].path == present
    assert not present.exists()  # the succeeding item still actually moved


# --- Category breakdown / bytes_freed math ------------------------------------------------------


def test_category_breakdown_and_bytes_freed_only_count_succeeded_items(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"x" * 10)
    b = tmp_path / "b.bin"
    b.write_bytes(b"y" * 20)
    missing = tmp_path / "missing.bin"

    report = apply_batch(
        [
            _candidate(a, size_bytes=10, category="cache_a"),
            _candidate(b, size_bytes=20, category="cache_a"),
            _candidate(missing, size_bytes=999, category="cache_b"),
        ],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
    )

    assert report.bytes_freed == 30
    assert report.category_breakdown["cache_a"].count == 2
    assert report.category_breakdown["cache_a"].bytes_freed == 30
    assert "cache_b" not in report.category_breakdown  # the failed item's category never counted


# --- ADR-0001: direct-delete (retention_days=None) ----------------------------------------------


def test_direct_delete_apply_permanently_removes_file(tmp_path: Path) -> None:
    target = tmp_path / "cache" / "file.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"redownloadable-cache-content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target, size_bytes=29, retention_days=None)],
        safety=_safety(),
        apply=True,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert report.files_failed == 0
    assert not target.exists()  # genuinely, permanently gone — not moved anywhere
    assert report.items[0].method == "direct_delete"
    assert report.items[0].vault_path is None

    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert len(entries) == 1
    assert entries[0].method == "direct_delete"
    assert entries[0].vault_path is None
    assert entries[0].retention_days is None
    assert entries[0].retention_until is None


def test_direct_delete_apply_permanently_removes_directory(tmp_path: Path) -> None:
    target = tmp_path / "node_modules"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "index.js").write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target, is_dir=True, size_bytes=7, retention_days=None)],
        safety=_safety(),
        apply=True,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert not target.exists()


def test_direct_delete_dry_run_touches_nothing(tmp_path: Path) -> None:
    target = tmp_path / "cache" / "file.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target, retention_days=None)],
        safety=_safety(),
        apply=False,
        manifest_path=manifest_path,
    )

    assert report.files_succeeded == 1
    assert report.items[0].method == "direct_delete"
    assert target.exists()
    assert target.read_bytes() == b"content"
    assert not manifest_path.exists()


def test_apply_batch_rejects_explicit_direct_delete_method(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")

    with pytest.raises(ValueError, match="direct_delete"):
        apply_batch(
            [_candidate(target)],
            safety=_safety(),
            apply=False,
            method="direct_delete",  # type: ignore[arg-type]
        )


def test_mixed_batch_vault_and_direct_delete_processes_both(tmp_path: Path) -> None:
    vaulted = tmp_path / "vaulted.bin"
    vaulted.write_bytes(b"vault-me")
    deleted = tmp_path / "cache" / "deleted.bin"
    deleted.parent.mkdir(parents=True)
    deleted.write_bytes(b"delete-me-forever")
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    report = apply_batch(
        [_candidate(vaulted, retention_days=30), _candidate(deleted, retention_days=None)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 2
    by_path = {item.path: item for item in report.items}
    assert by_path[vaulted].method == "vault"
    assert by_path[vaulted].vault_path is not None
    assert by_path[vaulted].vault_path.exists()
    assert by_path[deleted].method == "direct_delete"
    assert by_path[deleted].vault_path is None
    assert not deleted.exists()
    assert not vaulted.exists()  # moved into the vault, not left in place


def test_restore_refuses_direct_delete_batch_with_distinct_message(tmp_path: Path) -> None:
    target = tmp_path / "cache" / "file.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target, retention_days=None)],
        safety=_safety(),
        apply=True,
        manifest_path=manifest_path,
        now=_NOW,
    )

    with pytest.raises(DirectDeleteRestoreImpossibleError, match="permanently-deleted"):
        restore_batch(report.batch_id, manifest_path=manifest_path)


def test_restore_refuses_mixed_batch_containing_direct_delete_entry(tmp_path: Path) -> None:
    vaulted = tmp_path / "vaulted.bin"
    vaulted.write_bytes(b"vault-me")
    deleted = tmp_path / "cache" / "deleted.bin"
    deleted.parent.mkdir(parents=True)
    deleted.write_bytes(b"delete-me-forever")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(vaulted, retention_days=30), _candidate(deleted, retention_days=None)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )

    with pytest.raises(DirectDeleteRestoreImpossibleError):
        restore_batch(report.batch_id, manifest_path=manifest_path)


# --- ADR-0001: mandatory pre-delete safety re-check ----------------------------------------------


def test_pre_delete_recheck_raises_when_fresh_verdict_blocked_and_file_survives(
    tmp_path: Path,
) -> None:
    """The adversarial case: a candidate that already carries a stale `safety_verdict=ELIGIBLE`
    (simulating a bug in candidate generation) but whose *fresh* re-evaluation against the live
    config comes back BLOCKED must abort the whole batch — deleting nothing."""
    protected_dir = tmp_path / "protected"
    protected_dir.mkdir()
    target = protected_dir / "secret.bin"
    original_content = b"do-not-delete-me"
    target.write_bytes(original_content)

    safety = SafetyValidator(Config(safety=SafetyConfig(deny=[f"{protected_dir.as_posix()}/*"])))
    stale_eligible_candidate = _candidate(
        target, retention_days=None, safety_verdict=Verdict.ELIGIBLE
    )

    with pytest.raises(SafetyInvariantError, match="pre-delete safety re-check"):
        apply_batch(
            [stale_eligible_candidate],
            safety=safety,
            apply=True,
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
        )

    assert target.exists()
    assert target.read_bytes() == original_content


def test_pre_delete_recheck_does_not_run_on_dry_run(tmp_path: Path) -> None:
    """The fresh re-check only gates real deletion (`apply=True`); a dry-run preview never
    aborts, even for a candidate that would fail the fresh check on a real apply."""
    protected_dir = tmp_path / "protected"
    protected_dir.mkdir()
    target = protected_dir / "secret.bin"
    target.write_bytes(b"content")

    safety = SafetyValidator(Config(safety=SafetyConfig(deny=[f"{protected_dir.as_posix()}/*"])))
    candidate = _candidate(target, retention_days=None, safety_verdict=Verdict.ELIGIBLE)

    report = apply_batch(
        [candidate], safety=safety, apply=False, manifest_path=tmp_path / "manifest.jsonl"
    )
    assert report.files_succeeded == 1
    assert target.exists()


def test_pre_delete_recheck_missing_path_does_not_abort_whole_batch(tmp_path: Path) -> None:
    """A direct-delete candidate whose file vanished between candidate generation and apply
    (an unrelated race, not a safety violation) must not abort the rest of the batch — the
    natural per-item failure in the second pass reports it instead."""
    missing = tmp_path / "cache" / "already_gone.bin"  # never created on disk
    present = tmp_path / "cache" / "present.bin"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [
            _candidate(missing, retention_days=None, size_bytes=1),
            _candidate(present, retention_days=None, size_bytes=7),
        ],
        safety=_safety(),
        apply=True,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_processed == 2
    assert report.files_succeeded == 1
    assert report.files_failed == 1
    assert not present.exists()


# --- ADR-0001: manifest completeness for direct-deleted items ------------------------------------


def test_manifest_direct_delete_entry_records_rebuild_instruction_and_no_vault_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cache" / "file.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    candidate = Candidate(
        path=target,
        is_dir=False,
        category="package_cache",
        category_group="package_caches",
        size_bytes=7,
        tier=Tier.A,
        rationale="Package/model download cache — redownloaded automatically.",
        rebuild_instruction="Re-run the package manager; the cache repopulates automatically.",
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="DEFAULT_ELIGIBLE",
        retention_days=None,
    )

    report = apply_batch(
        [candidate], safety=_safety(), apply=True, manifest_path=manifest_path, now=_NOW
    )
    assert report.files_succeeded == 1

    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.method == "direct_delete"
    assert entry.vault_path is None
    assert entry.retention_days is None
    assert entry.retention_until is None
    assert entry.category == "package_cache"
    assert entry.category_group == "package_caches"
    assert entry.rationale == candidate.rationale
    assert entry.rebuild_instruction == candidate.rebuild_instruction
    assert entry.is_dir is False


def test_manifest_purged_fields_round_trip_through_json() -> None:
    entry = QuarantineManifestEntry(
        batch_id="batch_test",
        original_path=Path("C:/Users/gg/Downloads/old_installer.exe"),
        size_bytes=1234,
        is_dir=False,
        category="old_installer",
        category_group="old_installers",
        rationale="test rationale",
        rebuild_instruction="Re-download from the original source if needed again.",
        tier=Tier.A,
        method="vault",
        vault_path=Path("data/quarantine/batch_test/abc_old_installer.exe"),
        retention_days=30,
        quarantined_at=_NOW,
        retention_until=_NOW + 30 * 86400.0,
        purged=True,
        purged_at=_NOW + 31 * 86400.0,
    )
    round_tripped = QuarantineManifestEntry.model_validate_json(entry.model_dump_json())
    assert round_tripped == entry
