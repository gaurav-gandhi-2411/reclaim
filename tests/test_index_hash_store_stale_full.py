"""`store_partial_hashes` / `store_full_hashes` must never leave a hash computed against an OLD
(size, mtime) looking valid for the NEW one (the two columns share one hash_size/hash_mtime key)."""

from __future__ import annotations

from pathlib import Path

from reclaim.index import ScanIndex
from reclaim.models import FileRecord

_SIZE = 200_000


def _record(path: Path) -> FileRecord:
    return FileRecord(
        path=path,
        is_dir=False,
        size_bytes=_SIZE,
        attributes=0,
        ext=".bin",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=100.0,
        ctime=100.0,
        dev=1,
        ino=1,
    )


def test_interrupted_run_cannot_leave_a_stale_full_hash_valid_for_changed_content(
    tmp_path: Path,
) -> None:
    """Teeth: a partial hash re-stamped for a NEW (size, mtime) must drop the OLD full hash, or
    the next load would accept it for the new content."""
    f = tmp_path / "f.bin"
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records([_record(f)], scanned_at=1000.0)
        index.store_partial_hashes([(f, _SIZE, 100.0, "p-old")])
        index.store_full_hashes([(f, _SIZE, 100.0, "full-old")])
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.partial_hash, entry.full_hash) == ("p-old", "full-old")
        index.store_partial_hashes([(f, _SIZE, 200.0, "p-new")])  # run interrupted before full
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.hash_mtime, entry.partial_hash, entry.full_hash) == (200.0, "p-new", None)
        index.store_full_hashes([(f, _SIZE, 300.0, "full-newer")])
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.hash_mtime, entry.partial_hash, entry.full_hash) == (
            300.0,
            None,
            "full-newer",
        )


def test_same_key_keeps_the_sibling_hash(tmp_path: Path) -> None:
    """The other half of the rule: a store against the SAME (size, mtime) keeps the sibling, so a
    normal partial-then-full run still ends with both hashes cached."""
    f = tmp_path / "f.bin"
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records([_record(f)], scanned_at=1000.0)
        index.store_partial_hashes([(f, _SIZE, 100.0, "p")])
        index.store_full_hashes([(f, _SIZE, 100.0, "full")])
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.partial_hash, entry.full_hash) == ("p", "full")
        index.store_partial_hashes([(f, _SIZE, 100.0, "p")])  # idempotent re-store
        entry = index.load_hash_cache()[f.as_posix()]
        assert (entry.partial_hash, entry.full_hash) == ("p", "full")
