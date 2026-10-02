"""perf/dedup-warmup: the parallel, hardlink-aware, window-batched hash pipeline must produce the
same duplicate clusters as a brute-force oracle, never treat a partial match as a duplicate, and
reuse (but never wrongly trust) persisted hashes across a restart."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from pathlib import Path

import pytest

import reclaim.dedup as dedup_module
from reclaim.dedup import _PARTIAL_HASH_CHUNK_BYTES, find_duplicate_clusters
from reclaim.index import ScanIndex
from reclaim.models import FileRecord, HashSkip

_BIG = 3 * _PARTIAL_HASH_CHUNK_BYTES  # > the whole-file threshold, so a real partial stage runs


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _real_record(path: Path) -> FileRecord:
    st = path.stat()
    return FileRecord(
        path=path,
        is_dir=False,
        size_bytes=st.st_size,
        attributes=0,
        ext=path.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=st.st_mtime,
        ctime=st.st_ctime,
        dev=st.st_dev,
        ino=st.st_ino,
    )


def _index(tmp_path: Path, files: list[Path]) -> Path:
    db = tmp_path / "index.sqlite3"
    with ScanIndex(db) as index:
        index.upsert_records([_real_record(f) for f in files], scanned_at=1000.0)
    return db


def _oracle(files: list[Path]) -> set[frozenset[Path]]:
    """Independent brute force: whole-file SHA-256 within same-size groups, no cache, no stages."""
    groups: dict[tuple[int, str], set[Path]] = defaultdict(set)
    for f in files:
        data = f.read_bytes()
        if data:
            groups[(len(data), hashlib.sha256(data).hexdigest())].add(f)
    return {frozenset(g) for g in groups.values() if len(g) >= 2}


def _as_sets(clusters: list) -> set[frozenset[Path]]:
    return {frozenset({c.keep.path, *(d.path for d in c.duplicates)}) for c in clusters}


def _count_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"partial": 0, "full": 0}
    real_partial, real_full = dedup_module._compute_partial_hash, dedup_module._compute_full_hash

    def partial(path: Path, size: int) -> str:
        calls["partial"] += 1
        return real_partial(path, size)

    def full(path: Path) -> str:
        calls["full"] += 1
        return real_full(path)

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", partial)
    monkeypatch.setattr(dedup_module, "_compute_full_hash", full)
    return calls


def _fixture_tree(root: Path) -> list[Path]:
    files = [
        _write(root / "a" / "big1.bin", b"X" * _BIG),
        _write(root / "b" / "big2.bin", b"X" * _BIG),
        _write(root / "c" / "big3.bin", b"X" * _BIG),
        # Same size, same head and tail, different middle: a partial match that is NOT a duplicate.
        _write(root / "d" / "mid1.bin", b"H" * 70000 + b"1" * 60000 + b"T" * 70000),
        _write(root / "d" / "mid2.bin", b"H" * 70000 + b"2" * 60000 + b"T" * 70000),
        _write(root / "e" / "small1.bin", b"s" * 5000),
        _write(root / "e" / "small2.bin", b"s" * 5000),
        _write(root / "e" / "small3.bin", b"t" * 5000),  # same size, different content
        _write(root / "f" / "empty1.bin", b""),
        _write(root / "f" / "empty2.bin", b""),
        _write(root / "g" / "unique.bin", b"u" * 1234),
    ]
    return files


def test_clusters_equal_bruteforce_oracle(tmp_path: Path) -> None:
    files = _fixture_tree(tmp_path / "tree")
    db = _index(tmp_path, files)
    with ScanIndex(db) as index:
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)
    assert _as_sets(clusters) == _oracle(files)
    assert len(clusters) == 2  # the three big copies, and the two small ones


def test_partial_match_alone_is_never_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        _write(tmp_path / "mid1.bin", b"H" * 70000 + b"1" * 60000 + b"T" * 70000),
        _write(tmp_path / "mid2.bin", b"H" * 70000 + b"2" * 60000 + b"T" * 70000),
    ]
    db = _index(tmp_path, files)
    calls = _count_calls(monkeypatch)
    with ScanIndex(db) as index:
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)
    assert clusters == []
    assert calls == {"partial": 2, "full": 2}  # the full read is what rejected them


def test_small_files_are_not_read_twice_and_still_persist_a_full_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [_write(tmp_path / "s1.bin", b"s" * 50000), _write(tmp_path / "s2.bin", b"s" * 50000)]
    db = _index(tmp_path, files)
    calls = _count_calls(monkeypatch)
    with ScanIndex(db) as index:
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)
        cache = index.load_hash_cache()
    assert len(clusters) == 1
    assert calls == {"partial": 2, "full": 0}
    assert clusters[0].full_hash == cache[files[0].as_posix()].full_hash
    assert cache[files[0].as_posix()].full_hash == cache[files[0].as_posix()].partial_hash
    assert clusters[0].full_hash == dedup_module._compute_full_hash(files[0])


@pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")
def test_hardlink_siblings_are_hashed_once_but_all_stay_in_the_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write(tmp_path / "orig.bin", b"Z" * _BIG)
    link = tmp_path / "link.bin"
    os.link(original, link)
    copy = _write(tmp_path / "copy.bin", b"Z" * _BIG)
    files = [original, link, copy]
    db = _index(tmp_path, files)
    calls = _count_calls(monkeypatch)
    with ScanIndex(db) as index:
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)
        cache = index.load_hash_cache()
    assert _as_sets(clusters) == {frozenset(files)} == _oracle(files)
    assert calls == {"partial": 2, "full": 2}  # 2 distinct inodes, not 3 files
    assert all(cache[f.as_posix()].full_hash is not None for f in files)  # sibling persisted too


@pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")
def test_unreadable_hardlink_group_records_a_skip_per_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write(tmp_path / "orig.bin", b"Z" * 1000)
    link = tmp_path / "link.bin"
    os.link(original, link)
    other = _write(tmp_path / "other.bin", b"Z" * 1000)
    db = _index(tmp_path, [original, link, other])

    def locked(path: Path, size: int) -> str:
        if path in (original, link):
            raise PermissionError("[WinError 32] in use")
        return "d"

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", locked)
    skips: list[HashSkip] = []
    with ScanIndex(db) as index:
        assert find_duplicate_clusters(index, min_reclaim_bytes=0, skips=skips) == []
    assert {s.path for s in skips} == {original, link}


def test_restart_reuses_persisted_hashes_and_returns_identical_clusters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _fixture_tree(tmp_path / "tree")
    db = _index(tmp_path, files)
    with ScanIndex(db) as index:
        first = find_duplicate_clusters(index, min_reclaim_bytes=0)
    calls = _count_calls(monkeypatch)
    with ScanIndex(db) as index:  # a fresh connection == a process restart
        second = find_duplicate_clusters(index, min_reclaim_bytes=0)
    assert calls == {"partial": 0, "full": 0}
    assert [(c.full_hash, c.keep.path, c.duplicates) for c in second] == [
        (c.full_hash, c.keep.path, c.duplicates) for c in first
    ]


def test_changed_file_is_rehashed_not_served_from_a_stale_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [_write(tmp_path / "x1.bin", b"A" * _BIG), _write(tmp_path / "x2.bin", b"A" * _BIG)]
    db = _index(tmp_path, files)
    with ScanIndex(db) as index:
        assert len(find_duplicate_clusters(index, min_reclaim_bytes=0)) == 1
    # Same size, new content, new mtime -- the rescan updates the row's size/mtime.
    files[1].write_bytes(b"B" * _BIG)
    os.utime(files[1], (1_800_000_000, 1_800_000_000))
    with ScanIndex(db) as index:
        index.upsert_records([_real_record(files[1])], scanned_at=2000.0)
    calls = _count_calls(monkeypatch)
    with ScanIndex(db) as index:
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)
    assert clusters == []
    assert calls["partial"] == 1  # only the changed file; the untouched one hit the cache


def test_interrupted_run_cannot_leave_a_stale_full_hash_valid_for_changed_content(
    tmp_path: Path,
) -> None:
    """Teeth for the `store_*_hashes` fix: a partial hash re-stamped for a NEW (size, mtime)
    must drop the OLD full hash, or the next load would accept it for the new content."""
    f = _write(tmp_path / "f.bin", b"A" * _BIG)
    db = _index(tmp_path, [f])
    with ScanIndex(db) as index:
        index.store_partial_hashes([(f, _BIG, 100.0, "p-old")])
        index.store_full_hashes([(f, _BIG, 100.0, "full-old")])
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.partial_hash, entry.full_hash) == ("p-old", "full-old")
        index.store_partial_hashes([(f, _BIG, 200.0, "p-new")])  # run interrupted before full
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.hash_mtime, entry.partial_hash, entry.full_hash) == (200.0, "p-new", None)
        index.store_full_hashes([(f, _BIG, 300.0, "full-newer")])
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.hash_mtime, entry.partial_hash, entry.full_hash) == (
            300.0,
            None,
            "full-newer",
        )


@pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")
def test_stale_hardlink_name_is_skipped_like_before_not_borrowed_from_its_sibling(
    tmp_path: Path,
) -> None:
    """The index can outlive a file: a sibling name deleted since the scan must be reported
    unreadable exactly as it was when every name was hashed separately -- never admitted to a
    cluster on the strength of its sibling's hash."""
    original = _write(tmp_path / "orig.bin", b"Z" * _BIG)
    link = tmp_path / "link.bin"
    os.link(original, link)
    copy = _write(tmp_path / "copy.bin", b"Z" * _BIG)
    db = _index(tmp_path, [original, link, copy])
    link.unlink()
    skips: list[HashSkip] = []
    with ScanIndex(db) as index:
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0, skips=skips)
    assert _as_sets(clusters) == {frozenset({original, copy})}
    assert [(s.path, s.stage) for s in skips] == [(link, "partial")]


@pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")
def test_unreadable_first_name_does_not_hide_its_readable_hardlink_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _write(tmp_path / "a_orig.bin", b"Z" * 1000)
    link = tmp_path / "b_link.bin"
    os.link(original, link)
    copy = _write(tmp_path / "c_copy.bin", b"Z" * 1000)
    db = _index(tmp_path, [original, link, copy])
    real = dedup_module._compute_partial_hash

    def first_name_locked(path: Path, size: int) -> str:
        if path == original:
            raise PermissionError("[WinError 32] in use")
        return real(path, size)

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", first_name_locked)
    skips: list[HashSkip] = []
    with ScanIndex(db) as index:
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0, skips=skips)
    assert _as_sets(clusters) == {frozenset({link, copy})}
    assert [s.path for s in skips] == [original]


def test_environment_root_cache_gives_identical_answers_with_one_probe_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-clustering eligibility used to re-probe every ancestor for every member (91% of the
    candidate-generation time on a real index); a per-pass cache must not change a single answer."""
    venv = tmp_path / "proj" / ".venv"
    (venv).mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x")
    files = [venv / "Lib" / "site-packages" / "pkg" / f"m{i}.py" for i in range(5)]
    files.append(tmp_path / "proj" / "src" / "plain.py")
    probes: list[Path] = []
    real = dedup_module._has_site_packages
    monkeypatch.setattr(
        dedup_module, "_has_site_packages", lambda d: (probes.append(d), real(d))[1]
    )
    uncached = [dedup_module._environment_root(f) for f in files]
    uncached_probes = len(probes)
    probes.clear()
    cache: dict[Path, bool] = {}
    cached = [dedup_module._environment_root(f, cache) for f in files]
    assert cached == uncached
    assert uncached[0] == venv
    assert len(probes) < uncached_probes
    assert len(probes) == len(set(probes))  # each directory probed at most once
