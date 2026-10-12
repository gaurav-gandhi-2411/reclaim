from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import reclaim.executor as executor_module
from reclaim import safety_env
from reclaim.config import Config
from reclaim.executor import (
    QuarantineManifestEntry,
    append_manifest_entries,
    apply_batch,
    read_manifest_entries,
    restore_batch,
)
from reclaim.models import Candidate, Tier, Verdict
from reclaim.purge import purge_expired
from reclaim.safety import SafetyValidator
from reclaim.safety_env import RealProfileAccessError

# Hermetic by construction: every path lives under tmp_path. The "real profile" is simulated by
# pointing the guard's captured real roots at a directory inside tmp_path (the same idiom as
# tests/test_hermetic_guard.py), so a refusal fires with nothing real in reach.

_NOW = 1_700_000_000.0
_DAY = 86400.0


def _real_zone(tmp_path: Path) -> Path:
    """The stand-in 'real profile': only the files under test live here."""
    return tmp_path / "realzone"


def _sandbox(tmp_path: Path) -> Path:
    """Manifest and vault live here, deliberately NOT under the stand-in real root: the manifest
    writers are guarded too, so a manifest under the real root is refused before any intent
    exists and there would be nothing to abort. (See the dedicated test at the end.)"""
    path = tmp_path / "sandbox"
    path.mkdir(exist_ok=True)
    return path


def _refuse_everything_under(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(safety_env, "_real_roots", (safety_env._norm(root),))
    monkeypatch.setattr(safety_env, "_sandbox_roots", [])
    monkeypatch.setattr(safety_env, "_sandbox_inherited", True)


def _candidate(path: Path, *, retention_days: int | None = 30, size_bytes: int = 10) -> Candidate:
    return Candidate(
        path=path,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        size_bytes=size_bytes,
        tier=Tier.A,
        rationale="test rationale",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="TEST_REASON",
        retention_days=retention_days,
        size_guard_exempt=False,
        rebuildable=False,
    )


def _phases(manifest: Path, operation: str) -> list[str]:
    return [e.phase for e in read_manifest_entries(manifest) if e.operation == operation]


def _two_files(tmp_path: Path) -> tuple[Path, Path]:
    tree = _real_zone(tmp_path) / "tree"
    tree.mkdir(parents=True)
    first, second = tree / "a.bin", tree / "b.bin"
    first.write_bytes(b"a" * 10)
    second.write_bytes(b"b" * 10)
    return first, second


@pytest.mark.parametrize(
    ("method", "retention_days"),
    # direct_delete is never requested for a batch: it is a candidate with retention_days=None.
    [("vault", 30), ("vault", None), ("recycle_bin", 30)],
    ids=["vault", "direct_delete", "recycle_bin"],
)
def test_apply_refusal_closes_the_intent_as_aborted_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: Any, retention_days: int | None
) -> None:
    first, second = _two_files(tmp_path)
    manifest = _sandbox(tmp_path) / "manifest.jsonl"
    _refuse_everything_under(monkeypatch, _real_zone(tmp_path))

    with pytest.raises(RealProfileAccessError):
        apply_batch(
            [_candidate(first, retention_days=retention_days), _candidate(second)],
            safety=SafetyValidator(Config()),
            apply=True,
            method=method,
            vault_dir=_sandbox(tmp_path) / "vault",
            manifest_path=manifest,
            now=_NOW,
        )

    # Nothing moved or deleted, and the batch stopped at the first item (never reached `second`).
    assert first.read_bytes() == b"a" * 10
    assert second.read_bytes() == b"b" * 10
    assert not any((_sandbox(tmp_path) / "vault").rglob("*.bin"))
    # The one intent that was written is closed, not left for recovery to classify.
    assert _phases(manifest, "apply") == ["intent", "aborted"]
    intent, aborted = read_manifest_entries(manifest)
    assert intent.intent_id is not None and intent.intent_id == aborted.intent_id


def test_apply_ordinary_exception_is_still_isolated_per_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behaviour for a normal Exception is unchanged: that item fails (intent + aborted), the
    batch carries on with the next one."""
    first, second = _two_files(tmp_path)
    manifest = tmp_path / "manifest.jsonl"
    real_move = executor_module._atomic_move

    def flaky_move(src: Path, dst: Path, *, is_dir: bool) -> None:
        if src == first:
            raise OSError("simulated disk error")
        real_move(src, dst, is_dir=is_dir)

    monkeypatch.setattr(executor_module, "_atomic_move", flaky_move)

    report = apply_batch(
        [_candidate(first), _candidate(second)],
        safety=SafetyValidator(Config()),
        apply=True,
        vault_dir=tmp_path / "vault",
        manifest_path=manifest,
        now=_NOW,
    )

    assert [i.succeeded for i in report.items] == [False, True]
    assert report.items[0].error == "simulated disk error"
    assert first.exists() and not second.exists()
    assert _phases(manifest, "apply") == ["intent", "aborted", "intent", "done"]


def test_apply_synchronous_purge_refusal_closes_its_own_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0032 second pass: the apply itself succeeds (vaulted), then the immediate purge's
    delete primitive is refused. Only the purge intent is aborted; the vault copy survives."""
    target = tmp_path / "cache" / "huge.bin"
    target.parent.mkdir()
    target.write_bytes(b"x" * 10)
    manifest = tmp_path / "manifest.jsonl"

    def refuse_deletes(path: Any, *, operation: str = "delete", **_kw: Any) -> None:
        if operation in {"delete the file", "remove the tree"}:
            raise RealProfileAccessError(f"refusing to {operation}")

    monkeypatch.setattr(executor_module, "assert_not_real_profile_under_pytest", refuse_deletes)

    with pytest.raises(RealProfileAccessError):
        apply_batch(
            [_candidate(target, retention_days=None, size_bytes=2 * 1024**3)],
            safety=SafetyValidator(Config()),
            apply=True,
            vault_dir=tmp_path / "vault",
            manifest_path=manifest,
            direct_delete_size_guard_retention_days=0,
            now=_NOW,
        )

    assert _phases(manifest, "apply") == ["intent", "done"]
    assert _phases(manifest, "purge") == ["intent", "aborted"]
    assert [p for p in (tmp_path / "vault").rglob("*") if p.is_file()]  # still restorable


def test_restore_refusal_closes_the_intent_as_aborted_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _second = _two_files(tmp_path)
    manifest = _sandbox(tmp_path) / "manifest.jsonl"
    vault = _sandbox(tmp_path) / "vault"
    report = apply_batch(
        [_candidate(first)],
        safety=SafetyValidator(Config()),
        apply=True,
        vault_dir=vault,
        manifest_path=manifest,
        now=_NOW,
    )
    assert not first.exists()
    vaulted = report.items[0].vault_path
    assert vaulted is not None

    _refuse_everything_under(monkeypatch, _real_zone(tmp_path))  # restore target is under it
    with pytest.raises(RealProfileAccessError):
        restore_batch(
            report.batch_id,
            manifest_path=manifest,
            vault_dir=vault,
            safety=SafetyValidator(Config()),
        )

    assert not first.exists() and vaulted.exists()  # nothing was moved back
    assert _phases(manifest, "restore") == ["intent", "aborted"]


def test_purge_refusal_closes_the_intent_as_aborted_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _sandbox(tmp_path) / "manifest.jsonl"
    # The purge target (the vault copy) is what the guard refuses, so it sits under the real root.
    vault_file = _real_zone(tmp_path) / "vault" / "item.bin"
    vault_file.parent.mkdir(parents=True)
    vault_file.write_bytes(b"x" * 10)
    entry = QuarantineManifestEntry(
        batch_id="batch_test",
        original_path=tmp_path / "gone.bin",
        size_bytes=10,
        is_dir=False,
        category="old_installer",
        category_group="old_installers",
        rationale="test rationale",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=vault_file,
        retention_days=30,
        quarantined_at=_NOW - 40 * _DAY,
        retention_until=_NOW - 10 * _DAY,
    )
    append_manifest_entries(manifest, [entry])
    _refuse_everything_under(monkeypatch, _real_zone(tmp_path))

    with pytest.raises(RealProfileAccessError):
        purge_expired(
            apply=True,
            manifest_path=manifest,
            vault_dir=_real_zone(tmp_path) / "vault",
            safety=SafetyValidator(Config()),
            now=_NOW,
        )

    assert vault_file.exists()
    assert _phases(manifest, "purge") == ["intent", "aborted"]


def test_a_manifest_under_the_real_root_is_refused_before_any_entry_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documents the behaviour once the manifest writers are guarded as well: if the manifest
    itself is under the real root, the refusal propagates before the first intent, nothing is
    moved, and no manifest file (not even a partial one) is left behind. Holds with or without
    that guard: either the writer refuses, or the move guard refuses after an intent that is then
    closed, and in both cases the target is untouched and the refusal propagates."""
    first, _second = _two_files(tmp_path)
    manifest = _real_zone(tmp_path) / "manifest.jsonl"
    _refuse_everything_under(monkeypatch, _real_zone(tmp_path))

    with pytest.raises(RealProfileAccessError):
        apply_batch(
            [_candidate(first)],
            safety=SafetyValidator(Config()),
            apply=True,
            vault_dir=_sandbox(tmp_path) / "vault",
            manifest_path=manifest,
            now=_NOW,
        )

    assert first.read_bytes() == b"a" * 10
    if manifest.exists():  # pre-#145 behaviour: an intent was written and then closed
        assert _phases(manifest, "apply") == ["intent", "aborted"]
