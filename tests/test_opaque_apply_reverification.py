from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import reclaim.executor as executor_module
from reclaim.config import Config
from reclaim.executor import ItemApplyResult, apply_batch
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Tier, Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import long_path, scan_tree

# ADR-0042: the apply-time re-verification of an opaque dependency tree, driven through the public
# `apply_batch` direct-delete path. (evals/test_apply_opaque_reverify.py holds the safety-gate
# copy of the headline cases; this file lives under tests/ so the per-module coverage gate that
# CI measures from tests/ alone sees these branches too, and it stands alone on purpose.)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Reclaim targets Windows/NTFS exclusively"
)

_NOW = 1_700_000_000.0


def _candidate(path: Path, index: ScanIndex, *, retention_days: int | None = None) -> Candidate:
    record = index.get_record(path)
    assert record is not None, f"no scan record for {path}"
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
        safety=SafetyValidator(Config()),
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


def _node_modules(root: Path) -> Path:
    nm = root / "app" / "node_modules"
    (nm / "left-pad").mkdir(parents=True)
    (nm / "left-pad" / "index.js").write_bytes(b"module.exports = 1;\n")
    (nm / "react" / "lib").mkdir(parents=True)
    (nm / "react" / "lib" / "react.js").write_bytes(b"r" * 500)
    return nm


def _age(root: Path, days: float = 40) -> None:
    old = time.time() - days * 86400
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in (*filenames, *dirnames):
            os.utime(os.path.join(dirpath, name), (old, old))  # noqa: PTH118
    os.utime(root, (old, old))


def _refused(result: ItemApplyResult) -> bool:
    return result.succeeded is False and result.skip_reason == "identity_changed_since_scan"


def _make_venv(venv: Path, *, module: str, content: bytes) -> Path:
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = C:/Python\n", encoding="utf-8")
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ-fake-interpreter")
    site = venv / "Lib" / "site-packages" / "pkg"
    site.mkdir(parents=True)
    path = site / module
    path.write_bytes(content)
    return path


def _fault(monkeypatch: pytest.MonkeyPatch, error: OSError, suffix: str) -> None:
    """os.stat and os.lstat raise `error` for paths ending in `suffix`."""

    def wrap(real: Callable[..., os.stat_result]) -> Callable[..., os.stat_result]:
        def inner(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if str(path).endswith(suffix):
                raise error
            return real(path, *args, **kwargs)

        return inner

    monkeypatch.setattr(os, "stat", wrap(os.stat))
    monkeypatch.setattr(os, "lstat", wrap(os.lstat))


# --- accepted / aggregate components ------------------------------------------------------------


def test_an_untouched_opaque_node_modules_is_direct_deleted(tmp_path: Path) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        result = _apply(_candidate(nm, index), index, tmp_path)
    assert result.succeeded is True and result.method == "direct_delete"
    assert not nm.exists()


def _add(nm: Path) -> None:
    (nm / "react" / "lib" / "new.js").write_bytes(b"n")


def _remove(nm: Path) -> None:
    (nm / "left-pad" / "index.js").unlink()


def _grow(nm: Path) -> None:
    with (nm / "react" / "lib" / "react.js").open("ab") as handle:
        handle.write(b"more")


def _touch_newer(nm: Path) -> None:
    deep = nm / "react" / "lib" / "react.js"
    newer = deep.stat().st_mtime + 1000
    os.utime(deep, (newer, newer))


def _rename_dir(nm: Path) -> None:
    (nm / "left-pad").rename(nm / "left-pad-renamed")


def _transient_dir(nm: Path) -> None:
    transient = nm / "react" / "lib" / "transient"
    transient.mkdir()
    transient.rmdir()


@pytest.mark.parametrize(
    "change",
    [_add, _remove, _grow, _touch_newer, _rename_dir, _transient_dir],
    ids=["added", "removed", "grown", "newer_mtime", "renamed_dir", "transient_dir"],
)
def test_any_change_inside_an_opaque_tree_makes_the_direct_delete_refuse(
    tmp_path: Path, change: Callable[[Path], None]
) -> None:
    nm = _node_modules(tmp_path)
    _age(nm)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(nm, index)
        change(nm)
        result = _apply(candidate, index, tmp_path)
    assert _refused(result)
    assert nm.exists()


def test_only_the_logical_byte_total_changing_refuses(tmp_path: Path) -> None:
    """Two offsetting swaps keep count, every mtime and the physical total equal; only the
    logical byte total moves."""
    nm = tmp_path / "app" / "node_modules"
    for sub in ("p", "q", "r", "t"):
        (nm / sub).mkdir(parents=True)
    file_p, file_q = nm / "p" / "f.js", nm / "q" / "f.js"
    file_r, file_t = nm / "r" / "f.js", nm / "t" / "f.js"
    file_p.write_bytes(b"p" * 50)
    os.link(file_p, file_q)
    file_r.write_bytes(b"r" * 80)
    file_t.write_bytes(b"t" * 30)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(nm, index)

        def swap(target: Path, make: Callable[[], None]) -> None:
            before, before_dir = target.stat(), target.parent.stat()
            target.unlink()
            make()
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.utime(target.parent, ns=(before_dir.st_atime_ns, before_dir.st_mtime_ns))

        before_t = file_t.stat()
        swap(file_q, lambda: file_q.write_bytes(b"q" * 80))
        swap(file_r, lambda: os.link(file_t, file_r))
        os.utime(file_t, ns=(before_t.st_atime_ns, before_t.st_mtime_ns))
        assert _refused(_apply(candidate, index, tmp_path))


def test_only_the_physical_byte_total_changing_refuses(tmp_path: Path) -> None:
    nm = tmp_path / "app" / "node_modules"
    (nm / "a").mkdir(parents=True)
    (nm / "b").mkdir()
    file_a, file_b = nm / "a" / "x.js", nm / "b" / "x.js"
    file_a.write_bytes(b"1" * 100)
    file_b.write_bytes(b"2" * 100)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(nm, index)
        before_b, before_dir = file_b.stat(), file_b.parent.stat()
        file_b.unlink()
        os.link(file_a, file_b)
        os.utime(file_b, ns=(before_b.st_atime_ns, before_b.st_mtime_ns))
        os.utime(file_b.parent, ns=(before_dir.st_atime_ns, before_dir.st_mtime_ns))
        assert _refused(_apply(candidate, index, tmp_path))


# --- opaque tree nested inside a larger candidate -----------------------------------------------


def test_an_opaque_child_of_a_larger_candidate_is_judged_by_its_aggregate(tmp_path: Path) -> None:
    nm = _node_modules(tmp_path)
    project = nm.parent
    (project / "package.json").write_text("{}", encoding="utf-8")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(project, index)
        # files below the opaque child must not trip "present now, never recorded"
        assert executor_module._direct_delete_directory_mismatch(candidate, index) is None
        (nm / "react" / "lib" / "sneaky.js").write_bytes(b"s")
        assert executor_module._direct_delete_directory_mismatch(candidate, index) is not None


# --- unreadable refuses, vanished skips ---------------------------------------------------------


def test_an_unremeasurable_opaque_tree_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(nm, index)
        monkeypatch.setattr(executor_module, "aggregate_tree", lambda *a, **k: None)
        assert _refused(_apply(candidate, index, tmp_path))
    assert nm.exists()


def test_an_unreadable_live_file_refuses_and_a_vanished_one_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(nm, index)
        # isolate the per-file hardlink loop from the aggregate recount
        monkeypatch.setattr(executor_module, "_opaque_aggregate_mismatch", lambda _r: None)
        with monkeypatch.context() as ctx:
            _fault(ctx, PermissionError(13, "denied"), "react.js")
            detail = executor_module._direct_delete_directory_mismatch(candidate, index)
            assert detail is not None and "could not be inspected" in detail
            assert _refused(_apply(candidate, index, tmp_path))
        with monkeypatch.context() as ctx:
            _fault(ctx, FileNotFoundError(2, "gone"), "react.js")
            assert executor_module._direct_delete_directory_mismatch(candidate, index) is None


def test_an_unreadable_opaque_root_refuses_and_a_vanished_one_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        record = index.get_record(nm)
        assert record is not None
        with monkeypatch.context() as ctx:
            _fault(ctx, PermissionError(13, "denied"), "node_modules")
            assert executor_module._opaque_aggregate_mismatch(record) is not None
        with monkeypatch.context() as ctx:
            _fault(ctx, FileNotFoundError(2, "gone"), "node_modules")
            assert executor_module._opaque_aggregate_mismatch(record) is None


def test_a_candidate_whose_identity_cannot_be_inspected_is_refused_with_an_honest_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(nm, index)
        with monkeypatch.context() as ctx:
            _fault(ctx, PermissionError(13, "denied"), "node_modules")
            mismatch = executor_module._top_level_identity_mismatch(candidate)
            assert mismatch is not None
            reason, detail = mismatch
            assert reason == "identity_changed_since_scan"
            assert "could not be inspected" in detail and "swapped" not in detail
            assert _refused(_apply(candidate, index, tmp_path))


def test_a_swapped_candidate_reports_the_identity_mismatch_detail(tmp_path: Path) -> None:
    nm = _node_modules(tmp_path)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(nm, index)
        wrong = replace(candidate, ino=candidate.ino + 1)
        mismatch = executor_module._top_level_identity_mismatch(wrong)
        assert mismatch is not None and "no longer matches" in mismatch[1]


# --- hardlink guard -----------------------------------------------------------------------------


def test_an_opaque_venv_hardlinked_into_another_environment_is_refused(tmp_path: Path) -> None:
    venv_a = tmp_path / "proj_a" / ".venv"
    venv_b = tmp_path / "proj_b" / ".venv"
    module_a = _make_venv(venv_a, module="shared_mod.py", content=b"shared-bytes")
    _make_venv(venv_b, module="own_mod.py", content=b"b-own")
    os.link(module_a, venv_b / "Lib" / "site-packages" / "pkg" / "alias.py")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        candidate = _candidate(venv_a, index)
        detail = executor_module._direct_delete_directory_mismatch(candidate, index)
        assert detail is not None and "hardlink-connected" in detail
        assert _refused(_apply(candidate, index, tmp_path))
    assert module_a.read_bytes() == b"shared-bytes"


def test_the_same_opaque_venv_without_the_link_is_deleted(tmp_path: Path) -> None:
    venv_a = tmp_path / "proj_a" / ".venv"
    venv_b = tmp_path / "proj_b" / ".venv"
    _make_venv(venv_a, module="shared_mod.py", content=b"shared-bytes")
    _make_venv(venv_b, module="own_mod.py", content=b"b-own")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        result = _apply(_candidate(venv_a, index), index, tmp_path)
    assert result.succeeded is True and result.method == "direct_delete"
    assert not venv_a.exists() and venv_b.exists()


def test_a_deep_over_max_path_venv_file_hardlinked_elsewhere_is_refused(tmp_path: Path) -> None:
    venv_a = tmp_path / "proj_a" / ".venv"
    venv_b = tmp_path / "proj_b" / ".venv"
    (venv_a / "Scripts").mkdir(parents=True)
    (venv_a / "pyvenv.cfg").write_text("home = C:/Python\n", encoding="utf-8")
    (venv_a / "Scripts" / "python.exe").write_bytes(b"MZ")
    deep = venv_a / "Lib" / "site-packages" / "pkg"
    for level in range(8):
        deep = deep / (f"level{level}_" + "d" * 38)
    os.makedirs(long_path(deep), exist_ok=True)  # noqa: PTH103 -- \\?\ str, not Path
    module = deep / "deepmod.py"
    with open(long_path(module), "wb") as handle:  # noqa: PTH123 -- \\?\ str, not Path
        handle.write(b"deep-shared")
    assert len(str(module)) > 260
    _make_venv(venv_b, module="own_mod.py", content=b"b-own")
    try:
        os.link(long_path(module), venv_b / "Lib" / "site-packages" / "pkg" / "alias.py")
        with ScanIndex(tmp_path / "index.sqlite3") as index:
            scan_tree(tmp_path, index)
            result = _apply(_candidate(venv_a, index), index, tmp_path)
        assert _refused(result)
        assert os.path.exists(long_path(module))  # noqa: PTH110 -- \\?\ str, not Path
    finally:
        shutil.rmtree(long_path(tmp_path / "proj_a"), ignore_errors=True)


# --- entry-count guard counts files inside an opaque tree ---------------------------------------


def test_entry_count_guard_counts_the_files_inside_an_opaque_tree(tmp_path: Path) -> None:
    nm = tmp_path / "node_modules"
    nm.mkdir()
    for i in range(12):
        (nm / f"f{i}.js").write_bytes(b"x")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path, index)
        assert index.subtree_entry_count(nm) == 1 + 12
        result = _apply(_candidate(nm, index), index, tmp_path, direct_delete_entry_count_guard=5)
    assert result.method == "vault"
