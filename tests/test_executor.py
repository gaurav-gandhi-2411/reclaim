from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reclaim.executor import (
    BatchNotFoundError,
    RecycleBinRestoreUnsupportedError,
    SafetyInvariantError,
    _latest_entries_for_batch,
    apply_batch,
    restore_batch,
)
from reclaim.models import Candidate, Tier, Verdict

_NOW = 1_700_000_000.0


def _candidate(
    path: Path,
    *,
    is_dir: bool = False,
    size_bytes: int = 100,
    category: str = "test_category",
    category_group: str = "test_group",
    tier: Tier = Tier.A,
    safety_verdict: Verdict = Verdict.ELIGIBLE,
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
