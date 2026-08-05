from __future__ import annotations

import os
from pathlib import Path

import pytest

from reclaim import drives
from reclaim.drives import NoFixedDrivesFoundError, is_network_drive, list_fixed_drives

pytestmark = pytest.mark.skipif(os.name != "nt", reason="drive enumeration is Windows-only")


# --- Real Win32 call -- the CI runner and every real Windows dev machine has at least a C:\
# fixed drive, so this exercises the actual `GetLogicalDrives`/`GetDriveTypeW` calls rather than
# mocking ctypes for the happy path (which would prove nothing about the real Win32 call).


def test_list_fixed_drives_finds_at_least_one_real_fixed_drive() -> None:
    found = list_fixed_drives()

    assert len(found) >= 1
    assert all(isinstance(d, Path) for d in found)
    # Every returned drive must itself be a real, currently-accessible directory -- not merely a
    # bitmask bit that happened to be set.
    assert all(d.is_dir() for d in found)
    # At least one real Windows machine convention: drive letters are single uppercase chars
    # followed by ":\\".
    assert all(len(d.as_posix()) == 3 and d.as_posix()[1] == ":" for d in found)


# --- Edge cases -- monkeypatched, since a real machine with zero fixed drives or with only
# removable/network drives isn't a reliable, reproducible fixture across environments.


def test_list_fixed_drives_raises_when_bitmask_has_no_letters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drives, "_raw_logical_drives_bitmask", lambda: 0)
    with pytest.raises(NoFixedDrivesFoundError):
        list_fixed_drives()


def test_list_fixed_drives_raises_when_every_present_letter_is_non_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bit 0 (A:) and bit 3 (D:) present, both reported as removable (2) -- zero DRIVE_FIXED (3)
    # results even though the bitmask itself is non-empty.
    monkeypatch.setattr(drives, "_raw_logical_drives_bitmask", lambda: (1 << 0) | (1 << 3))
    monkeypatch.setattr(drives, "_raw_drive_type", lambda _drive_root: 2)
    with pytest.raises(NoFixedDrivesFoundError):
        list_fixed_drives()


def test_list_fixed_drives_filters_out_non_fixed_letters(monkeypatch: pytest.MonkeyPatch) -> None:
    # C: (bit 2) is fixed, D: (bit 3) is a CD-ROM -- only C:\\ should be returned.
    monkeypatch.setattr(drives, "_raw_logical_drives_bitmask", lambda: (1 << 2) | (1 << 3))

    def fake_drive_type(drive_root: str) -> int:
        return 3 if drive_root == "C:\\" else 5  # DRIVE_FIXED vs DRIVE_CDROM

    monkeypatch.setattr(drives, "_raw_drive_type", fake_drive_type)

    found = list_fixed_drives()

    assert found == [Path("C:\\")]


# --- is_network_drive (Wave 1 finding #3, risk-targeted stat guard) ----------------------------


def test_is_network_drive_recognizes_unc_paths_without_any_win32_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_drive_root: str) -> int:
        raise AssertionError("UNC recognition must not need a GetDriveTypeW call")

    monkeypatch.setattr(drives, "_raw_drive_type", boom)

    assert is_network_drive(Path("\\\\server\\share\\subdir")) is True


def test_is_network_drive_true_for_a_mapped_network_drive_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drives, "_raw_drive_type", lambda _drive_root: 4)  # DRIVE_REMOTE

    assert is_network_drive(Path("Z:\\some\\path")) is True


def test_is_network_drive_false_for_a_local_fixed_drive_letter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drives, "_raw_drive_type", lambda _drive_root: 3)  # DRIVE_FIXED

    assert is_network_drive(Path("C:\\Users\\gaura")) is False
