from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import reclaim.dirlist as dirlist
from reclaim.dirlist import ListingUnsupported, filetime_to_unix_seconds, list_directory
from reclaim.scanner import _suffix_lower, long_path

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Reclaim targets Windows/NTFS exclusively"
)

_FILE_ATTRIBUTE_DIRECTORY = 0x10


def _listed(path: Path) -> tuple[int, dict[str, tuple[int, int, int, int, int, int]]]:
    """name -> (attributes, size, last_write, creation, id_low, id_high)."""
    serial, entries = list_directory(long_path(path))
    by_name = {e[0]: e[1:] for e in entries}
    assert len(by_name) == len(entries), "duplicate names in a listing"
    return serial, by_name


def test_listing_matches_scandir_names_and_stat_fields_for_plain_files(tmp_path: Path) -> None:
    """The whole point of the module: every field the scanner stores for a plain file must
    equal `os.stat()`'s, so a listing-sourced `FileRecord` is indistinguishable from the old one.
    Includes an empty file, a dotfile, unicode and space-bearing names."""
    names = ["a.txt", "empty", ".hidden", "naïve café.TXT", "with space.tar.gz", "日本語.md"]
    for i, name in enumerate(names):
        (tmp_path / name).write_bytes(b"x" * (i * 1000))
    (tmp_path / "sub").mkdir()

    serial, listed = _listed(tmp_path)

    assert set(listed) == {e.name for e in os.scandir(tmp_path)}
    for name in names:
        st = (tmp_path / name).lstat()
        attributes, size, last_write, creation, id_low, id_high = listed[name]
        assert size == st.st_size
        assert attributes == st.st_file_attributes
        assert filetime_to_unix_seconds(last_write) == st.st_mtime
        assert filetime_to_unix_seconds(creation) == st.st_ctime
        assert id_low | (id_high << 64) == st.st_ino
        assert serial == st.st_dev


def test_directory_listing_entry_is_not_a_substitute_for_stat(tmp_path: Path) -> None:
    """Pins why the scanner `os.stat()`s directories itself: a directory's listing size is NOT
    its `os.stat().st_size` (0 vs its index allocation size on NTFS), and a directory's listed
    mtime can lag its live one (NTFS refreshes a directory's entry in its parent lazily) -- so
    the listing is only trusted for a directory's identity fields."""
    sub = tmp_path / "sub"
    sub.mkdir()
    for i in range(300):  # enough entries that NTFS gives the directory an index allocation
        (sub / f"file_with_a_reasonably_long_name_{i:04d}.dat").write_bytes(b"")

    serial, listed = _listed(tmp_path)

    st = sub.lstat()
    attributes, listed_size, _last_write, _creation, id_low, id_high = listed["sub"]
    assert attributes & _FILE_ATTRIBUTE_DIRECTORY
    assert id_low | (id_high << 64) == st.st_ino
    assert serial == st.st_dev
    assert listed_size != st.st_size, "if these ever agree, the directory-stat exception is moot"


def test_hardlinked_names_report_the_same_file_id(tmp_path: Path) -> None:
    """Hardlink identity is what dedup/`check_hardlink_shared_active_install` rest on: two names
    of one file must list with identical (volume, file id)."""
    original = tmp_path / "original.bin"
    original.write_bytes(b"shared bytes")
    os.link(original, tmp_path / "alias.bin")

    serial, listed = _listed(tmp_path)

    assert listed["original.bin"][4:] == listed["alias.bin"][4:]
    assert listed["original.bin"][4] != 0
    assert serial == original.stat().st_dev


@pytest.mark.parametrize(
    "mtime_ns",
    [
        1_700_000_000_000_000_000,
        1_700_000_000_123_456_700,  # sub-second, 100ns-tick aligned
        1_234_567_890_999_999_900,
        946_684_800_000_000_100,  # 2000-01-01 + one tick
        4_000_000_000_000_000_000,  # past 2038
    ],
)
def test_filetime_conversion_is_bit_identical_to_os_stat(tmp_path: Path, mtime_ns: int) -> None:
    """The index compares mtimes for equality (`is_unchanged`): a one-ulp difference between the
    listing-derived float and `os.stat().st_mtime` would flag every file as modified."""
    target = tmp_path / "t.bin"
    target.write_bytes(b"1")
    os.utime(target, ns=(mtime_ns, mtime_ns))

    _serial, listed = _listed(tmp_path)

    assert filetime_to_unix_seconds(listed["t.bin"][2]) == target.stat().st_mtime


def test_listing_spans_multiple_buffer_fills(tmp_path: Path) -> None:
    """More entries than one enumeration buffer holds: the loop must continue until
    ERROR_NO_MORE_FILES and lose nothing at a buffer boundary."""
    names = {f"{'n' * 120}_{i:05d}.dat" for i in range(3000)}  # ~3000 x ~400B > 256KB
    for name in names:
        (tmp_path / name).write_bytes(b"")

    _serial, listed = _listed(tmp_path)

    assert set(listed) == names


def test_empty_directory_lists_nothing(tmp_path: Path) -> None:
    serial, entries = list_directory(long_path(tmp_path))

    assert entries == []
    assert serial == tmp_path.stat().st_dev


def test_listing_works_past_max_path(tmp_path: Path) -> None:
    deep = tmp_path
    for _ in range(12):
        deep = deep / ("d" * 30)
    os.makedirs(long_path(deep))  # noqa: PTH103 -- \\?\ str, not Path
    with open(long_path(deep / "leaf.txt"), "wb") as handle:  # noqa: PTH123
        handle.write(b"leaf")
    assert len(str(deep)) > 260

    _serial, listed = _listed(deep)

    assert set(listed) == {"leaf.txt"}
    assert listed["leaf.txt"][1] == 4


def test_missing_directory_raises_oserror_with_a_scandir_style_message(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    with pytest.raises(OSError, match=r"WinError [23]") as excinfo:
        list_directory(long_path(missing))

    assert not isinstance(excinfo.value, ListingUnsupported)
    assert excinfo.value.winerror in (2, 3)


def test_a_file_path_is_not_listable(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(OSError):
        list_directory(long_path(target))


def test_known_serial_skips_the_volume_probe(tmp_path: Path) -> None:
    """A walk passes the serial it learned from its first directory back in; the answer must be
    echoed unchanged and the listing itself unaffected."""
    (tmp_path / "x").write_bytes(b"")
    real_serial = tmp_path.stat().st_dev

    serial, entries = list_directory(long_path(tmp_path), real_serial)

    assert serial == real_serial
    assert [e[0] for e in entries] == ["x"]


def test_non_ntfs_volume_is_reported_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ReFS/FAT/redirectors must fall back to the legacy path, never be served from a listing
    whose ID/caching semantics were only validated on NTFS."""
    monkeypatch.setitem(dirlist._ntfs_by_volume, tmp_path.stat().st_dev, False)

    with pytest.raises(ListingUnsupported):
        list_directory(long_path(tmp_path))


@pytest.mark.parametrize(
    "name",
    [
        "a.txt",
        "A.TXT",
        "archive.tar.gz",
        ".gitignore",
        ".hidden.ext",
        "noext",
        "trailingdot.",
        "double..dot",
        "..dots",
        "a.b.c.D",
        "x.",
        ".",
        "é.Ünï",
        "file name with spaces.PDF",
        "日本語.TXT",
    ],
)
def test_suffix_lower_matches_pathlib(name: str) -> None:
    """`_suffix_lower` replaces `Path(name).suffix.lower()` on the hot path; it must agree with
    pathlib on this Python for every shape a file name can take."""
    assert _suffix_lower(name) == Path(name).suffix.lower()
