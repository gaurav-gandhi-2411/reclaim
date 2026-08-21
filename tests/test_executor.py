from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import reclaim.executor as executor_module
from reclaim.config import Config, SafetyConfig
from reclaim.executor import (
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    ManifestLockTimeoutError,
    QuarantineManifestEntry,
    RecycleBinRestoreUnsupportedError,
    RestoreIntegrityError,
    SafeModeViolationError,
    SafetyInvariantError,
    VaultIntegrityError,
    _append_and_sync,
    _close_manifest_for_sync,
    _latest_entries_for_batch,
    _open_manifest_for_sync,
    apply_batch,
    long_path,
    restore_batch,
)
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Mode, Tier, Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

_NOW = 1_700_000_000.0


def _make_deep_tree(root: Path, *, depth: int = 15, segment_len: int = 20) -> Path:
    r"""Builds a directory tree whose full path comfortably exceeds Windows' 260-char MAX_PATH,
    to exercise `\\?\`-prefixed long-path handling (ADR-0004). Uses `os.makedirs` on a raw
    `\\?\`-prefixed string rather than `Path.mkdir` — `pathlib.Path` doesn't reliably round-trip
    that prefix, same reasoning as `reclaim.executor`'s own long-path helpers."""
    current = root
    for i in range(depth):
        current = current / (f"seg_{i:03d}_" + "x" * segment_len)
        os.makedirs(long_path(current), exist_ok=True)  # noqa: PTH103
    assert len(str(current)) > 260, f"fixture path too short: {len(str(current))} chars"
    return current


def _long_read_bytes(path: Path) -> bytes:
    r"""Reads a file via its `\\?\`-prefixed path — the test's own read must be long-path-safe
    too, independent of whether the production code under test got it right."""
    with open(long_path(path), "rb") as fh:  # noqa: PTH123
        return fh.read()


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
    size_guard_exempt: bool = False,
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
        safety_verdict=safety_verdict,
        safety_reason_code="TEST_REASON",
        retention_days=retention_days,
        size_guard_exempt=size_guard_exempt,
        rebuildable=rebuildable,
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
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 10,
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
    vault_dir = tmp_path / "vault"

    apply_report = apply_batch(
        [_candidate(target)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 1,
    )

    second = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 2,
    )
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
    vault_dir = tmp_path / "vault"

    apply_report = apply_batch(
        [_candidate(target)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    # Something else now occupies the original path.
    target.write_bytes(b"unrelated-new-content")

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW,
    )
    assert restore_report.files_failed == 1
    assert restore_report.files_succeeded == 0
    assert restore_report.items[0].error is not None
    assert "already exists" in restore_report.items[0].error
    assert target.read_bytes() == b"unrelated-new-content"  # never clobbered


# --- restore_batch per-item (OSError, VaultIntegrityError) failure isolation --------------------


def test_restore_batch_isolates_one_failed_item_from_the_rest_of_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The try/except around `_atomic_move` inside `restore_batch`'s vault_entries loop: with
    2+ restorable vault entries in one batch, a failure targeting one specific entry (by path)
    must not abort restoring the others."""
    good_target = tmp_path / "good.bin"
    good_target.write_bytes(b"good-content")
    bad_target = tmp_path / "bad.bin"
    bad_target.write_bytes(b"bad-content")
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    apply_report = apply_batch(
        [_candidate(good_target, retention_days=30), _candidate(bad_target, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    assert apply_report.files_succeeded == 2
    assert not good_target.exists()
    assert not bad_target.exists()

    import reclaim.executor as executor_module

    real_atomic_move = executor_module._atomic_move

    def _flaky_atomic_move(src: Path, dst: Path, *, is_dir: bool) -> None:
        if dst == bad_target:
            raise OSError("simulated: permission denied restoring this one entry")
        real_atomic_move(src, dst, is_dir=is_dir)

    monkeypatch.setattr(executor_module, "_atomic_move", _flaky_atomic_move)

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 1,
    )

    assert restore_report.files_processed == 2
    assert restore_report.files_succeeded == 1
    assert restore_report.files_failed == 1
    assert good_target.exists()
    assert good_target.read_bytes() == b"good-content"
    assert not bad_target.exists()  # the failed restore never moved anything back

    by_path = {item.original_path: item for item in restore_report.items}
    assert by_path[good_target].succeeded is True
    assert by_path[bad_target].succeeded is False
    assert by_path[bad_target].error is not None

    latest = {
        e.original_path: e for e in _latest_entries_for_batch(manifest_path, apply_report.batch_id)
    }
    assert latest[good_target].restored is True
    assert latest[bad_target].restored is False  # never marked restored


def test_restore_batch_isolated_failure_survives_vault_integrity_error_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same isolation guarantee for the other exception type this except clause catches --
    `VaultIntegrityError`, not just plain `OSError`."""
    from reclaim.executor import VaultIntegrityError

    good_target = tmp_path / "good.bin"
    good_target.write_bytes(b"good-content")
    bad_target = tmp_path / "bad.bin"
    bad_target.write_bytes(b"bad-content")
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    apply_report = apply_batch(
        [_candidate(good_target, retention_days=30), _candidate(bad_target, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    import reclaim.executor as executor_module

    real_atomic_move = executor_module._atomic_move

    def _flaky_atomic_move(src: Path, dst: Path, *, is_dir: bool) -> None:
        if dst == bad_target:
            raise VaultIntegrityError("simulated: parity mismatch restoring this one entry")
        real_atomic_move(src, dst, is_dir=is_dir)

    monkeypatch.setattr(executor_module, "_atomic_move", _flaky_atomic_move)

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 1,
    )

    assert restore_report.files_succeeded == 1
    assert restore_report.files_failed == 1
    assert good_target.exists()
    assert not bad_target.exists()


# --- ADR-0004: long-path-safe, atomic-or-nothing vault/restore moves ---------------------------


def test_vault_move_and_restore_survive_path_past_max_path(tmp_path: Path) -> None:
    """The real-disk regression this ADR responds to: a directory tree deep enough that its
    full path exceeds Windows' 260-char MAX_PATH must vault-move AND restore successfully, with
    the payload byte-identical on both ends of the round trip — not just short paths, which the
    pre-ADR-0004 throwaway-file test only ever proved."""
    top = tmp_path / "deep_root"
    top.mkdir()
    leaf = _make_deep_tree(top)
    content = b"deep-path-payload-content-" * 200
    payload_rel = Path("payload.bin")
    with open(long_path(leaf / payload_rel), "wb") as fh:  # noqa: PTH123 -- \\?\ str, not Path
        fh.write(content)

    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    apply_report = apply_batch(
        [_candidate(top, is_dir=True, size_bytes=len(content), retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert apply_report.files_succeeded == 1, apply_report.items
    # The per-line ignores below are all `\?\`-str paths, not Path -- see module docstring above.
    assert not os.path.exists(long_path(top))  # noqa: PTH110 -- source fully gone

    entries = _latest_entries_for_batch(manifest_path, apply_report.batch_id)
    vault_path = entries[0].vault_path
    assert vault_path is not None
    rel_from_top = leaf.relative_to(top) / payload_rel
    vaulted_payload = vault_path / rel_from_top
    assert _long_read_bytes(vaulted_payload) == content

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 1,
    )
    assert restore_report.files_succeeded == 1, restore_report.items
    assert os.path.exists(long_path(top))  # noqa: PTH110
    restored_payload = top / rel_from_top
    assert _long_read_bytes(restored_payload) == content
    assert not os.path.exists(long_path(vault_path))  # noqa: PTH110 -- moved out, not copied


def test_vault_move_cleans_up_partial_copy_on_injected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the exact real-disk failure mode: `os.rename` can't be used (forced here to
    exercise the fallback deterministically, rather than depending on a real cross-volume setup)
    and the subsequent `shutil.copytree` fails partway through. Proves the atomic-or-nothing
    guarantee: the source is left completely untouched, the vault gets zero orphaned bytes for
    this item, and the item is recorded as failed rather than silently losing data or leaving
    debris behind for a human to find and clean up by hand."""
    src = tmp_path / "source_dir"
    src.mkdir()
    (src / "file_a.bin").write_bytes(b"a" * 100)
    (src / "file_b.bin").write_bytes(b"b" * 100)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copytree fallback path")

    def _fake_copytree(_src_path: str, dst_path: str, **_kwargs: object) -> str:
        Path(dst_path).mkdir(parents=True, exist_ok=True)
        (Path(dst_path) / "file_a.bin").write_bytes(b"a" * 100)
        raise OSError("simulated: copytree fails partway through, file_b never copied")

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)
    monkeypatch.setattr(executor_module.shutil, "copytree", _fake_copytree)

    report = apply_batch(
        [_candidate(src, is_dir=True, size_bytes=200, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 0
    assert report.files_failed == 1
    assert src.exists()  # source completely untouched
    assert (src / "file_a.bin").read_bytes() == b"a" * 100
    assert (src / "file_b.bin").read_bytes() == b"b" * 100
    leftover = list(vault_dir.rglob("*")) if vault_dir.exists() else []
    assert leftover == [], f"orphaned vault debris: {leftover}"
    assert _latest_entries_for_batch(manifest_path, report.batch_id) == []  # never claimed done


def test_vault_move_detects_and_cleans_up_incomplete_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copytree that raises no exception but silently produces an incomplete copy (e.g. an
    interrupted process that leaves no error, just missing bytes) must still be caught by the
    file-count/total-bytes parity check, not accepted as a successful vault entry."""
    src = tmp_path / "source_dir"
    src.mkdir()
    (src / "file_a.bin").write_bytes(b"a" * 100)
    (src / "file_b.bin").write_bytes(b"b" * 100)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copytree fallback path")

    def _fake_copytree(_src_path: str, dst_path: str, **_kwargs: object) -> str:
        Path(dst_path).mkdir(parents=True, exist_ok=True)
        (Path(dst_path) / "file_a.bin").write_bytes(b"a" * 100)
        return dst_path  # returns normally — file_b silently missing, no exception raised

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)
    monkeypatch.setattr(executor_module.shutil, "copytree", _fake_copytree)

    report = apply_batch(
        [_candidate(src, is_dir=True, size_bytes=200, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 0
    assert report.files_failed == 1
    assert "parity mismatch" in (report.items[0].error or "")
    assert src.exists()
    assert (src / "file_a.bin").read_bytes() == b"a" * 100
    assert (src / "file_b.bin").read_bytes() == b"b" * 100
    leftover = list(vault_dir.rglob("*")) if vault_dir.exists() else []
    assert leftover == [], f"orphaned vault debris: {leftover}"
    assert _latest_entries_for_batch(manifest_path, report.batch_id) == []


def test_vault_move_succeeds_when_source_contains_readonly_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real-disk regression (2026-07-17): a vaulted directory containing a `.git` directory
    (packfiles/loose objects are read-only by git's own design) must vault successfully — the
    fallback copy-then-remove-source path must be able to remove read-only files from the
    source, not fail or silently leave them behind. Forces the copytree fallback (rather than
    the atomic os.rename fast path) so the read-only removal logic is actually exercised; the
    copy itself is real, not mocked, so the read-only attribute genuinely propagates to the
    destination too."""
    src = tmp_path / "source_dir"
    src.mkdir()
    readonly_file = src / "packed-object.pack"
    readonly_file.write_bytes(b"git-object-content")
    readonly_file.chmod(stat.S_IREAD)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copytree fallback path")

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)

    report = apply_batch(
        [_candidate(src, is_dir=True, size_bytes=18, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 1, report.items
    assert not src.exists()  # source (including the read-only file) fully removed
    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    vault_path = entries[0].vault_path
    assert vault_path is not None
    assert (vault_path / "packed-object.pack").read_bytes() == b"git-object-content"


def test_vault_move_cleanup_removes_readonly_files_from_partial_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real-disk regression (2026-07-17): the first version of this fix used
    `shutil.rmtree(..., ignore_errors=True)` for cleanup, which silently left read-only
    git-object files behind as orphaned vault debris after a parity-mismatch failure — exactly
    the failure mode ADR-0004 exists to prevent. Cleanup must actually remove read-only files,
    not swallow the permission error and give up partway."""
    src = tmp_path / "source_dir"
    src.mkdir()
    (src / "file_a.bin").write_bytes(b"a" * 100)
    (src / "file_b.bin").write_bytes(b"b" * 100)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copytree fallback path")

    def _fake_copytree(_src_path: str, dst_path: str, **_kwargs: object) -> str:
        Path(dst_path).mkdir(parents=True, exist_ok=True)
        readonly_copy = Path(dst_path) / "file_a.bin"
        readonly_copy.write_bytes(b"a" * 100)
        readonly_copy.chmod(stat.S_IREAD)  # mirrors a real read-only git-object copy
        return dst_path  # file_b never copied -> parity check below must catch this

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)
    monkeypatch.setattr(executor_module.shutil, "copytree", _fake_copytree)

    report = apply_batch(
        [_candidate(src, is_dir=True, size_bytes=200, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 0
    assert report.files_failed == 1
    assert "parity mismatch" in (report.items[0].error or "")
    leftover = list(vault_dir.rglob("*")) if vault_dir.exists() else []
    assert leftover == [], f"orphaned read-only vault debris: {leftover}"


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
        restore_batch(report.batch_id, manifest_path=manifest_path, safety=_safety())


def test_restore_batch_not_found_raises() -> None:
    with pytest.raises(BatchNotFoundError):
        restore_batch(
            "nonexistent-batch-id",
            manifest_path=Path("does_not_exist.jsonl"),
            safety=_safety(),
        )


# --- Stage 2 safety boundary: apply_batch's own mode=Mode.SAFE guard ---------------------------


def test_apply_batch_raises_safe_mode_violation_for_vault_method_bypassing_caller_resolution(
    tmp_path: Path,
) -> None:
    """apply_batch's own last-line-of-defense guard: both real callers (CLI, API) already
    pre-resolve method to "recycle_bin" before calling apply_batch, so this raise path is never
    exercised in practice -- call directly with method="vault" while mode=Mode.SAFE (bypassing
    that pre-resolution) and confirm it refuses the entire batch before any filesystem
    mutation."""
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    with pytest.raises(SafeModeViolationError):
        apply_batch(
            [_candidate(target)],
            safety=_safety(),
            apply=True,
            method="vault",
            mode=Mode.SAFE,
            vault_dir=tmp_path / "vault",
            manifest_path=manifest_path,
            now=_NOW,
        )

    assert target.exists()  # untouched
    assert target.read_bytes() == b"content"
    assert not manifest_path.exists()  # nothing written


def test_apply_batch_safe_mode_with_recycle_bin_method_succeeds_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one combination safe mode allows: method="recycle_bin" with mode=Mode.SAFE applies
    normally, exercising send2trash rather than being refused."""
    import reclaim.executor as executor_module

    calls: list[str] = []
    monkeypatch.setattr(executor_module.send2trash, "send2trash", lambda path: calls.append(path))

    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target)],
        safety=_safety(),
        apply=True,
        method="recycle_bin",
        mode=Mode.SAFE,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert calls == [str(target)]
    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert entries[0].method == "recycle_bin"


# --- ADR-0004: single-file `shutil.copy2` fallback + cleanup-on-failure -------------------------


def test_single_file_copy2_fallback_succeeds_when_rename_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-FILE `shutil.copy2` fallback's happy path (unlike the directory/copytree
    fallback, which already has a real-content long-path success test): forcing `os.rename` to
    fail routes into `copy2`, which succeeds cleanly -- source removed, destination byte-
    identical, restore round-trips."""
    src = tmp_path / "source_file.bin"
    original_content = b"cross-volume-fallback-content" * 20
    src.write_bytes(original_content)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copy2 fallback path")

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)

    report = apply_batch(
        [_candidate(src, size_bytes=len(original_content), retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert not src.exists()  # source removed by the copy-then-unlink fallback
    vault_item = report.items[0]
    assert vault_item.vault_path is not None
    assert vault_item.vault_path.read_bytes() == original_content

    restore_report = restore_batch(
        report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 1,
    )
    assert restore_report.files_succeeded == 1
    assert src.exists()
    assert src.read_bytes() == original_content


def test_single_file_copy2_fallback_propagates_error_and_cleans_up_partial_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-FILE (not directory) cross-volume fallback in `_atomic_move`: force
    `os.rename` to fail (routing into the `shutil.copy2` branch), then force `shutil.copy2`
    itself to fail partway. Proves the atomic-or-nothing guarantee for the file branch, mirroring
    the directory/copytree tests above: the exception propagates, the partial destination copy
    is cleaned up, and the source is left completely untouched."""
    src = tmp_path / "source_file.bin"
    original_content = b"do-not-touch-source" * 50
    src.write_bytes(original_content)
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copy2 fallback path")

    def _fake_copy2(_src_path: str, dst_path: str, **_kwargs: object) -> str:
        Path(dst_path).write_bytes(b"partial-garbage")  # simulate a failed/interrupted copy
        raise OSError("simulated: copy2 fails partway through")

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)
    monkeypatch.setattr(executor_module.shutil, "copy2", _fake_copy2)

    report = apply_batch(
        [_candidate(src, size_bytes=len(original_content), retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 0
    assert report.files_failed == 1
    assert src.exists()  # source completely untouched
    assert src.read_bytes() == original_content
    leftover = list(vault_dir.rglob("*")) if vault_dir.exists() else []
    assert leftover == [], f"orphaned partial-copy debris: {leftover}"
    assert _latest_entries_for_batch(manifest_path, report.batch_id) == []  # never claimed done


def test_single_file_copy2_fallback_size_mismatch_raises_integrity_error_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `copy2` that raises no exception but silently produces a size-mismatched destination
    (simulated via a monkeypatched `os.path.getsize` on the destination) must still be caught by
    the size-parity check -- `VaultIntegrityError`, source untouched, partial copy cleaned up."""
    src = tmp_path / "source_file.bin"
    src.write_bytes(b"real-content-here")
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copy2 fallback path")

    real_getsize = executor_module.os.path.getsize

    def _fake_getsize(path: str) -> int:
        size = real_getsize(path)
        # Only lie about the destination's size (inside vault_dir); the source's real pre_size
        # measurement must stay truthful, or the mismatch this test wants would never trigger.
        if str(vault_dir) in path:
            return size + 1
        return size

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)
    monkeypatch.setattr(executor_module.os.path, "getsize", _fake_getsize)

    report = apply_batch(
        [_candidate(src, size_bytes=17, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 0
    assert report.files_failed == 1
    assert "mismatch" in (report.items[0].error or "")
    assert src.exists()
    assert src.read_bytes() == b"real-content-here"
    leftover = list(vault_dir.rglob("*")) if vault_dir.exists() else []
    assert leftover == [], f"orphaned partial-copy debris: {leftover}"


def test_atomic_move_single_file_copy2_fallback_cleanup_failure_is_only_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check that VaultIntegrityError itself (not the ADR-0004 directory variant) is the
    concrete exception type raised for the single-file size-mismatch case, by calling
    `_atomic_move` directly rather than through `apply_batch`'s broad `except Exception`."""
    from reclaim.executor import _atomic_move

    src = tmp_path / "source_file.bin"
    src.write_bytes(b"content")
    dst = tmp_path / "vault" / "dest_file.bin"

    import reclaim.executor as executor_module

    def _fake_rename(_src: str, _dst: str) -> None:
        raise OSError("simulated: force the copy2 fallback path")

    real_getsize = executor_module.os.path.getsize

    def _fake_getsize(path: str) -> int:
        if "dest_file" in path:
            return real_getsize(path) + 1
        return real_getsize(path)

    monkeypatch.setattr(executor_module.os, "rename", _fake_rename)
    monkeypatch.setattr(executor_module.os.path, "getsize", _fake_getsize)

    with pytest.raises(VaultIntegrityError, match="mismatch"):
        _atomic_move(src, dst, is_dir=False)

    assert src.exists()
    assert not dst.exists()


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


def test_direct_delete_removes_readonly_file(tmp_path: Path) -> None:
    """ADR-0004 addendum (2026-07-17): the direct_delete path's single-file branch must clear
    the read-only attribute before unlink, same as the directory/rmtree branch — a lone
    read-only file (a git loose object sitting directly in a candidate directory, for example)
    must not silently fail to delete."""
    target = tmp_path / "cache" / "readonly_file.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"readonly-content")
    target.chmod(stat.S_IREAD)
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target, size_bytes=17, retention_days=None)],
        safety=_safety(),
        apply=True,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert report.files_failed == 0
    assert not target.exists()


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


# --- ADR-0003: cost-aware size guard downgrades oversized direct-delete candidates ------------


def test_oversized_direct_delete_candidate_downgrades_to_vault(tmp_path: Path) -> None:
    """Core ADR-0003 invariant: a `retention_days=None` candidate at/above the size guard is
    forced to `vault`, never `direct_delete`, regardless of its category — recovery cost, not
    category, decides permanence."""
    target = tmp_path / "cache" / "huge_model.safetensors"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"stand-in content")  # actual bytes on disk are irrelevant to the guard
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    report = apply_batch(
        [_candidate(target, size_bytes=2 * 1024 * 1024 * 1024, retention_days=None)],
        safety=_safety(),
        apply=True,
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.files_succeeded == 1
    assert report.items[0].method == "vault"
    assert report.items[0].vault_path is not None
    assert report.items[0].vault_path.exists()
    assert not target.exists()  # moved into the vault, not left in place

    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert entries[0].method == "vault"
    assert entries[0].retention_days == 30  # default direct_delete_size_guard_retention_days
    assert entries[0].retention_until is not None


def test_oversized_direct_delete_candidate_stays_restorable(tmp_path: Path) -> None:
    """The guard-downgraded item is a normal vaulted entry as far as `restore_batch` is
    concerned — restorability is decided by `entry.method`, not `entry.retention_days`."""
    target = tmp_path / "huge_model.safetensors"
    target.write_bytes(b"content")
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    report = apply_batch(
        [_candidate(target, size_bytes=2 * 1024 * 1024 * 1024, retention_days=None)],
        safety=_safety(),
        apply=True,
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    restore_report = restore_batch(
        report.batch_id, manifest_path=manifest_path, vault_dir=vault_dir, safety=_safety()
    )
    assert restore_report.files_succeeded == 1
    assert target.exists()


def test_direct_delete_size_guard_respects_configured_threshold(tmp_path: Path) -> None:
    """A custom, smaller guard threshold triggers on a candidate well below the 1GB default —
    proves the threshold is actually threaded through, not hardcoded."""
    target = tmp_path / "medium.bin"
    target.write_bytes(b"x")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target, size_bytes=500, retention_days=None)],
        safety=_safety(),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
        direct_delete_size_guard_bytes=100,
        direct_delete_size_guard_retention_days=7,
    )

    assert report.items[0].method == "vault"
    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert entries[0].retention_days == 7


def test_direct_delete_size_guard_does_not_trigger_below_threshold(tmp_path: Path) -> None:
    target = tmp_path / "small.bin"
    target.write_bytes(b"x")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(target, size_bytes=50, retention_days=None)],
        safety=_safety(),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
        direct_delete_size_guard_bytes=100,
    )

    assert report.items[0].method == "direct_delete"
    assert not target.exists()


def _flat_tree(root: Path, *, file_count: int) -> Path:
    cache_dir = root / "cache_dir"
    cache_dir.mkdir()
    for i in range(file_count):
        (cache_dir / f"file_{i}.bin").write_bytes(b"x")
    return cache_dir


def test_direct_delete_entry_count_guard_respects_configured_threshold(tmp_path: Path) -> None:
    """ADR-0032 (P0-K1a/M1 cost-budget follow-up): a directory candidate whose scan-recorded
    subtree entry count is at or above a configured (small, for test speed) threshold is
    force-downgraded to vault, the same way the byte-size guard already is — proves the
    threshold is actually threaded through `apply_batch`, not hardcoded."""
    cache_dir = _flat_tree(tmp_path, file_count=5)
    manifest_path = tmp_path / "manifest.jsonl"

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        report = apply_batch(
            [_candidate(cache_dir, is_dir=True, size_bytes=5, retention_days=None)],
            safety=_safety(),
            apply=True,
            vault_dir=tmp_path / "vault",
            manifest_path=manifest_path,
            now=_NOW,
            scan_index=index,
            direct_delete_entry_count_guard=2,  # real tree has 5 files, well above this
        )

    assert report.items[0].method == "vault"
    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert entries[0].method == "vault"


def test_direct_delete_entry_count_guard_does_not_trigger_below_threshold(tmp_path: Path) -> None:
    cache_dir = _flat_tree(tmp_path, file_count=3)
    manifest_path = tmp_path / "manifest.jsonl"

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        report = apply_batch(
            [_candidate(cache_dir, is_dir=True, size_bytes=3, retention_days=None)],
            safety=_safety(),
            apply=True,
            vault_dir=tmp_path / "vault",
            manifest_path=manifest_path,
            now=_NOW,
            scan_index=index,
            direct_delete_entry_count_guard=1000,  # real tree has 3 files, well below this
        )

    assert report.items[0].method == "direct_delete"
    assert not cache_dir.exists()


def test_size_guard_exempt_candidate_still_subject_to_entry_count_guard(tmp_path: Path) -> None:
    """The key motivating interaction (ADR-0032): `size_guard_exempt=True` (package_caches'
    real default) only ever meant "this category's RECOVERY cost doesn't scale with size" — it
    was never a statement about RE-WALK cost, which is what the entry-count guard protects
    against. A real `%LOCALAPPDATA%\\npm-cache`-shaped candidate (package_caches, exempt from
    the byte-size guard, but with a large real file count) must still be caught by the
    entry-count axis."""
    cache_dir = _flat_tree(tmp_path, file_count=5)
    manifest_path = tmp_path / "manifest.jsonl"

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        report = apply_batch(
            [
                _candidate(
                    cache_dir,
                    is_dir=True,
                    size_bytes=5,
                    category="package_cache",
                    retention_days=None,
                    size_guard_exempt=True,
                )
            ],
            safety=_safety(),
            apply=True,
            vault_dir=tmp_path / "vault",
            manifest_path=manifest_path,
            now=_NOW,
            scan_index=index,
            direct_delete_size_guard_bytes=1,  # would also fire on bytes if not exempt
            direct_delete_entry_count_guard=2,
        )

    # size_guard_exempt=True means the BYTE axis never fires (proven by the sibling test above),
    # but the entry-count axis is independent of that flag and fires anyway.
    assert report.items[0].method == "vault"


def test_entry_count_guard_never_fires_without_scan_index(tmp_path: Path) -> None:
    """Disclosed fallback (mirrors M1's own `scan_index is None` posture): with no `scan_index`
    passed, `subtree_entry_count` can never be queried, so this guard axis simply never fires —
    the byte-size guard and the top-level identity check still apply regardless. A real 5-file
    tree, well above the (tiny) configured threshold, still direct-deletes because there is no
    scan index to cheaply count it against."""
    cache_dir = _flat_tree(tmp_path, file_count=5)
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(cache_dir, is_dir=True, size_bytes=5, retention_days=None)],
        safety=_safety(),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
        direct_delete_entry_count_guard=2,
        # scan_index intentionally omitted
    )

    assert report.items[0].method == "direct_delete"
    assert not cache_dir.exists()


def test_size_guard_exempt_candidate_direct_deletes_regardless_of_size(tmp_path: Path) -> None:
    """ADR-0003 addendum: a package-cache-style candidate (`size_guard_exempt=True`) direct-
    deletes even at 20GB — the guard exists to protect expensive-to-recover items, and a package
    manager cache is exactly as cheap to rebuild at 20GB as at 20MB."""
    target = tmp_path / "uv_cache"
    target.mkdir()
    (target / "wheel.whl").write_bytes(b"x")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [
            _candidate(
                target,
                is_dir=True,
                size_bytes=20 * 1024 * 1024 * 1024,
                category="package_cache",
                retention_days=None,
                size_guard_exempt=True,
            )
        ],
        safety=_safety(),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.items[0].method == "direct_delete"
    assert not target.exists()
    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert entries[0].method == "direct_delete"
    assert entries[0].retention_days is None


def test_non_exempt_oversized_candidate_still_vaults_despite_similar_size(tmp_path: Path) -> None:
    """A non-package-cache candidate (`size_guard_exempt=False`, the default) at a comparable
    size to the exempt case above must still hit the guard and vault — the exemption is
    category-scoped, not a blanket size-guard bypass."""
    target = tmp_path / "huge_model.safetensors"
    target.write_bytes(b"x")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [
            _candidate(
                target,
                size_bytes=5 * 1024 * 1024 * 1024,
                category="model_cache",
                retention_days=None,
                size_guard_exempt=False,
            )
        ],
        safety=_safety(),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.items[0].method == "vault"
    assert report.items[0].vault_path is not None
    assert report.items[0].vault_path.exists()
    assert not target.exists()


def test_rebuildable_guard_downgraded_candidate_gets_zero_retention(tmp_path: Path) -> None:
    """ADR-0005: a rebuildable-category candidate (e.g. windows_temp) that the size guard
    downgrades to vault gets `retention_days=0` — immediately purge-eligible, not held for the
    normal 30-day window — since regret is impossible for a category whose only recovery path
    was always "rebuild it"."""
    target = tmp_path / "big_temp_dir"
    target.mkdir()
    (target / "file.bin").write_bytes(b"x")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [
            _candidate(
                target,
                is_dir=True,
                size_bytes=2 * 1024 * 1024 * 1024,
                category="windows_temp",
                retention_days=None,
                rebuildable=True,
            )
        ],
        safety=_safety(),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.items[0].method == "vault"
    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert entries[0].retention_days == 0
    assert entries[0].retention_until == _NOW  # already due — 0-day window from quarantine time


def test_non_rebuildable_guard_downgraded_candidate_keeps_default_retention(
    tmp_path: Path,
) -> None:
    """A guard-downgraded candidate that is NOT rebuildable (the default) keeps the normal
    `size_guard_retention_days` window — the zero-retention override is scoped to rebuildable
    categories only, not a blanket change to the guard's behavior."""
    target = tmp_path / "huge_model.safetensors"
    target.write_bytes(b"x")
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [
            _candidate(
                target,
                size_bytes=5 * 1024 * 1024 * 1024,
                category="model_cache",
                retention_days=None,
                rebuildable=False,
            )
        ],
        safety=_safety(),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        now=_NOW,
    )

    assert report.items[0].method == "vault"
    entries = _latest_entries_for_batch(manifest_path, report.batch_id)
    assert entries[0].retention_days == 30


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
        restore_batch(report.batch_id, manifest_path=manifest_path, safety=_safety())


def test_restore_mixed_batch_restores_vault_entry_and_skips_direct_delete_entry(
    tmp_path: Path,
) -> None:
    """ADR-0004: a batch mixing vault and direct_delete entries (the real shape of the
    2026-07-17 scoped apply — 23,565 direct_delete entries alongside 7 vault ones under one
    batch_id) must restore what's restorable rather than refusing the whole batch. The
    direct_delete entry is reported per-item as `restore_unsupported`, not a whole-call
    exception."""
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

    restore_report = restore_batch(
        report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 1,
    )

    assert restore_report.files_processed == 2
    assert restore_report.files_succeeded == 1
    assert restore_report.files_failed == 0
    assert restore_report.files_unsupported == 1
    assert vaulted.exists()
    assert vaulted.read_bytes() == b"vault-me"
    assert not deleted.exists()  # genuinely gone, never quarantined, never restorable

    by_path = {item.original_path: item for item in restore_report.items}
    assert by_path[vaulted].succeeded is True
    assert by_path[vaulted].restore_unsupported is False
    assert by_path[deleted].succeeded is False
    assert by_path[deleted].restore_unsupported is True
    assert "nothing to restore" in (by_path[deleted].error or "")


def test_restore_mixed_batch_with_recycle_bin_entry_also_partially_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same partial-restore behavior for a vault+recycle_bin mix, not just vault+direct_delete."""
    import reclaim.executor as executor_module

    monkeypatch.setattr(executor_module.send2trash, "send2trash", lambda path: None)

    vaulted = tmp_path / "vaulted.bin"
    vaulted.write_bytes(b"vault-me")
    trashed = tmp_path / "trashed.bin"
    trashed.write_bytes(b"trash-me")
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    apply_report = apply_batch(
        [_candidate(vaulted, retention_days=30, category_group="a")],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    # Same batch_id reused deliberately: manually append a recycle_bin entry sharing the batch,
    # since apply_batch's `method` param is batch-wide and can't itself produce a vault+
    # recycle_bin mix in one call (only vault+direct_delete, via retention_days=None).
    from reclaim.executor import QuarantineManifestEntry, append_manifest_entries

    append_manifest_entries(
        manifest_path,
        [
            QuarantineManifestEntry(
                batch_id=apply_report.batch_id,
                original_path=trashed,
                size_bytes=8,
                is_dir=False,
                category="test_category",
                category_group="a",
                rationale="test",
                rebuild_instruction=None,
                tier=Tier.A,
                method="recycle_bin",
                vault_path=None,
                retention_days=None,
                quarantined_at=_NOW,
                retention_until=None,
            )
        ],
    )

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=_safety(),
        now=_NOW + 1,
    )

    assert restore_report.files_succeeded == 1
    assert restore_report.files_unsupported == 1
    assert vaulted.read_bytes() == b"vault-me"
    by_path = {item.original_path: item for item in restore_report.items}
    assert by_path[trashed].restore_unsupported is True
    assert "Recycle Bin" in (by_path[trashed].error or "")


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


# --- Restore manifest-integrity guard (zip-slip equivalent) ------------------------------------


def test_restore_refuses_whole_batch_when_a_vault_path_escapes_the_vault_dir(
    tmp_path: Path,
) -> None:
    """A vault entry whose recorded vault_path doesn't resolve inside the configured vault
    directory must abort the ENTIRE restore, not just be skipped — this is the shape a
    corrupted/hand-edited manifest.jsonl would take, and trusting it would let restore_batch
    move an arbitrary file from outside the vault to wherever original_path says."""
    from reclaim.executor import append_manifest_entries

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"

    legit_target = tmp_path / "legit.bin"
    legit_target.write_bytes(b"legit-content")
    apply_report = apply_batch(
        [_candidate(legit_target, retention_days=30)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    legit_vault_path = apply_report.items[0].vault_path
    assert legit_vault_path is not None

    # A second, "tampered" entry sharing the same batch_id whose vault_path escapes vault_dir
    # entirely — simulates a corrupted/hand-edited manifest.jsonl, not anything this tool's own
    # apply_batch would ever produce.
    escaping_source = tmp_path / "outside_vault" / "not_actually_vaulted.bin"
    escaping_source.parent.mkdir(parents=True)
    escaping_source.write_bytes(b"should-never-move")
    tampered_original = tmp_path / "restored_elsewhere.bin"

    append_manifest_entries(
        manifest_path,
        [
            QuarantineManifestEntry(
                batch_id=apply_report.batch_id,
                original_path=tampered_original,
                size_bytes=18,
                is_dir=False,
                category="test_category",
                category_group="test_group",
                rationale="test",
                rebuild_instruction=None,
                tier=Tier.A,
                method="vault",
                vault_path=escaping_source,
                retention_days=30,
                quarantined_at=_NOW,
                retention_until=_NOW + 30 * 86400.0,
            )
        ],
    )

    with pytest.raises(RestoreIntegrityError, match="vault_path"):
        restore_batch(
            apply_report.batch_id,
            manifest_path=manifest_path,
            vault_dir=vault_dir,
            safety=_safety(),
            now=_NOW + 1,
        )

    # Refused the WHOLE call — the legit vault entry sharing this batch_id was never moved back
    # either, even though nothing was wrong with it specifically.
    assert legit_vault_path.exists()
    assert not legit_target.exists()
    assert escaping_source.exists()
    assert escaping_source.read_bytes() == b"should-never-move"
    assert not tampered_original.exists()


def test_restore_refuses_whole_batch_when_original_path_is_a_protected_root(
    tmp_path: Path,
) -> None:
    """A vault entry whose original_path matches a protected system root must also abort the
    entire restore — the "never write here, no matter what the manifest says" backstop,
    independent of the vault_path containment check above."""
    from reclaim.executor import append_manifest_entries

    protected_dir = tmp_path / "Windows"
    protected_dir.mkdir()
    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"

    legit_target = tmp_path / "legit.bin"
    legit_target.write_bytes(b"legit-content")
    safety = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=[f"{protected_dir.as_posix()}/*"]))
    )
    apply_report = apply_batch(
        [_candidate(legit_target, retention_days=30)],
        safety=safety,
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )
    legit_vault_path = apply_report.items[0].vault_path
    assert legit_vault_path is not None

    tampered_vault_source = vault_dir / apply_report.batch_id / "tampered_secret.bin"
    tampered_vault_source.parent.mkdir(parents=True, exist_ok=True)
    tampered_vault_source.write_bytes(b"should-never-land-in-windows")
    tampered_original = protected_dir / "secret.bin"

    append_manifest_entries(
        manifest_path,
        [
            QuarantineManifestEntry(
                batch_id=apply_report.batch_id,
                original_path=tampered_original,
                size_bytes=28,
                is_dir=False,
                category="test_category",
                category_group="test_group",
                rationale="test",
                rebuild_instruction=None,
                tier=Tier.A,
                method="vault",
                vault_path=tampered_vault_source,
                retention_days=30,
                quarantined_at=_NOW,
                retention_until=_NOW + 30 * 86400.0,
            )
        ],
    )

    with pytest.raises(RestoreIntegrityError, match="protected system root"):
        restore_batch(
            apply_report.batch_id,
            manifest_path=manifest_path,
            vault_dir=vault_dir,
            safety=safety,
            now=_NOW + 1,
        )

    assert legit_vault_path.exists()
    assert not legit_target.exists()
    assert not tampered_original.exists()


# --- Audit finding C10: OS-level lock around manifest.jsonl appends ----------------------------


def _manifest_entry_for_lock_test(*, batch_id: str, index: int) -> QuarantineManifestEntry:
    """A minimal, valid `phase="done"` entry — content is irrelevant to this test, only that
    every appended line round-trips as valid JSON and none get lost or merged."""
    return QuarantineManifestEntry(
        batch_id=batch_id,
        original_path=Path(f"C:/fake/{batch_id}/item_{index:04d}.txt"),
        size_bytes=index,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        rationale="C10 concurrency regression test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=Path(f"C:/fake/vault/{batch_id}/item_{index:04d}.txt"),
        retention_days=30,
        quarantined_at=_NOW,
        retention_until=_NOW + 30 * 86400.0,
    )


def test_concurrent_manifest_appends_from_two_threads_never_interleave_partial_lines(
    tmp_path: Path,
) -> None:
    """Audit finding C10: `apply_batch`/`restore_batch`/`purge_expired` each independently call
    `_open_manifest_for_sync(manifest_path)` once per batch, hold that file handle open for the
    whole batch, and write via `_append_and_sync` per item. Nothing prevented two of these batch
    calls from running concurrently against the SAME `manifest_path` — e.g. two dashboard
    requests dispatched via FastAPI `BackgroundTasks` (`run_in_threadpool` — real OS threads, not
    cooperative-only) — and two independent file handles opened in append mode, each doing
    multiple buffered `write()` calls per JSON line, could interleave partial lines from
    different threads, corrupting the manifest: this tool's entire audit trail and crash-recovery
    source of truth (`reclaim recover` parses this file to reconcile orphaned intents).

    This spawns two real OS threads (not two processes): Windows byte-range locks — what
    `msvcrt.locking` wraps inside `_acquire_manifest_lock`/`_release_manifest_lock` — are
    associated with the file HANDLE, not the process, so two handles opened by two threads in
    the SAME process contend exactly like two handles opened by two different processes would.
    A thread-based test exercises the identical OS-level contention path a real cross-process
    race would hit, without the added flakiness/cost of spawning and synchronizing real
    subprocesses to prove the same underlying mechanism.
    `tests/test_recovery.py`/`tests/_recovery_crash_harness.py` already cover genuine
    cross-process hard-crash recovery separately (`os._exit()` mid-batch, via subprocess) — this
    test's job is purely to prove the lock actually serializes concurrent writers, not to reprove
    that a subprocess is a subprocess.

    Confirmed this test fails without the fix: temporarily reverting `_acquire_manifest_lock`/
    `_release_manifest_lock` to no-ops (so `_open_manifest_for_sync` opens two fully independent,
    unsynchronized append-mode handles, matching the pre-fix code) makes this test fail
    reliably — corrupted/unparseable lines and/or a wrong total line count.
    """
    manifest_path = tmp_path / "manifest.jsonl"
    entries_per_thread = 300
    errors: list[BaseException] = []

    def _write_batch(batch_id: str) -> None:
        try:
            fh = _open_manifest_for_sync(manifest_path)
            try:
                for index in range(entries_per_thread):
                    entry = _manifest_entry_for_lock_test(batch_id=batch_id, index=index)
                    _append_and_sync(fh, entry)
            finally:
                _close_manifest_for_sync(fh)
        except BaseException as exc:  # pragma: no cover -- surfaced via `errors`, never swallowed
            errors.append(exc)

    threads = [
        threading.Thread(target=_write_batch, args=(batch_id,))
        for batch_id in ("batch_thread_a", "batch_thread_b")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not any(t.is_alive() for t in threads), "a writer thread hung — lock deadlock?"
    assert not errors, f"writer thread(s) raised: {errors!r}"

    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    expected_total = 2 * entries_per_thread
    assert len(lines) == expected_total, (
        f"expected {expected_total} manifest lines (one per _append_and_sync call across both "
        f"threads), got {len(lines)} — lines were lost or merged, a symptom of interleaved "
        "concurrent writes corrupting the manifest"
    )

    parsed_by_batch: dict[str, int] = {"batch_thread_a": 0, "batch_thread_b": 0}
    for line in lines:
        # Every line must independently parse as one complete, valid JSON entry — a corrupted
        # interleave (two threads' partial writes merged into one garbled line, or one write
        # split across two lines) fails this.
        entry = QuarantineManifestEntry.model_validate_json(line)
        parsed_by_batch[entry.batch_id] += 1

    assert parsed_by_batch == {
        "batch_thread_a": entries_per_thread,
        "batch_thread_b": entries_per_thread,
    }


# --- Audit finding C10 (second pass): deadlock/self-block, lock-failure, and ordering proofs ---


def test_reentrant_manifest_lock_acquire_on_second_handle_times_out_not_hangs_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit finding C10 (second pass), point 1: `apply_batch`/`restore_batch`/`purge_expired`
    each call `_open_manifest_for_sync` exactly once per batch, in a local variable, closed
    exactly once from a `finally:` block -- traced across all three call sites, there is no path
    today that calls it a second time on the same manifest before releasing the first handle.
    This test proves what WOULD happen if a future refactor introduced exactly that bug (or any
    other caller opened a second handle to the same `manifest_path` while the first is still
    held): Windows byte-range locks are handle-associated, not thread/process-associated (see
    `_acquire_manifest_lock`'s own docstring), so a second handle contends for the SAME lock
    exactly like a genuinely separate writer would -- there is no special case that lets "the
    same process/thread" skip the queue. The real question: does that contention hang FOREVER
    (a true self-deadlock, since nothing else exists to ever release the first handle), or does
    it fail loud within the bounded retry loop's timeout? Patches
    `_MANIFEST_LOCK_TIMEOUT_SECONDS`/`_MANIFEST_LOCK_POLL_SECONDS` down for test speed -- the
    bound itself, not its production magnitude, is what's being proven here.

    Run on a background thread with a generous `join(timeout=...)`, matching this file's other
    lock test's `t.join(timeout=60)` convention: if `_acquire_manifest_lock` ever regressed into
    a genuine infinite loop, this test fails fast with a clear assertion message instead of
    hanging the whole suite. Confirmed this test fails (times out / `t.is_alive()` stays True)
    if the `if time.monotonic() >= deadline: raise ...` bound is temporarily removed from
    `_acquire_manifest_lock`, restoring an actual infinite retry loop.
    """
    monkeypatch.setattr(executor_module, "_MANIFEST_LOCK_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(executor_module, "_MANIFEST_LOCK_POLL_SECONDS", 0.05)

    manifest_path = tmp_path / "manifest.jsonl"
    first_handle = _open_manifest_for_sync(manifest_path)  # holds the lock for the whole test

    result: dict[str, object] = {}

    def _reentrant_open() -> None:
        start = time.monotonic()
        try:
            executor_module._open_manifest_for_sync(manifest_path)
        except BaseException as exc:  # pragma: no cover -- surfaced via `result`, never swallowed
            result["error"] = exc
        result["elapsed"] = time.monotonic() - start

    t = threading.Thread(target=_reentrant_open)
    t.start()
    t.join(timeout=10)  # >> the patched 1.0s timeout -- a hang here is a real self-deadlock bug
    try:
        assert not t.is_alive(), (
            "a second _open_manifest_for_sync call against an already-locked manifest_path "
            "never returned -- infinite self-block, not a bounded timeout"
        )
        assert isinstance(result.get("error"), ManifestLockTimeoutError), (
            f"expected ManifestLockTimeoutError, got {result.get('error')!r}"
        )
        elapsed = result["elapsed"]
        assert isinstance(elapsed, float)
        # Bounded close to the patched timeout, not near-zero (would mean it never actually
        # contended) and not near the real production default (would mean the monkeypatch
        # didn't take effect and this test just got lucky finishing before the real timeout).
        assert 1.0 <= elapsed < 5.0, f"expected a ~1.0s bounded wait, got {elapsed:.2f}s"
    finally:
        _close_manifest_for_sync(first_handle)


def test_apply_batch_lock_acquisition_failure_leaves_batch_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit finding C10 (second pass), point 2, apply_batch's half: `apply_batch` opens the
    manifest -- and therefore acquires the OS-level lock -- exactly ONCE, before its
    per-candidate loop even starts (see `apply_batch`'s `manifest_fh = _open_manifest_for_sync
    (...)` line, above the `for index, candidate in enumerate(candidates, ...)` loop). A
    lock-acquisition failure must therefore be a clean "batch never started" failure: no
    candidate's source file moved, no manifest line written at all -- never a "some items
    already applied with no manifest trace" partial-batch failure, which would be silently
    worse than the interleaved-write corruption C10 exists to prevent.

    Forces `_acquire_manifest_lock` to fail immediately (rather than waiting out the real
    timeout) by monkeypatching it to always raise -- proves the CONTRACT (what `apply_batch`
    does with that failure), not the retry loop itself (already covered by the reentrant-lock
    test above and the real-crash test in `tests/test_recovery.py`).

    Confirmed this test fails if `apply_batch` is changed to catch `ManifestLockTimeoutError`
    around `_open_manifest_for_sync` and proceed with the batch anyway (a real,
    severity-escalating regression: files would move with zero audit trail) -- verified by
    temporarily wrapping that call in exactly such a try/except during test-writing, which makes
    `source.exists()` false and `pytest.raises` fail.
    """

    def _always_times_out(fh: Any) -> None:
        raise ManifestLockTimeoutError("simulated: lock never acquired")

    monkeypatch.setattr(executor_module, "_acquire_manifest_lock", _always_times_out)

    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    candidate = _candidate(source, size_bytes=source.stat().st_size)

    with pytest.raises(ManifestLockTimeoutError):
        apply_batch(
            [candidate],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=vault_dir,
            manifest_path=manifest_path,
            now=_NOW,
        )

    assert source.exists()  # untouched -- the per-candidate loop never ran
    assert not vault_dir.exists()  # nothing vaulted
    # `_open_manifest_for_sync` creates (but never writes to) the manifest file before the lock
    # acquisition it wraps can fail -- "exists but empty" and "never created" are both valid
    # "nothing was written" outcomes; only actual manifest content would signal a real bug.
    assert not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") == ""


def test_restore_batch_lock_acquisition_failure_leaves_batch_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit finding C10 (second pass), point 2, restore_batch's half: same "batch never
    started" contract as `apply_batch`'s version of this test above -- `restore_batch` opens the
    manifest once, before its `vault_entries` loop, so a lock-acquisition failure must leave the
    vault copy exactly where it was and the manifest exactly as it was before this call (i.e.
    only the ORIGINAL apply's own entries, no new restore-intent line at all)."""
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"
    target = tmp_path / "sub" / "file.bin"
    target.parent.mkdir(parents=True)
    original_content = b"real-bytes" * 50
    target.write_bytes(original_content)

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
    assert vault_path is not None and vault_path.exists()
    manifest_content_before = manifest_path.read_text(encoding="utf-8")

    def _always_times_out(fh: Any) -> None:
        raise ManifestLockTimeoutError("simulated: lock never acquired")

    monkeypatch.setattr(executor_module, "_acquire_manifest_lock", _always_times_out)

    with pytest.raises(ManifestLockTimeoutError):
        restore_batch(
            apply_report.batch_id,
            manifest_path=manifest_path,
            vault_dir=vault_dir,
            safety=_safety(),
            now=_NOW + 10,
        )

    assert not target.exists()  # restore never ran
    assert vault_path.exists()  # vault copy untouched
    assert manifest_path.read_text(encoding="utf-8") == manifest_content_before  # no new lines


def test_two_serialized_batches_preserve_chronological_order_and_blocked_batch_writes_nothing(
    tmp_path: Path,
) -> None:
    """Audit finding C10 (second pass), point 3: with two batches serialized by the lock, batch
    A must fully complete (write every one of its entries and release the lock) BEFORE batch B's
    `_open_manifest_for_sync` call can even return -- so the manifest's line order must match
    real chronological completion order (A's entries form a contiguous prefix, B's a contiguous
    suffix, never interleaved), and B must genuinely write NOTHING while it's blocked waiting --
    not "wrote something that happened to land after A's", but literally has no open handle to
    write through until A releases.

    Uses a `threading.Event` to guarantee B only starts trying to acquire the lock AFTER A has
    confirmed it already holds it -- this makes B's blocked state deterministic (not a timing
    race that could pass by luck) rather than merely probable, unlike
    `test_concurrent_manifest_appends_from_two_threads_never_interleave_partial_lines` above
    (which proves no corruption from two truly-simultaneous writers, but deliberately leaves
    which one wins the race unspecified, so it can't assert a specific before/after ordering).
    """
    manifest_path = tmp_path / "manifest.jsonl"
    a_holds_lock = threading.Event()
    a_may_release = threading.Event()
    entries_per_batch = 20

    def _batch_a() -> None:
        fh = _open_manifest_for_sync(manifest_path)
        a_holds_lock.set()
        a_may_release.wait(timeout=30)
        for index in range(entries_per_batch):
            entry = _manifest_entry_for_lock_test(batch_id="batch_a", index=index)
            _append_and_sync(fh, entry)
        _close_manifest_for_sync(fh)

    def _batch_b() -> None:
        a_holds_lock.wait(timeout=30)  # only ever attempt once A genuinely holds the lock
        fh = _open_manifest_for_sync(manifest_path)  # blocks until A releases
        for index in range(entries_per_batch):
            entry = _manifest_entry_for_lock_test(batch_id="batch_b", index=index)
            _append_and_sync(fh, entry)
        _close_manifest_for_sync(fh)

    a_thread = threading.Thread(target=_batch_a)
    b_thread = threading.Thread(target=_batch_b)
    a_thread.start()
    assert a_holds_lock.wait(timeout=10), "batch A never signaled it holds the manifest lock"
    b_thread.start()

    # Give B a real chance to attempt-and-block while A is still deliberately holding the lock.
    time.sleep(0.5)
    assert b_thread.is_alive(), (
        "batch B's _open_manifest_for_sync returned while batch A still holds the lock -- the "
        "lock did not actually serialize the two batches"
    )
    # B is confirmed blocked -- nothing exists on disk yet from EITHER batch (A hasn't written
    # any entries yet either; it's paused on `a_may_release`), so B has provably written zero
    # bytes while blocked, not merely "zero bytes so far by coincidence". Checked via file SIZE
    # (a metadata-only `os.stat`, no data-range access) rather than `read_text()`: A's exclusive
    # byte-range lock covers byte 0, so a second handle trying to READ that same byte while A
    # holds it legitimately raises `PermissionError` on Windows too -- confirmed empirically
    # while writing this test (a `read_text()` attempt here fails exactly that way), which is
    # itself further proof the lock is real, not merely a write-vs-write convention.
    assert not manifest_path.exists() or manifest_path.stat().st_size == 0

    a_may_release.set()  # let A write its entries and release the lock
    a_thread.join(timeout=30)
    b_thread.join(timeout=30)
    assert not a_thread.is_alive()
    assert not b_thread.is_alive()

    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 * entries_per_batch
    parsed = [QuarantineManifestEntry.model_validate_json(line) for line in lines]
    batch_ids = [entry.batch_id for entry in parsed]
    # Chronological, non-interleaved: every "batch_a" line before every "batch_b" line.
    assert batch_ids == ["batch_a"] * entries_per_batch + ["batch_b"] * entries_per_batch
    # Per-batch internal write order preserved too (index 0..19 in order for each batch) --
    # `_manifest_entry_for_lock_test` stores `index` as `size_bytes`.
    a_sizes = [entry.size_bytes for entry in parsed if entry.batch_id == "batch_a"]
    b_sizes = [entry.size_bytes for entry in parsed if entry.batch_id == "batch_b"]
    assert a_sizes == list(range(entries_per_batch))
    assert b_sizes == list(range(entries_per_batch))
