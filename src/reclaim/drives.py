from __future__ import annotations

import ctypes
import string
from pathlib import Path

# Win32 GetDriveTypeW return values (winbase.h) -- only DRIVE_FIXED (a real, locally-attached
# hard disk / SSD partition) is ever eligible for the "scan my whole computer" SIMPLE-mode
# full-drive scan (full-drive-scan-eta); every other type (DRIVE_UNKNOWN=0, DRIVE_NO_ROOT_DIR=1,
# DRIVE_REMOVABLE=2, DRIVE_REMOTE=4, DRIVE_CDROM=5, DRIVE_RAMDISK=6) is excluded on purpose -- see
# `list_fixed_drives`'s docstring.
_DRIVE_FIXED = 3


class NoFixedDrivesFoundError(RuntimeError):
    """Raised by `list_fixed_drives` when `GetLogicalDrives`/`GetDriveTypeW` found zero
    `DRIVE_FIXED` volumes on this machine -- see that function's docstring for why this is a
    loud refusal, never a silent empty list."""


def _raw_logical_drives_bitmask() -> int:  # pragma: no cover -- real Win32 call;
    # `list_fixed_drives`'s branches are covered by monkeypatching this + `_raw_drive_type`
    # directly (see test_drives.py's edge-case tests), not by controlling the actual drive
    # layout of the test machine -- same convention as `elevation.py`'s `_raw_is_admin`.
    return int(ctypes.windll.kernel32.GetLogicalDrives())


def _raw_drive_type(drive_root: str) -> int:  # pragma: no cover -- see
    # `_raw_logical_drives_bitmask` above.
    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive_root)))


def list_fixed_drives() -> list[Path]:
    r"""Every locally-attached fixed drive (`DRIVE_FIXED`) on this machine, as real `Path`
    objects like `Path("C:\\")` -- the "scan my whole computer" SIMPLE-mode full-drive-scan
    entry point's drive enumeration (`GET /api/scan/fixed-drives`, `POST /api/scan/full-drive`;
    see `api.service`).

    Uses `GetLogicalDrives()` (a bitmask of the 26 possible drive letters actually present) plus
    one `GetDriveTypeW` call per present letter, filtered to `DRIVE_FIXED` only -- explicitly
    excludes removable media (USB sticks, SD cards -- `DRIVE_REMOVABLE`), network drives (mapped
    shares -- scanning someone else's server was never the intent), optical drives, RAM disks,
    and unknown/no-root-directory drives. A drive letter that merely exists but isn't a real
    local fixed disk must never silently become part of "my whole computer".

    Raises `NoFixedDrivesFoundError` rather than returning an empty list when zero fixed drives
    are found -- every real Windows machine has at least one fixed `C:\` drive, so an empty
    result here means something is genuinely wrong (an unusual/sandboxed environment, or the
    Win32 call failing in an unexpected way), and the caller (the full-drive-scan endpoint) must
    refuse loudly rather than silently "scanning" zero drives and reporting a hollow success.
    """
    bitmask = _raw_logical_drives_bitmask()
    drives: list[Path] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not (bitmask & (1 << index)):
            continue
        drive_root = f"{letter}:\\"
        if _raw_drive_type(drive_root) == _DRIVE_FIXED:
            drives.append(Path(drive_root))

    if not drives:
        raise NoFixedDrivesFoundError(
            "GetLogicalDrives()/GetDriveTypeW() found zero DRIVE_FIXED volumes on this machine "
            "-- refusing to start a full-drive scan of nothing. Every real Windows machine has "
            "at least one fixed C:\\ drive, so this is unexpected; if it persists, check this "
            "process's Win32 GetLastError diagnostics."
        )
    return drives
