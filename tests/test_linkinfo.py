from __future__ import annotations

import os
from pathlib import Path

import pytest

from reclaim.linkinfo import estimate_reclaimable_bytes, read_link_identity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")


def _make_hardlink_group(tmp_path: Path, *, count: int, content: bytes) -> list[Path]:
    """Real hardlinks (via `os.link`, not a mock) — `count` distinct names all pointing to the
    same inode."""
    first = tmp_path / "member_0.bin"
    first.write_bytes(content)
    paths = [first]
    for i in range(1, count):
        linked = tmp_path / f"member_{i}.bin"
        os.link(first, linked)
        paths.append(linked)
    return paths


def test_read_link_identity_reflects_real_nlink(tmp_path: Path) -> None:
    standalone = tmp_path / "standalone.bin"
    standalone.write_bytes(b"x" * 50)
    identity = read_link_identity(standalone)
    assert identity is not None
    assert identity.nlink == 1

    group = _make_hardlink_group(tmp_path, count=3, content=b"y" * 50)
    for path in group:
        info = read_link_identity(path)
        assert info is not None
        assert info.nlink == 3
        assert info.dev == read_link_identity(group[0]).dev  # type: ignore[union-attr]
        assert info.ino == read_link_identity(group[0]).ino  # type: ignore[union-attr]


def test_read_link_identity_returns_none_for_vanished_path(tmp_path: Path) -> None:
    assert read_link_identity(tmp_path / "does_not_exist.bin") is None


def test_standalone_file_reclaimable_equals_logical_size(tmp_path: Path) -> None:
    target = tmp_path / "standalone.bin"
    target.write_bytes(b"x" * 100)

    estimates = estimate_reclaimable_bytes([(target, 100)])
    assert estimates[target].logical_bytes == 100
    assert estimates[target].reclaimable_bytes == 100
    assert estimates[target].resolved is True


def test_hardlink_group_fully_in_candidate_set_counts_blocks_once(tmp_path: Path) -> None:
    """The exact required scenario: a hardlink group (3 paths, 1 inode) where ALL THREE are
    being deleted together — the shared blocks are only freed once every name is gone, so the
    total reclaimable across all three must equal ONE file's size, not three times it."""
    group = _make_hardlink_group(tmp_path, count=3, content=b"z" * 200)
    candidates = [(path, 200) for path in group]

    estimates = estimate_reclaimable_bytes(candidates)

    assert sum(e.reclaimable_bytes for e in estimates.values()) == 200  # counted once, not 600
    assert sum(e.logical_bytes for e in estimates.values()) == 600  # logical size still 3x
    non_zero = [path for path, e in estimates.items() if e.reclaimable_bytes > 0]
    assert len(non_zero) == 1  # exactly one member credited with the reclaim


def test_hardlink_group_partially_in_candidate_set_is_zero_reclaimable(tmp_path: Path) -> None:
    """A hardlink group where only SOME names are candidates (one name — the "kept" copy in
    dedup terms — lives outside the candidate set): deleting the candidates alone never frees
    the shared blocks, since the external name still holds them open. This is the general
    mechanism behind "a duplicate that's actually a hardlink to the kept copy reports 0
    reclaimable" (ADR-0006)."""
    group = _make_hardlink_group(tmp_path, count=3, content=b"w" * 150)
    kept, *candidates_only = group  # kept is deliberately excluded from the candidate set
    candidates = [(path, 150) for path in candidates_only]

    estimates = estimate_reclaimable_bytes(candidates)

    assert all(e.reclaimable_bytes == 0 for e in estimates.values())
    assert all(e.logical_bytes == 150 for e in estimates.values())
    assert kept.exists()  # sanity: the excluded member is untouched by this pure computation


def test_unresolvable_path_is_conservatively_zero_reclaimable(tmp_path: Path) -> None:
    missing = tmp_path / "vanished.bin"
    estimates = estimate_reclaimable_bytes([(missing, 999)])
    assert estimates[missing].logical_bytes == 999
    assert estimates[missing].reclaimable_bytes == 0
    assert estimates[missing].resolved is False


def test_reparse_point_reporting_nlink_one_is_conservatively_zero_reclaimable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0008: a reparse point that looks standalone by `st_nlink` (`nlink == 1`) must never
    be confidently reported as fully reclaimable — `st_nlink` alone cannot see storage-sharing
    mechanisms some reparse points implement (e.g. Windows Data Deduplication's chunk store),
    unlike a real hardlink group, which `st_nlink` reports correctly. Exercised via monkeypatch
    rather than a real reparse point: creating one needs elevated privilege or Developer Mode,
    which this project's own real-disk investigation (ADR-0008) found unavailable on the dev
    machine — the same constraint would make a real-reparse-point version of this test
    unreliable wherever it runs."""
    target = tmp_path / "reparse_like.bin"
    target.write_bytes(b"r" * 80)

    from reclaim import linkinfo

    real_identity = linkinfo.read_link_identity(target)
    assert real_identity is not None
    reparse_identity = linkinfo.LinkIdentity(
        dev=real_identity.dev, ino=real_identity.ino, nlink=1, is_reparse_point=True
    )
    monkeypatch.setattr(linkinfo, "read_link_identity", lambda path: reparse_identity)

    estimates = estimate_reclaimable_bytes([(target, 80)])
    assert estimates[target].reclaimable_bytes == 0
    assert estimates[target].resolved is False


def test_mixed_standalone_and_hardlinked_candidates(tmp_path: Path) -> None:
    """A realistic candidate set: one ordinary standalone file plus a fully-covered hardlink
    pair, in the same `estimate_reclaimable_bytes` call — each group resolved independently."""
    standalone = tmp_path / "standalone.bin"
    standalone.write_bytes(b"a" * 40)
    pair = _make_hardlink_group(tmp_path, count=2, content=b"b" * 60)

    candidates = [(standalone, 40), (pair[0], 60), (pair[1], 60)]
    estimates = estimate_reclaimable_bytes(candidates)

    assert estimates[standalone].reclaimable_bytes == 40
    assert sum(estimates[p].reclaimable_bytes for p in pair) == 60
