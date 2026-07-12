from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from reclaim.index import ScanIndex
from reclaim.models import FileRecord

_SECONDS_PER_DAY = 86400.0
# Above dedup.py's whole-file-vs-chunked partial-hash threshold (128KB) so the near-duplicate
# fixture actually exercises the two-chunk read path, not the whole-file shortcut.
_NEAR_DUP_SIZE_BYTES = 200_000
_NEAR_DUP_CHUNK_BYTES = 64 * 1024


def _random_bytes(n: int, seed: int) -> bytes:
    # Deterministic fixture content, not security-sensitive.
    return random.Random(seed).randbytes(n)  # noqa: S311


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@dataclass(frozen=True, slots=True)
class DedupFixtureTree:
    """Key paths from the materialized Stage 4 dedup fixture tree, for eval assertions."""

    root: Path
    dup_pair_a: Path
    dup_pair_b: Path
    dup_triple_a: Path
    dup_triple_b: Path
    dup_triple_c: Path
    near_dup_a: Path
    near_dup_b: Path
    same_size_diff_a: Path
    same_size_diff_b: Path
    unique_1: Path
    unique_2: Path
    downloads_copy: Path
    nondownloads_copy: Path
    ctime_older: Path
    ctime_newer: Path
    depth_shallow: Path
    depth_deep: Path
    blocked_keep: Path
    blocked_duplicate: Path


def build_dedup_fixture_tree(root: Path, index: ScanIndex, *, now: float) -> DedupFixtureTree:
    """Materializes a real filesystem tree under `root` (real byte content, for the ground-
    truth precision proof) and indexes it directly via `index.upsert_records` rather than
    `scan_tree` — every FileRecord's size/mtime/ctime is set explicitly here so the
    keep-heuristic's Downloads/Temp, ctime, and depth cases are fully deterministic, instead of
    depending on real OS file-creation-time ordering (which this suite has no reliable way to
    control across CI runners). The bytes on disk are always real and independently readable,
    which is what the precision assertion in evals/test_dedup.py actually needs to be true.

    Never touches anything outside `root`.
    """
    records: list[FileRecord] = []

    def add(path: Path, content: bytes, *, ctime: float) -> Path:
        _write(path, content)
        records.append(
            FileRecord(
                path=path,
                is_dir=False,
                size_bytes=len(content),
                attributes=0,
                ext=path.suffix.lower(),
                git_repo_root=None,
                git_repo_clean=False,
                mtime=ctime,
                ctime=ctime,
            )
        )
        return path

    dup_content = _random_bytes(500, seed=1)
    dup_pair_a = add(root / "Documents" / "report.docx", dup_content, ctime=now)
    dup_pair_b = add(root / "Backup" / "report_copy.docx", dup_content, ctime=now)

    triple_content = _random_bytes(300, seed=2)
    dup_triple_a = add(root / "Photos" / "img.jpg", triple_content, ctime=now)
    dup_triple_b = add(root / "Photos" / "Album1" / "img.jpg", triple_content, ctime=now)
    dup_triple_c = add(root / "Photos" / "Album2" / "img.jpg", triple_content, ctime=now)

    # Same size, identical first/last 64KB, different middle bytes: must NOT cluster (the case
    # that actually exercises the full-hash disambiguation step).
    first_chunk = _random_bytes(_NEAR_DUP_CHUNK_BYTES, seed=10)
    last_chunk = _random_bytes(_NEAR_DUP_CHUNK_BYTES, seed=11)
    middle_len = _NEAR_DUP_SIZE_BYTES - 2 * _NEAR_DUP_CHUNK_BYTES
    middle_a = _random_bytes(middle_len, seed=12)
    middle_b = _random_bytes(middle_len, seed=13)
    near_dup_a = add(
        root / "Videos" / "clip_v1.bin", first_chunk + middle_a + last_chunk, ctime=now
    )
    near_dup_b = add(
        root / "Videos" / "clip_v2.bin", first_chunk + middle_b + last_chunk, ctime=now
    )

    # Simpler negative case: same size, but different everywhere (won't even survive the
    # partial-hash grouping step).
    same_size_diff_a = add(root / "Misc" / "data_a.bin", _random_bytes(700, seed=20), ctime=now)
    same_size_diff_b = add(root / "Misc" / "data_b.bin", _random_bytes(700, seed=21), ctime=now)

    unique_1 = add(root / "Misc" / "unique_1.bin", _random_bytes(111, seed=40), ctime=now)
    unique_2 = add(root / "Misc" / "unique_2.bin", _random_bytes(222, seed=41), ctime=now)

    # Keep-heuristic rule 1: location beats ctime. Downloads copy is *older* (by ctime) than
    # the non-Downloads copy, yet the non-Downloads copy must still be the one kept.
    downloads_content = _random_bytes(400, seed=30)
    downloads_copy = add(
        root / "Downloads" / "install_notes.txt",
        downloads_content,
        ctime=now - 100 * _SECONDS_PER_DAY,
    )
    nondownloads_copy = add(
        root / "Documents" / "install_notes.txt",
        downloads_content,
        ctime=now - 1 * _SECONDS_PER_DAY,
    )

    # Keep-heuristic rule 2: ctime tiebreak when location ties (both outside Downloads/Temp).
    ctime_content = _random_bytes(450, seed=31)
    ctime_older = add(
        root / "Archive" / "old_notes.txt", ctime_content, ctime=now - 50 * _SECONDS_PER_DAY
    )
    ctime_newer = add(
        root / "Archive" / "new_notes.txt", ctime_content, ctime=now - 1 * _SECONDS_PER_DAY
    )

    # Keep-heuristic rule 3: depth tiebreak when location and ctime both tie.
    depth_content = _random_bytes(480, seed=32)
    depth_shallow = add(root / "Shallow" / "notes.txt", depth_content, ctime=now)
    depth_deep = add(root / "Shallow" / "nested" / "deeper" / "notes.txt", depth_content, ctime=now)

    # SafetyValidator exclusion: one member sits under the fixture's protected "Windows" root
    # (see build_dedup_tree's caller for the fixture-relative protected_roots config) and must
    # be excluded from candidate output even though clustering finds it.
    blocked_content = _random_bytes(350, seed=33)
    blocked_keep = add(root / "Documents" / "config.ini", blocked_content, ctime=now)
    blocked_duplicate = add(
        root / "Windows" / "System32" / "config.ini", blocked_content, ctime=now
    )

    index.upsert_records(records, scanned_at=now)

    return DedupFixtureTree(
        root=root,
        dup_pair_a=dup_pair_a,
        dup_pair_b=dup_pair_b,
        dup_triple_a=dup_triple_a,
        dup_triple_b=dup_triple_b,
        dup_triple_c=dup_triple_c,
        near_dup_a=near_dup_a,
        near_dup_b=near_dup_b,
        same_size_diff_a=same_size_diff_a,
        same_size_diff_b=same_size_diff_b,
        unique_1=unique_1,
        unique_2=unique_2,
        downloads_copy=downloads_copy,
        nondownloads_copy=nondownloads_copy,
        ctime_older=ctime_older,
        ctime_newer=ctime_newer,
        depth_shallow=depth_shallow,
        depth_deep=depth_deep,
        blocked_keep=blocked_keep,
        blocked_duplicate=blocked_duplicate,
    )
