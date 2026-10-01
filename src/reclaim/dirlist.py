from __future__ import annotations

import ctypes
import functools
import struct
import threading
from ctypes import wintypes

# Directory listing that returns each entry's FILE ID, size, attributes and times straight from
# the directory enumeration itself (`GetFileInformationByHandleEx` with
# `FileIdExtdDirectoryInfo`), instead of the `os.scandir` + one `os.stat()` per entry the scan
# walk used to need. `os.scandir` on Windows is backed by `FindNextFile`, whose
# `WIN32_FIND_DATA` carries attributes/size/times but NOT the file ID (st_ino) or the volume
# serial (st_dev) -- see `scanner.build_record`'s comment -- so a correct hardlink-identity scan
# had to pay a CreateFile + GetFileInformationByHandle + CloseHandle round-trip per file. One
# enumeration call returns ~a thousand entries (one kernel transition, one buffer) with the ID
# included.
#
# Scope/exactness contract (enforced by tests/test_dirlist.py + the real-subtree equality check
# in the PR): for an entry on an NTFS volume this module's (size, attributes, mtime, ctime, ino,
# dev) must equal what `os.stat(path, follow_symlinks=False)` reports. It is only trusted on
# NTFS (`volume_is_ntfs`): ReFS has 128-bit IDs and different directory-entry caching, and
# FAT/exFAT/network redirectors may not support the info class at all -- every one of those makes
# `list_directory` raise `ListingUnsupported`, and the caller falls back to the legacy
# scandir+stat path for that directory. Reparse points and cloud placeholders are never
# resolved from the listing by the caller either (they keep the timeout-guarded `os.stat`).

_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE: never block anyone else's access
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # required to open a directory handle
_INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

# FILE_INFO_BY_HANDLE_CLASS values (winbase.h)
_FILE_ID_INFO = 18
_FILE_ID_EXTD_DIRECTORY_INFO = 19
_FILE_ID_EXTD_DIRECTORY_RESTART_INFO = 20

_ERROR_NO_MORE_FILES = 18
# Win32 errors meaning "this volume/redirector does not implement this info class" -- as opposed
# to a real access/IO failure, which must propagate exactly like a failed `os.scandir` does.
# (INVALID_FUNCTION, NOT_SUPPORTED, INVALID_PARAMETER, INVALID_LEVEL)
_UNSUPPORTED_ERRORS = frozenset({1, 50, 87, 124})

_BUFFER_BYTES = 256 * 1024

# FILE_ID_EXTD_DIR_INFO, fixed 88-byte prefix (little-endian, packed as the kernel lays it out):
#   0 NextEntryOffset u32 | 4 FileIndex u32 (skipped) | 8 CreationTime i64 | 16 LastAccessTime
#   (skipped) | 24 LastWriteTime i64 | 32 ChangeTime (skipped) | 40 EndOfFile i64 | 48
#   AllocationSize (skipped) | 56 FileAttributes u32 | 60 FileNameLength u32 | 64 EaSize u32 |
#   68 ReparsePointTag u32 | 72 FileId (128-bit, low u64 then high u64) | 88 FileName[].
_ENTRY = struct.Struct("<I4xq8xq8xq8xIIIIQQ")
_NAME_OFFSET = 88
assert _ENTRY.size == _NAME_OFFSET  # noqa: S101 -- layout self-check, import-time only

_FILETIME_UNIX_EPOCH_OFFSET = 116444736000000000
_TICKS_PER_SECOND = 10_000_000


class ListingUnsupported(OSError):
    """The volume or redirector behind a directory can't serve the ID-bearing listing; the
    caller must fall back to `os.scandir` + per-entry `os.stat()` for it."""


class _FileIdInfo(ctypes.Structure):
    _fields_ = (("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16))


@functools.cache
def _kernel32() -> ctypes.WinDLL:
    """The configured kernel32 handle, built on first use rather than at import so importing
    `reclaim.scanner` stays possible off Windows (same lazy convention as `preflight.py`)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetFileInformationByHandleEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    k32.GetVolumeInformationByHandleW.argtypes = (
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    k32.GetVolumeInformationByHandleW.restype = wintypes.BOOL
    return k32


# volume serial -> "is NTFS". A volume's file system never changes under a live process, so one
# query per volume per process is enough; a plain dict is safe across the scan's worker threads
# (worst case two threads both query once and store the same answer).
_ntfs_by_volume: dict[int, bool] = {}
_volume_lock = threading.Lock()


def _raise_last_error(path: str, error: int) -> None:
    raise OSError(None, ctypes.FormatError(error).strip(), path, error)


def _volume_is_ntfs(handle: int, serial: int) -> bool:
    cached = _ntfs_by_volume.get(serial)
    if cached is not None:
        return cached
    fs_name = ctypes.create_unicode_buffer(32)
    ok = _kernel32().GetVolumeInformationByHandleW(
        handle, None, 0, None, None, None, fs_name, len(fs_name)
    )
    is_ntfs = bool(ok) and fs_name.value == "NTFS"
    with _volume_lock:
        _ntfs_by_volume[serial] = is_ntfs
    return is_ntfs


def filetime_to_unix_seconds(filetime: int) -> float:
    """FILETIME (100ns ticks since 1601) -> the exact float `os.stat().st_mtime` reports for
    it. Mirrors CPython's `FILE_TIME_to_time_t_nsec` + `fill_time` (C truncating division, then
    `seconds + nanoseconds * 1e-9`) rather than a simpler `ticks / 1e7 - epoch` expression,
    which differs from it in the last float bits for many timestamps -- the index compares
    mtimes for equality (`is_unchanged`), so a one-ulp drift would flag every file as changed."""
    shifted = filetime - _FILETIME_UNIX_EPOCH_OFFSET
    seconds = abs(shifted) // _TICKS_PER_SECOND
    remainder = abs(shifted) % _TICKS_PER_SECOND
    if shifted < 0:
        seconds, remainder = -seconds, -remainder
    return seconds + remainder * 100 * 1e-9


# One listed entry: (name, attributes, size, last_write_filetime, creation_filetime, file_id_low,
# file_id_high). A plain tuple, not a dataclass -- this is built once per file on the hot path.
ListedEntry = tuple[str, int, int, int, int, int, int]


_buffers = threading.local()


def _thread_buffer() -> tuple[ctypes.Array[ctypes.c_char], memoryview]:
    """One reusable enumeration buffer per worker thread. Allocating (and zero-filling) 256KB
    for every directory was measured at ~50us/directory -- more than the enumeration call
    itself -- and the kernel overwrites the portion it uses, so reuse is safe."""
    pair = getattr(_buffers, "pair", None)
    if pair is None:
        buffer = ctypes.create_string_buffer(_BUFFER_BYTES)
        pair = (buffer, memoryview(buffer).cast("B"))
        _buffers.pair = pair
    return pair


def list_directory(path: str, volume_serial: int | None = None) -> tuple[int, list[ListedEntry]]:
    r"""Lists `path` (a `\\?\`-prefixed absolute string, as `scanner.long_path` produces) and
    returns `(volume_serial, entries)` with `.`/`..` omitted, exactly like `os.scandir`.

    `volume_serial` is the 64-bit serial `os.stat().st_dev` reports. Pass `None` for the first
    directory of a walk: it is read from the open directory handle and the volume is checked to
    be NTFS (else `ListingUnsupported`). A caller that already holds the serial for this walk
    passes it back in to skip both calls (~60us/directory measured): every directory reached
    without crossing a reparse point lives on the same volume as the one the walk started on,
    since an NTFS volume can only join another's namespace through a reparse point (mount
    point), which the scan never descends into.

    Raises `OSError` (carrying the Win32 error code, so `str(exc)` reads like `os.scandir`'s)
    when the directory can't be opened or read, and `ListingUnsupported` when the volume isn't
    NTFS or the info class isn't available -- the only two outcomes a caller needs to
    distinguish."""
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        path,
        _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        _raise_last_error(path, ctypes.get_last_error())
    try:
        if volume_serial is None:
            id_info = _FileIdInfo()
            if not kernel32.GetFileInformationByHandleEx(
                handle, _FILE_ID_INFO, ctypes.byref(id_info), ctypes.sizeof(id_info)
            ):
                error = ctypes.get_last_error()
                if error in _UNSUPPORTED_ERRORS:
                    raise ListingUnsupported(None, "FileIdInfo unsupported", path, error)
                _raise_last_error(path, error)
            volume_serial = id_info.VolumeSerialNumber
            if not _volume_is_ntfs(handle, volume_serial):
                raise ListingUnsupported(None, "not an NTFS volume", path, 0)

        entries: list[ListedEntry] = []
        buffer, view = _thread_buffer()
        info_class = _FILE_ID_EXTD_DIRECTORY_RESTART_INFO
        unpack_from = _ENTRY.unpack_from
        while True:
            if not kernel32.GetFileInformationByHandleEx(handle, info_class, buffer, _BUFFER_BYTES):
                error = ctypes.get_last_error()
                if error == _ERROR_NO_MORE_FILES:
                    break
                if error in _UNSUPPORTED_ERRORS and info_class == (
                    _FILE_ID_EXTD_DIRECTORY_RESTART_INFO
                ):
                    raise ListingUnsupported(None, "directory info class unsupported", path, error)
                _raise_last_error(path, error)
            info_class = _FILE_ID_EXTD_DIRECTORY_INFO
            offset = 0
            while True:
                (
                    next_offset,
                    creation,
                    last_write,
                    size,
                    attributes,
                    name_len,
                    _ea,
                    _tag,
                    id_lo,
                    id_hi,
                ) = unpack_from(view, offset)
                start = offset + _NAME_OFFSET
                name = str(view[start : start + name_len], "utf-16-le", "surrogatepass")
                if name != "." and name != "..":
                    entries.append((name, attributes, size, last_write, creation, id_lo, id_hi))
                if next_offset == 0:
                    break
                offset += next_offset
        return volume_serial, entries
    finally:
        kernel32.CloseHandle(handle)
