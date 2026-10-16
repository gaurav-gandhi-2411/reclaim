from __future__ import annotations

import os
from pathlib import Path

import pytest

from reclaim.config import Config
from reclaim.executor import (
    ItemApplyResult,
    _direct_delete_directory_mismatch,
    apply_batch,
)
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Tier, Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

# ADR-0042 teeth: a dependency tree (node_modules, .venv, ...) is ONE index row carrying
# aggregates, so the per-file recorded-identity comparison M1 used to run over it is replaced by
# (a) an aggregate equality check (files, bytes, newest mtime) and (b) the unchanged LIVE per-file
# hardlink loop. These tests are the proof that neither half was lost: each one fails if the
# corresponding check is neutered (mutation results are recorded in the PR).
#
# Registered in scripts/verify.py's `_SAFETY_GATE_FILES`, like every other apply-safety eval.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner/executor target Windows/NTFS only")

_NOW = 1_700_000_000.0


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


def _candidate_for(path: Path, index: ScanIndex, *, retention_days: int | None) -> Candidate:
    record = index.get_record(path)
    assert record is not None, f"no scan record for {path}"
    assert record.opaque is True, f"{path} was expected to be an opaque root"
    return Candidate(
        path=path,
        is_dir=True,
        category="test_category",
        category_group="test_group",
        size_bytes=record.agg_bytes,
        tier=Tier.A,
        rationale="test rationale",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="TEST_REASON",
        retention_days=retention_days,
        dev=record.dev,
        ino=record.ino,
        mtime=record.mtime,
    )


def _apply(
    candidate: Candidate, index: ScanIndex, tmp_path: Path, **kwargs: object
) -> ItemApplyResult:
    report = apply_batch(
        [candidate],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
        scan_index=index,
        **kwargs,  # type: ignore[arg-type]
    )
    assert len(report.items) == 1
    return report.items[0]


def _make_venv(venv: Path, *, module_name: str, content: bytes) -> Path:
    """A `.venv` whose structure `dedup._environment_root` recognises (pyvenv.cfg + interpreter
    under Scripts + Lib/site-packages). Returns the module file."""
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = C:\\Python\n", encoding="utf-8")
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ-fake-interpreter")
    site = venv / "Lib" / "site-packages" / "pkg"
    site.mkdir(parents=True)
    module = site / module_name
    module.write_bytes(content)
    return module


# --- (i) hardlink into a second recognised environment must still be refused -------------------


def test_opaque_venv_with_a_file_hardlinked_into_another_environment_is_refused(
    tmp_path: Path,
) -> None:
    venv_a = tmp_path / "proj_a" / ".venv"
    venv_b = tmp_path / "proj_b" / ".venv"
    module_a = _make_venv(venv_a, module_name="shared_mod.py", content=b"shared-bytes")
    _make_venv(venv_b, module_name="own_mod.py", content=b"b-own")
    os.link(module_a, venv_b / "Lib" / "site-packages" / "pkg" / "shared_alias.py")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate_for(venv_a, index, retention_days=None)
        # The aggregate of the live tree still equals the recorded one: ONLY the hardlink loop
        # can stop this apply.
        assert _direct_delete_directory_mismatch(candidate, index) is not None
        result = _apply(candidate, index, tmp_path)

    assert result.succeeded is False
    assert result.skip_reason == "identity_changed_since_scan"
    assert venv_a.exists() and module_a.read_bytes() == b"shared-bytes"


def test_the_same_opaque_venv_without_the_link_is_accepted_and_deleted(tmp_path: Path) -> None:
    venv_a = tmp_path / "proj_a" / ".venv"
    venv_b = tmp_path / "proj_b" / ".venv"
    _make_venv(venv_a, module_name="shared_mod.py", content=b"shared-bytes")
    _make_venv(venv_b, module_name="own_mod.py", content=b"b-own")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate_for(venv_a, index, retention_days=None)
        assert _direct_delete_directory_mismatch(candidate, index) is None
        result = _apply(candidate, index, tmp_path)

    assert result.succeeded is True
    assert result.method == "direct_delete"
    assert not venv_a.exists()
    assert venv_b.exists()


# --- (ii) any change inside an opaque node_modules between scan and apply must refuse -----------


def _node_modules(root: Path) -> Path:
    nm = root / "app" / "node_modules"
    (nm / "left-pad").mkdir(parents=True)
    (nm / "left-pad" / "index.js").write_bytes(b"module.exports = 1;\n")
    (nm / "react" / "lib").mkdir(parents=True)
    (nm / "react" / "lib" / "react.js").write_bytes(b"r" * 500)
    return nm


@pytest.mark.parametrize("change", ["added", "removed", "grown", "newer_mtime_same_size"])
def test_a_change_inside_an_opaque_node_modules_makes_the_direct_delete_refuse(
    tmp_path: Path, change: str
) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate_for(nm, index, retention_days=None)
        recorded = index.get_record(nm)
        assert recorded is not None and recorded.agg_newest_mtime is not None
        assert _direct_delete_directory_mismatch(candidate, index) is None  # control: untouched

        deep = nm / "react" / "lib" / "react.js"
        if change == "added":
            (nm / "react" / "lib" / "new.js").write_bytes(b"n")
        elif change == "removed":
            (nm / "left-pad" / "index.js").unlink()
        elif change == "grown":
            with deep.open("ab") as handle:
                handle.write(b"more")
        else:
            bumped = recorded.agg_newest_mtime + 1000
            os.utime(deep, (bumped, bumped))  # same size, newer content timestamp

        detail = _direct_delete_directory_mismatch(candidate, index)
        assert detail is not None
        result = _apply(candidate, index, tmp_path)

    assert result.succeeded is False
    assert result.skip_reason == "identity_changed_since_scan"
    assert nm.exists()


def test_an_untouched_opaque_node_modules_is_direct_deleted(tmp_path: Path) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        result = _apply(_candidate_for(nm, index, retention_days=None), index, tmp_path)
    assert result.succeeded is True and result.method == "direct_delete"
    assert not nm.exists()


def test_a_change_inside_an_opaque_child_of_a_larger_direct_delete_candidate_is_refused(
    tmp_path: Path,
) -> None:
    """The candidate is the PROJECT directory; the opaque tree is a child. Entries below the
    opaque child must not trip the 'present now, never recorded' rule, but a real change there
    must still be caught by the child's aggregate."""
    nm = _node_modules(tmp_path)
    project = nm.parent
    (project / "package.json").write_text("{}", encoding="utf-8")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        record = index.get_record(project)
        assert record is not None
        candidate = Candidate(
            path=project,
            is_dir=True,
            category="test_category",
            category_group="test_group",
            size_bytes=index.subtree_size_bytes(project),
            tier=Tier.A,
            rationale="r",
            rebuild_instruction=None,
            safety_verdict=Verdict.ELIGIBLE,
            safety_reason_code="T",
            retention_days=None,
            dev=record.dev,
            ino=record.ino,
        )
        assert _direct_delete_directory_mismatch(candidate, index) is None
        (nm / "react" / "lib" / "sneaky.js").write_bytes(b"s")
        assert _direct_delete_directory_mismatch(candidate, index) is not None


def test_an_unremeasurable_opaque_tree_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.executor as executor_module

    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate_for(nm, index, retention_days=None)
        monkeypatch.setattr(executor_module, "aggregate_tree", lambda *a, **k: None)
        assert _direct_delete_directory_mismatch(candidate, index) is not None


# --- apply-time entry-count guard still trips on an opaque tree ---------------------------------


def test_entry_count_guard_counts_the_files_inside_an_opaque_tree(tmp_path: Path) -> None:
    nm = tmp_path / "node_modules"
    nm.mkdir()
    for i in range(12):
        (nm / f"f{i}.js").write_bytes(b"x")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        assert index.subtree_entry_count(nm) == 1 + 12
        result = _apply(
            _candidate_for(nm, index, retention_days=None),
            index,
            tmp_path,
            direct_delete_entry_count_guard=5,  # the 12 files are no longer rows, still counted
        )
    assert result.method == "vault"
