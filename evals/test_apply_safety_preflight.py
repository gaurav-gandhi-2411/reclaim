from __future__ import annotations

import os
from pathlib import Path

import pytest

from reclaim.config import (
    CategoriesConfig,
    Config,
    DuplicatesConfig,
    SafetyConfig,
)
from reclaim.dedup import generate_duplicate_candidates
from reclaim.executor import ItemApplyResult, apply_batch
from reclaim.index import ScanIndex
from reclaim.models import Candidate, FileRecord, Tier, Verdict
from reclaim.preflight import (
    check_file_in_use,
    check_hardlink_shared_active_install,
    enumerate_directory_identity,
)
from reclaim.safety import SafetyValidator
from reclaim.scanner import long_path

# Audit P0-1 (docs/AUDIT-2026-08.md): the safety-named home for both R6 pre-flight regression
# guards ("no live process holds it", "not hardlink-backed into an active install") AND the
# ADR-0009 real-disk-incident regression test moved here from `tests/test_dedup.py` (audit P2 --
# that test was undiscoverable from the safety-test naming convention it now lives on). See
# `scripts/verify.py`'s own `_SAFETY_GATE_FILES` tuple, which this file is registered in, so a
# bare `uv run pytest tests/` (which silently skips all of `evals/` -- see that script's own
# docstring) is never the last word on whether these guards still pass.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner/executor target Windows/NTFS only")

_NOW = 1_700_000_000.0


# --- Shared test helpers ------------------------------------------------------------------------
#
# Duplicated (not imported) from `tests/test_dedup.py`/`tests/test_executor.py` rather than
# pulled in via a cross-directory import: `tests/` and `evals/` are two independent pytest
# rootdirs today (no shared conftest/utils module between them, same "duplicate twice, abstract
# on the third occurrence" convention this codebase already applies elsewhere -- e.g. `_due` is
# triplicated across `scanner.py`/`executor.py`/`dedup.py`, each documenting why).


def _index_record(
    path: str,
    *,
    size_bytes: int,
    mtime: float = 100.0,
    ctime: float = 100.0,
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=False,
        size_bytes=size_bytes,
        attributes=0,
        ext=p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=mtime,
        ctime=ctime,
    )


def _make_conda_env(root: Path) -> Path:
    """A real `conda-meta/` marker directory -- the canonical signal conda itself uses to
    recognize a directory as an environment root (base install or named env alike)."""
    (root / "conda-meta").mkdir(parents=True)
    return root


def _make_standalone_python_install(root: Path) -> Path:
    """A real `python.exe` + `Lib/` pair -- the canonical, tool-agnostic structural signature of
    a complete standalone CPython installation (has neither `conda-meta/` nor `pyvenv.cfg`)."""
    root.mkdir(parents=True)
    (root / "python.exe").write_bytes(b"fake-exe")
    (root / "Lib").mkdir()
    return root


def _make_venv(root: Path) -> Path:
    """A real `pyvenv.cfg` marker file -- the canonical signal Python's own `venv` module uses.
    Mirrors `tests/test_dedup.py::_make_venv` exactly."""
    root.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = C:/Python312\n")
    return root


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
    )


def _result_for(report_items: tuple[ItemApplyResult, ...], path: Path) -> ItemApplyResult:
    for item in report_items:
        if item.path == path:
            return item
    raise AssertionError(f"no ItemApplyResult for {path} in {report_items}")


# --- Moved from tests/test_dedup.py (audit P2) -------------------------------------------------


def test_generate_duplicate_candidates_excludes_standalone_python_install_copy(
    tmp_path: Path,
) -> None:
    """ADR-0009: the actual real-disk incident this closes. A shared, uv-managed Python
    installation's stdlib file (`Lib/socket.py`) was byte-identical to a named conda
    environment's own copy of the same stdlib file -- before this fix, the uv install wasn't
    recognized as ANY kind of environment (`_environment_root` returned `None` for it, since it
    has neither `conda-meta/` nor `pyvenv.cfg`), so it was proposed for deletion and applied for
    real, breaking every project on the machine that referenced that shared interpreter build.
    Recovered from the Windows Recycle Bin afterward -- this test is the regression guard so it
    can't happen again."""
    uv_python = _make_standalone_python_install(
        tmp_path / "uv" / "python" / "cpython-3.12.12-windows-x86_64-none"
    )
    uv_socket_path = uv_python / "Lib" / "socket.py"
    uv_socket_path.write_bytes(b"stdlib-socket-module-" * 1_000)

    conda_root = tmp_path / "anaconda3"
    named_env = _make_conda_env(conda_root / "envs" / "tes-cleanroom-080")
    env_socket_path = named_env / "Lib" / "socket.py"
    env_socket_path.parent.mkdir(parents=True, exist_ok=True)
    env_socket_path.write_bytes(uv_socket_path.read_bytes())
    size = uv_socket_path.stat().st_size

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [
                _index_record(str(env_socket_path), size_bytes=size, ctime=100.0),  # kept
                _index_record(str(uv_socket_path), size_bytes=size, ctime=200.0),
            ],
            scanned_at=1000.0,
        )
        config = Config(
            safety=SafetyConfig(protected_roots=[]),
            categories=CategoriesConfig(duplicates=DuplicatesConfig(min_reclaim_bytes=0)),
        )
        candidates = generate_duplicate_candidates(index, config, SafetyValidator(config))

    assert candidates == []  # the shared interpreter install's copy must never be proposed


# --- (a) Live-process handle check: reclaim.preflight.check_file_in_use ------------------------


def test_check_file_in_use_false_for_an_untouched_file(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    assert check_file_in_use(target, is_dir=False) is False


def test_check_file_in_use_true_while_a_handle_is_held_open_in_this_process(
    tmp_path: Path,
) -> None:
    """The exact real technique this guard is built on: a plain Python `open()` (default,
    permissive share mode) held in THIS SAME process is enough to trigger
    `ERROR_SHARING_VIOLATION` against `check_file_in_use`'s exclusive-mode probe -- verified
    directly against the real Win32 API before writing `preflight.py`, not assumed from
    documentation. No other process, mock, or monkeypatch is needed to reproduce "a live process
    holds this file open"."""
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")

    handle = open(target, "r+b")  # noqa: SIM115, PTH123 -- held deliberately across the assertion
    try:
        assert check_file_in_use(target, is_dir=False) is True
    finally:
        handle.close()

    # Once released, the same path is no longer flagged.
    assert check_file_in_use(target, is_dir=False) is False


def test_check_file_in_use_false_for_a_nonexistent_path(tmp_path: Path) -> None:
    """A vanished path is a DIFFERENT, already-handled problem (the real mutation attempt's own
    per-item try/except in `apply_batch`) -- not evidence of a live process holding it open."""
    assert check_file_in_use(tmp_path / "does-not-exist.bin", is_dir=False) is False


def test_check_file_in_use_directory_flags_when_a_top_level_file_is_open(tmp_path: Path) -> None:
    """Directory candidates are probed via a bounded sample of their own top-level FILES (never
    recursed into subdirectories -- see `preflight._MAX_DIRECTORY_TOP_LEVEL_PROBES`)."""
    root = tmp_path / "some_cache_dir"
    root.mkdir()
    locked_file = root / "locked.bin"
    locked_file.write_bytes(b"x")
    (root / "sibling.bin").write_bytes(b"y")

    handle = open(locked_file, "r+b")  # noqa: SIM115, PTH123
    try:
        assert check_file_in_use(root, is_dir=True) is True
    finally:
        handle.close()

    assert check_file_in_use(root, is_dir=True) is False


def test_check_file_in_use_directory_does_not_recurse_into_subdirectories(tmp_path: Path) -> None:
    """Documented, deliberate limitation: a lock on a file NESTED inside a subdirectory of the
    candidate directory is invisible to this specific probe -- the real move/delete attempt's own
    try/except in `apply_batch` is what catches it when the actual mutation is attempted."""
    root = tmp_path / "some_cache_dir"
    nested = root / "nested"
    nested.mkdir(parents=True)
    nested_locked = nested / "locked.bin"
    nested_locked.write_bytes(b"x")

    handle = open(nested_locked, "r+b")  # noqa: SIM115, PTH123
    try:
        assert check_file_in_use(root, is_dir=True) is False  # not detected -- documented gap
    finally:
        handle.close()


# --- (b) Hardlink-into-active-install check: reclaim.preflight.check_hardlink_shared... --------


def test_check_hardlink_shared_active_install_false_when_nlink_is_one(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    result = check_hardlink_shared_active_install(target)
    assert result.is_shared_with_other_environment is False
    assert result.sibling_environment_roots == ()


def test_check_hardlink_shared_active_install_false_when_siblings_are_same_environment(
    tmp_path: Path,
) -> None:
    """Two hardlinked names inside the SAME environment must never flag -- only a sibling
    resolving into a DIFFERENT environment root is a positive detection."""
    venv = _make_venv(tmp_path / "project" / ".venv")
    site_packages = venv / "Lib" / "site-packages" / "pkg"
    site_packages.mkdir(parents=True)
    original = site_packages / "module.py"
    original.write_bytes(b"shared-content")
    duplicate_name_same_env = site_packages / "module_alias.py"
    os.link(original, duplicate_name_same_env)

    result = check_hardlink_shared_active_install(original)
    assert result.is_shared_with_other_environment is False


def test_check_hardlink_shared_active_install_detects_sibling_in_a_different_live_environment(
    tmp_path: Path,
) -> None:
    """The real-disk shape this audit found live on this machine: a cache file (no environment
    of its own -- `_environment_root` returns `None` for it) hardlinked into several DIFFERENT
    live `.venv`-shaped environments. Builds a real hardlink via `os.link()` -- no mock, no
    monkeypatch -- across several fake `.venv` roots using the same structural marker
    (`pyvenv.cfg`) `dedup._environment_root` looks for."""
    cache_root = tmp_path / "uv-cache" / "archive-v0" / "abc123"
    cache_root.mkdir(parents=True)
    cache_file = cache_root / "mypy_extensions.py"
    cache_file.write_bytes(b"stdlib-ish-module-content")

    venv_roots: list[Path] = []
    for i in range(3):
        venv = _make_venv(tmp_path / f"project_{i}" / ".venv")
        site_packages = venv / "Lib" / "site-packages"
        site_packages.mkdir(parents=True)
        linked = site_packages / "mypy_extensions.py"
        os.link(cache_file, linked)
        venv_roots.append(venv)

    assert cache_file.stat().st_nlink == 4  # cache original + 3 venv-linked names

    result = check_hardlink_shared_active_install(cache_file)
    assert result.is_shared_with_other_environment is True
    assert result.own_environment_root is None  # the cache itself isn't inside any environment
    assert set(result.sibling_environment_roots) == set(venv_roots)


def test_check_hardlink_shared_active_install_false_for_an_unresolvable_path(
    tmp_path: Path,
) -> None:
    result = check_hardlink_shared_active_install(tmp_path / "does-not-exist.bin")
    assert result.is_shared_with_other_environment is False


# --- (d) batched directory-identity enumeration: reclaim.preflight.enumerate_directory_identity -
#
# P0-K1a M1 cost-budget fix: `executor._live_subtree_records` used to call `os.stat()` once per
# entry (17-28s added to a real apply against the worst-case real fixture -- PLAN.md's 2026-08-21
# checkpoint). `enumerate_directory_identity` replaces that with one open directory handle and a
# batched `GetFileInformationByHandleEx`/`FileIdBothDirectoryInfo` read. This is a HARD
# PREREQUISITE test (per this fix's own task brief): if `FileId` doesn't match `os.stat().st_ino`
# for the same real file, this whole approach is unsound and must not be relied on anywhere.


def test_batch_enumerated_file_id_matches_os_stat_ino_for_real_files(tmp_path: Path) -> None:
    """Real files (not mocked) -- `enumerate_directory_identity`'s `FileId` must exactly equal
    `os.stat(path, follow_symlinks=False).st_ino` for every one of them, and `is_dir`/attributes
    must agree with `os.stat`'s own `st_file_attributes`. A wrong struct offset would read
    garbage, not a plausible-looking wrong inode -- so an exact match across several real files
    (not just one) is real evidence the `_FILE_ID_BOTH_DIR_INFO` layout is correct, not a
    coincidence."""
    names = ["a.txt", "b.bin", "a_much_longer_filename_to_perturb_struct_offsets.dat"]
    for i, name in enumerate(names):
        (tmp_path / name).write_bytes(f"content-{i}".encode() * (i + 1))
    nested_dir = tmp_path / "nested_subdir"
    nested_dir.mkdir()

    entries = enumerate_directory_identity(long_path(tmp_path))
    assert entries is not None
    by_name = {entry.name: entry for entry in entries}
    assert set(by_name) == {*names, "nested_subdir"}

    for name in names:
        live_path = tmp_path / name
        st = os.stat(live_path, follow_symlinks=False)  # noqa: PTH116 -- comparing against the
        # exact call this fix replaces, not a Path-vs-str style choice.
        entry = by_name[name]
        assert entry.ino == st.st_ino, f"{name}: FileId {entry.ino} != st_ino {st.st_ino}"
        assert entry.is_dir is False
        assert entry.size_bytes == st.st_size

    dir_entry = by_name["nested_subdir"]
    dir_st = os.stat(nested_dir, follow_symlinks=False)  # noqa: PTH116
    assert dir_entry.ino == dir_st.st_ino
    assert dir_entry.is_dir is True


def test_batch_enumerated_directory_dot_and_dotdot_are_filtered(tmp_path: Path) -> None:
    (tmp_path / "only_child.txt").write_bytes(b"x")
    entries = enumerate_directory_identity(long_path(tmp_path))
    assert entries is not None
    assert "." not in {e.name for e in entries}
    assert ".." not in {e.name for e in entries}


def test_batch_enumerated_directory_empty_returns_empty_list(tmp_path: Path) -> None:
    empty_dir = tmp_path / "genuinely_empty"
    empty_dir.mkdir()
    entries = enumerate_directory_identity(long_path(empty_dir))
    assert entries == []  # NOT None -- the directory opened fine, it just has nothing in it


def test_batch_enumerated_directory_missing_path_returns_none(tmp_path: Path) -> None:
    assert enumerate_directory_identity(long_path(tmp_path / "does-not-exist")) is None


def test_batch_enumerated_directory_many_entries_spans_multiple_buffer_reads(
    tmp_path: Path,
) -> None:
    """`_DIR_ENUM_INITIAL_BUFFER_BYTES` (64KB) comfortably fits a few hundred short-named
    entries in one `GetFileInformationByHandleEx` call -- this creates enough real files that a
    single read cannot possibly hold them all, exercising the multi-call pagination loop for
    real rather than only ever hitting the single-call happy path."""
    file_count = 2000
    for i in range(file_count):
        (tmp_path / f"file_{i:05d}.bin").write_bytes(b"x")

    entries = enumerate_directory_identity(long_path(tmp_path))
    assert entries is not None
    assert len(entries) == file_count
    names = {e.name for e in entries}
    assert names == {f"file_{i:05d}.bin" for i in range(file_count)}
    # Every entry's identity is independently real, not a byproduct of pagination miscounting --
    # spot-check a real `os.stat` on a sample spread across the run (first, middle, last).
    for i in (0, file_count // 2, file_count - 1):
        target = tmp_path / f"file_{i:05d}.bin"
        st = os.stat(target, follow_symlinks=False)  # noqa: PTH116
        matching = next(e for e in entries if e.name == target.name)
        assert matching.ino == st.st_ino


# --- Integration: apply_batch skips instead of attempting the mutation -------------------------


def test_apply_batch_skips_file_in_use_and_continues_the_rest_of_the_batch(
    tmp_path: Path,
) -> None:
    """Reproduces the live case at the `apply_batch` level: a locked candidate must be skipped
    (not attempted, not written to the manifest as an aborted intent) while a SECOND, unrelated
    candidate in the same batch is still applied normally -- the existing "abort the item, not
    the run" shape, extended to a pre-flight skip rather than a caught mutation failure."""
    locked_target = tmp_path / "locked.bin"
    locked_target.write_bytes(b"locked-content")
    normal_target = tmp_path / "normal.bin"
    normal_target.write_bytes(b"normal-content")

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"

    handle = open(locked_target, "r+b")  # noqa: SIM115, PTH123
    try:
        report = apply_batch(
            [_candidate(locked_target, size_bytes=14), _candidate(normal_target, size_bytes=14)],
            safety=_safety(),
            apply=True,
            method="vault",
            vault_dir=vault_dir,
            manifest_path=manifest_path,
            now=_NOW,
        )
    finally:
        handle.close()

    locked_result = _result_for(report.items, locked_target)
    normal_result = _result_for(report.items, normal_target)

    assert locked_result.succeeded is False
    assert locked_result.skip_reason == "file_in_use"
    assert locked_result.error is None  # never attempted -- not an OS error
    assert locked_target.exists()  # untouched: still at its original location

    assert normal_result.succeeded is True
    assert normal_result.skip_reason is None
    assert not normal_target.exists()  # genuinely vaulted -- the batch was not aborted

    assert report.files_processed == 2
    assert report.files_succeeded == 1
    assert report.files_failed == 1


def test_apply_batch_skips_hardlink_shared_active_install_and_continues_the_rest_of_the_batch(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "uv-cache"
    cache_root.mkdir()
    cache_file = cache_root / "six.py"
    cache_file.write_bytes(b"stdlib-ish-module-content")

    venv = _make_venv(tmp_path / "project" / ".venv")
    site_packages = venv / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    os.link(cache_file, site_packages / "six.py")

    normal_target = tmp_path / "normal.bin"
    normal_target.write_bytes(b"normal-content")

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"

    report = apply_batch(
        [_candidate(cache_file, size_bytes=26), _candidate(normal_target, size_bytes=14)],
        safety=_safety(),
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=_NOW,
    )

    cache_result = _result_for(report.items, cache_file)
    normal_result = _result_for(report.items, normal_target)

    assert cache_result.succeeded is False
    assert cache_result.skip_reason == "hardlink_shared_active_install"
    assert cache_result.error is None
    assert cache_file.exists()  # untouched -- the hardlink group is completely intact
    assert (site_packages / "six.py").exists()

    assert normal_result.succeeded is True
    assert normal_result.skip_reason is None
    assert not normal_target.exists()

    assert report.files_processed == 2
    assert report.files_succeeded == 1
    assert report.files_failed == 1


def test_apply_batch_dry_run_never_probes_preflight_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run's own documented invariant ("makes zero mutating filesystem calls") is preserved:
    the pre-flight probes only run when `apply=True`, so a dry-run preview never pays their real
    filesystem-probe cost and never skips an item that would otherwise show as "would succeed"."""
    import reclaim.executor as executor_module

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("dry-run must never call a pre-flight probe")

    monkeypatch.setattr(executor_module, "check_file_in_use", _boom)
    monkeypatch.setattr(executor_module, "check_hardlink_shared_active_install", _boom)

    target = tmp_path / "file.bin"
    target.write_bytes(b"content")

    report = apply_batch(
        [_candidate(target, size_bytes=7)],
        safety=_safety(),
        apply=False,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
    )
    assert report.files_succeeded == 1
    assert report.items[0].skip_reason is None
