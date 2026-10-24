"""Relocate a directory to another volume behind a junction (spec `docs/specs/relocate.md`).

The data moves, the path stays: after a move the old path is a directory junction to the copy
on the other volume, so no program's configuration changes. The stages are the ones the
prototype (`scripts/relocate_dir.ps1`) proved on the real machine:

    plan (dry run, changes nothing) -> probe -> copy -> verify every file (SHA-256) -> freeze
    (rename the source to `<name>.reclaim-moved`) -> delta re-sync -> junction swap

The old copy is kept until a separate `delete_old_copy` call, which re-hashes everything first.
Every step is written to a journal before it runs, so a process killed at any point leaves a
state `recover` either rolls back or finishes; `rollback` restores the original at any stage,
including after the old copy was deleted (it copies the data back first).

Nothing here deletes data the user has no other copy of: the only trees removed are our own
copy in the target (before the swap, or after a rollback proved every file of it is in the
restored source) and the old copy (after every file of it is proved present in the target).
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import hashlib
import json
import os
import shutil
import stat
import struct
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

import structlog

from reclaim.config import Config
from reclaim.executor import rmtree_reparse_point_safe
from reclaim.models import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FILE_ATTRIBUTE_REPARSE_POINT
from reclaim.safety import SafetyValidator
from reclaim.safety_env import assert_not_real_profile_under_pytest
from reclaim.scanner import long_path

logger = structlog.get_logger(__name__)

MOVED_SUFFIX = ".reclaim-moved"
PROBE_SUFFIX = ".reclaim-probe"
RESTORE_SUFFIX = ".reclaim-restore"
# Spec rule 9: the target volume needs 1.2x the source size free (the copy plus headroom for
# whatever else writes to that volume during a long copy).
SPACE_MARGIN = 1.2
# Spec section 2: the old copy is kept until the user confirms, or this many days pass.
KEEP_OLD_COPY_DAYS = 7
# Files written this close to the copy's start are re-hashed in the delta pass even when their
# size and mtime look unchanged (the prototype's 2 s window: NTFS mtime is precise, but a
# writer can finish inside the same tick the copy read the file).
_DELTA_WINDOW_NS = 2_000_000_000
# Room the journal's drive needs for the move's journal and manifest (a few hundred bytes per
# file) and for the rollback's own writes if the move fails: the journal usually lives on the
# drive being freed, which is often the full one (B7 M2). A failed save after the freeze would
# leave the folder parked at `<name>.reclaim-moved` with no way to roll back.
JOURNAL_RESERVE_BYTES = 64 * 2**20
_CHUNK = 1 << 20
_MAX_LISTED = 5

# Offline and cloud-placeholder bits: reading such a file makes the sync client download it, so
# a move would pull a whole cloud folder onto the disk it is meant to free.
_FILE_ATTRIBUTE_OFFLINE = 0x00001000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_CLOUD_BITS = _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN
_CLOUD_BITS |= FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
# Attributes carried to the copy (robocopy /COPY:DAT's "A"): read-only, hidden, system, archive,
# not-content-indexed.
_COPIED_ATTRS = 0x1 | 0x2 | 0x4 | 0x20 | 0x2000
_FILE_ATTRIBUTE_NORMAL = 0x80
# EFS: a plain read-and-write copy would write the file back in the clear (or fail for another
# user), so an encrypted tree is never moved (B7 L5).
_FILE_ATTRIBUTE_ENCRYPTED = 0x4000
# GetDriveTypeW: only a local fixed drive is a target; a network, removable or RAM drive can
# vanish while the junction still points at it.
_DRIVE_FIXED = 3
_DRIVE_KINDS = {
    0: "unknown",
    1: "missing",
    2: "removable",
    4: "network",
    5: "optical",
    6: "RAM",
}


class Verdict(StrEnum):
    MOVABLE = "movable"
    GUIDED = "guided"  # movable only through the owning program's own steps (Docker, WSL)
    MUST_STAY = "must_stay"
    ALREADY_MOVED = "already_moved"


class Stage(StrEnum):
    PLANNED = "planned"
    PROBING = "probing"
    COPYING = "copying"
    COPIED = "copied"
    FREEZING = "freezing"
    FROZEN = "frozen"
    SYNCING = "syncing"
    SYNCED = "synced"
    SWAPPING = "swapping"
    DONE = "done"
    DELETING_OLD = "deleting_old"
    DELETED_OLD = "deleted_old"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class RelocateError(Exception):
    """A move, rollback or delete refused or failed. `str()` says what to do next."""


class InUseError(RelocateError):
    """A rename probe failed: some program holds a file open under the directory."""


class NothingChangedError(RelocateError):
    """A rollback refused before it changed anything (no space, a folder in use, nothing to put
    back): the journal goes back to the stage it was at, so a DONE move stays DONE and its
    `delete-old` still works (B8)."""


class _TargetInUseError(InUseError, NothingChangedError):
    """`InUseError` for the rollback's probe of the moved folder, before any change."""


@dataclass(frozen=True, slots=True)
class Policy:
    """What must never move, decided before anything else is looked at.

    `exclusion_match` is the user's exclusion check (`SafetyValidator.exclusion_match`): it is
    called first, on the path text, then on the names beneath it, and a hit ends the inspection.
    `protected` is the built-in protected-roots check. The path lists are matched as "is,
    inside, or contains", case-insensitively.
    """

    exclusion_match: Callable[[Path], str | None]
    protected: Callable[[Path], bool]
    # The same check on the path text as journaled, without following a junction: after a move
    # the original path is a junction, which `exclusion_match` canonicalises to the target's
    # path, so a path pattern naming the original folder would no longer hit (B8 M-B). Hits
    # from either check refuse. None: only `exclusion_match` is applied.
    exclusion_match_text: Callable[[Path], str | None] | None = None
    hardlink_caches: tuple[Path, ...] = ()
    guided: tuple[tuple[Path, str], ...] = ()
    app_dirs: tuple[Path, ...] = ()
    broad_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class Classification:
    verdict: Verdict
    reasons: tuple[str, ...]
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    newest_mtime: float = 0.0
    link_target: str | None = None


@dataclass(slots=True)
class TreeScan:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    newest_mtime: float = 0.0
    reparse_points: list[str] = field(default_factory=list)
    multi_linked: list[str] = field(default_factory=list)
    venv_markers: list[str] = field(default_factory=list)
    cloud_files: list[str] = field(default_factory=list)
    encrypted: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Plan:
    source: str
    target: str
    moved: str
    ok: bool
    reasons: tuple[str, ...]
    files: int
    bytes: int
    target_free: int
    source_free: int


@dataclass(slots=True)
class Journal:
    """The move's durable record and audit log. Saved (atomically) before every step."""

    id: str
    source: str
    target: str
    moved: str
    stage: str
    files: int
    bytes: int
    created_at: float
    source_free_before: int
    target_created: bool = False
    created_parents: list[str] = field(default_factory=list)
    copy_started_ns: int = 0
    junction_attempted: bool = False
    swapped_at: float = 0.0
    source_free_after: int | None = None
    # Folder identities (NTFS file IDs, which a rename keeps): the original folder, the
    # rollback's restore copy and our target. Only a folder with the first or second ID is
    # ever taken as "the original" at the original path (second hostile review, B2), so a
    # folder something else created there is never reported as restored. The *_planned flags
    # are saved before the folder is made, so a kill between the two is recognisable.
    source_id: int = 0
    restore_id: int = 0
    restore_planned: bool = False
    target_id: int = 0
    target_planned: bool = False
    # Saved before the old copy's removal starts: from then on it may be partial (B6 M2).
    old_copy_partial: bool = False
    # Saved before the freeze rename: until then the original was never moved (B3 H1).
    freeze_attempted: bool = False
    # The old copy as it was at the swap; delete_old_copy compares against it (B3 H3).
    manifest: str = ""
    # Set only once the restore folder is a complete, hash-verified copy of the moved folder, and
    # cleared as soon as anything starts changing it again. A restore folder without it may be
    # partial and is never put back as the folder (B7 H1).
    restore_verified: bool = False
    # The user asked for the old copy to be put back; recover honours it (B7 L1).
    use_old_copy: bool = False
    # Set when the journal reaches ROLLED_BACK. A later DELETED_OLD then ends a move that was
    # undone, so it no longer blocks the folder; without it the path is a live junction (B7 L3).
    rolled_back: bool = False
    # The volume (NTFS serial) of the original folder; a folder file ID alone is only unique
    # per volume (B7 L5). 0 in journals written before this field.
    source_dev: int = 0
    history: list[list[object]] = field(default_factory=list)
    path: str = ""

    def log(self, stage: Stage, note: str = "", clock: Callable[[], float] = time.time) -> None:
        self.stage = stage.value
        if stage == Stage.ROLLED_BACK:
            self.rolled_back = True
        self.history.append([round(clock(), 3), stage.value, note])
        self.save()
        logger.info("relocate_step", id=self.id, stage=stage.value, note=note)

    def save(self) -> None:
        data = asdict(self)
        data.pop("path")
        tmp = self.path + ".tmp"
        assert_not_real_profile_under_pytest(tmp, operation="write the journal")
        with open(tmp, "w", encoding="utf-8") as fh:  # noqa: PTH123 -- str path, see module note
            json.dump(data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)  # noqa: PTH105 -- str paths, see module note

    @classmethod
    def load(cls, path: Path | str) -> Journal:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            data = json.load(fh)
        if not isinstance(data, dict):
            raise TypeError(f"{path} is not a move journal")
        journal = cls(**data)
        for name in ("id", "source", "target", "moved", "stage"):
            if not isinstance(getattr(journal, name), str):
                raise TypeError(f"{path}: field {name!r} is not text")
        journal.path = str(path)
        return journal


@dataclass(frozen=True, slots=True)
class Outcome:
    journal: Journal
    message: str
    kept: tuple[str, ...] = ()


# ------------------------------------------------------------------ path helpers


def _norm(path: Path | str) -> str:
    """Absolute, backslashed, no `\\\\?\\` prefix, no trailing separator (except a drive root)."""
    text = str(path)
    if text[:8].upper() == "\\\\?\\UNC\\":
        text = "\\\\" + text[8:]  # \\?\UNC\server\share -> \\server\share (B10 L3)
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    text = str(Path(text).absolute())
    if len(text) > 3:
        text = text.rstrip("\\")
    return text


def _key(path: Path | str) -> str:
    return _norm(path).casefold()


def _relation(a: Path | str, b: Path | str) -> bool:
    """True when `a` is `b`, is inside it, or contains it."""
    ka, kb = _key(a), _key(b)
    return ka == kb or ka.startswith(kb + "\\") or kb.startswith(ka + "\\")


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.stat(long_path(Path(path)), follow_symlinks=False)  # noqa: PTH116
    except OSError:
        return None


def _is_reparse(path: str) -> bool:
    st = _lstat(path)
    return st is not None and bool(st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _exists(path: str) -> bool:
    return _lstat(path) is not None


def _junction_target(path: str) -> str | None:
    try:
        return _norm(os.readlink(long_path(Path(path))))  # noqa: PTH115
    except (OSError, ValueError):
        return None


def _volume_serial(path: str) -> int | None:
    """The volume serial of `path`, or of its nearest existing ancestor."""
    current = Path(_norm(path))
    while True:
        st = _lstat(str(current))
        if st is not None:
            try:
                return os.stat(long_path(current)).st_dev  # noqa: PTH116 -- follows a link
            except OSError:
                return None
        if current.parent == current:
            return None
        current = current.parent


def _volume_anchor(path: str) -> str:
    """The nearest existing real folder at or above `path`: a junction (the moved folder's
    old path) is not followed, so what is measured is the volume `path` itself is on."""
    current = Path(_norm(path))
    while (not _exists(str(current)) or _is_reparse(str(current))) and current.parent != current:
        current = current.parent
    return str(current)


def _free_bytes(path: str) -> int:
    """Free space of the volume `path` itself is on, never through a junction (B6 M1)."""
    return shutil.disk_usage(long_path(Path(_volume_anchor(path)))).free


# ------------------------------------------------------------------ Win32


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_kernel32.DeviceIoControl.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_kernel32.SetFileAttributesW.restype = wintypes.BOOL
_kernel32.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetLongPathNameW.restype = wintypes.DWORD


def _long_name(path: str) -> str | None:
    """`path` with every component spelled as it is on disk (8.3 aliases expanded), or None
    when it cannot be read. Renaming an 8.3 alias renames the real entry, so a move given a
    short name would leave the real path empty (B9 M1)."""
    query = str(long_path(Path(path)))
    size = _kernel32.GetLongPathNameW(query, None, 0)
    if size == 0:
        return None
    buf = ctypes.create_unicode_buffer(size)
    if _kernel32.GetLongPathNameW(query, buf, size) == 0:
        return None
    return _norm(buf.value)


def _as_spelled_on_disk(path: str) -> tuple[str, str | None]:
    """`(path, reason)`. The nearest existing part of `path` (itself, or the closest existing
    ancestor of a target that is still to be created) must be spelled as it is on disk, because
    every journal, overlap and app-folder check compares path text: an 8.3 alias anywhere in it
    passes them all (B9 M1 for the source, B10 M1 for the target's parent). A difference in case
    only is not refused: the path is returned with the on-disk case, so the folder keeps its
    own name on disk and every later check sees one spelling (B10 L2). `reason` is set when the
    spelling is another one, or cannot be read."""
    existing, rest = path, []
    while not _exists(existing):
        parent = _norm(Path(existing).parent)
        if _key(parent) == _key(existing):
            return path, None  # nothing of it exists (a drive that is gone): refused elsewhere
        rest.append(Path(existing).name)
        existing = parent
    spelled = _long_name(existing)
    if spelled is None:
        return path, f"the full name of {existing} could not be read"
    if _key(spelled) != _key(existing):
        return path, f"{existing} is a short or alternate name: use its full name, {spelled}"
    return _norm(Path(spelled, *reversed(rest))), None


class _StreamData(ctypes.Structure):
    # WIN32_FIND_STREAM_DATA: MAX_PATH + 36 characters for the stream name.
    _fields_ = [("StreamSize", ctypes.c_longlong), ("cStreamName", ctypes.c_wchar * 296)]


_kernel32.FindFirstStreamW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_kernel32.FindFirstStreamW.restype = wintypes.HANDLE
_kernel32.FindNextStreamW.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
_kernel32.FindNextStreamW.restype = wintypes.BOOL
_kernel32.FindClose.argtypes = [wintypes.HANDLE]
_kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetVolumePathNameW.restype = wintypes.BOOL
_kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
_kernel32.GetVolumeInformationW.restype = wintypes.BOOL
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_ERROR_HANDLE_EOF = 38
_UNNAMED_STREAM = "::$DATA"

_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FSCTL_SET_REPARSE_POINT = 0x000900A4
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_INVALID_HANDLE = wintypes.HANDLE(-1).value


def _create_junction(link: str, target: str) -> None:
    r"""Makes `link` (which must not exist) a directory junction to `target`.

    Not `_winapi.CreateJunction`: it resolves the target without the `\\?\` prefix, so on a
    machine with `LongPathsEnabled=0` it fails for a target over 260 characters, and a prefixed
    target makes it write a broken `\??\\\?\` substitute name (both measured 2026-10-24). This
    writes the mount-point reparse data itself; `\??\` names have no 260 limit.
    """
    assert_not_real_profile_under_pytest(link, operation="create a junction at")
    substitute = ("\\??\\" + _norm(target)).encode("utf-16-le")
    printed = _norm(target).encode("utf-16-le")
    names = substitute + b"\0\0" + printed + b"\0\0"
    body = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed)) + names
    buffer = struct.pack("<IHH", _IO_REPARSE_TAG_MOUNT_POINT, len(body), 0) + body
    raw = long_path(Path(link))
    os.mkdir(raw)  # noqa: PTH102
    try:
        handle = _kernel32.CreateFileW(
            raw,
            _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle in (None, _INVALID_HANDLE):
            raise ctypes.WinError(ctypes.get_last_error())  # noqa: TRY301
        try:
            returned = wintypes.DWORD(0)
            data = ctypes.create_string_buffer(buffer, len(buffer))
            ok = _kernel32.DeviceIoControl(
                handle,
                _FSCTL_SET_REPARSE_POINT,
                data,
                len(buffer),
                None,
                0,
                ctypes.byref(returned),
                None,
            )
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(handle)
    except BaseException:
        os.rmdir(raw)  # noqa: PTH106 -- still an empty directory: the reparse data never landed
        raise


def _remove_junction(link: str) -> None:
    """Removes the junction entry only; `os.rmdir` on a reparse point never touches its target."""
    assert_not_real_profile_under_pytest(link, operation="remove the junction")
    if not _is_reparse(link):
        raise RelocateError(f"{link} is not a junction; refusing to remove it")
    os.rmdir(long_path(Path(link)))  # noqa: PTH106


def _set_attributes(path: str, attributes: int) -> None:
    value = attributes & _COPIED_ATTRS or _FILE_ATTRIBUTE_NORMAL
    if not _kernel32.SetFileAttributesW(path, value):
        raise ctypes.WinError(ctypes.get_last_error())


def _named_streams(path: str) -> list[str]:
    """The named data streams of a file or folder (`:name:$DATA`), not the unnamed one.

    NTFS files can carry more than their main content: the "downloaded from the internet" mark
    (`Zone.Identifier`), app metadata, a whole second payload. A plain read-and-write copies
    only the main stream, so these would be lost once the old copy is deleted (found by the
    first hostile review, B1 finding 4); every copy and every hash here covers them."""
    data = _StreamData()
    handle = _kernel32.FindFirstStreamW(path, 0, ctypes.byref(data), 0)
    if handle in (None, _INVALID_HANDLE):
        error = ctypes.get_last_error()
        if error == _ERROR_HANDLE_EOF:
            return []
        raise ctypes.WinError(error)
    names: list[str] = []
    try:
        while True:
            if data.cStreamName != _UNNAMED_STREAM:
                names.append(data.cStreamName)
            if not _kernel32.FindNextStreamW(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error == _ERROR_HANDLE_EOF:
                    break
                raise ctypes.WinError(error)
    finally:
        _kernel32.FindClose(handle)
    return sorted(names)


def _rename(src: str, dst: str) -> None:
    assert_not_real_profile_under_pytest(src, operation="rename")
    assert_not_real_profile_under_pytest(dst, operation="rename onto")
    os.rename(long_path(Path(src)), long_path(Path(dst)))  # noqa: PTH104


# ------------------------------------------------------------------ trees


def _walk(root: str) -> Iterable[tuple[str, os.DirEntry[str], bool]]:
    """(relative path, entry, is_dir) for everything under `root`, never following a reparse
    point. Raises OSError for an unreadable directory: callers that copy or verify must not
    treat "could not list" as "empty"."""
    base = long_path(Path(root))
    stack = [("", base)]
    while stack:
        rel_dir, current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                rel = rel_dir + "\\" + entry.name if rel_dir else entry.name
                st = entry.stat(follow_symlinks=False)
                if st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    # The plan refused trees with links; one appearing later is not copied as
                    # a plain file or folder (that would follow it or drop it).
                    raise RelocateError(f"{rel} under {root} is a link; a copy would not keep it")
                is_dir = entry.is_dir(follow_symlinks=False)
                yield rel, entry, is_dir
                if is_dir:
                    stack.append((rel, entry.path))


def scan_tree(root: str) -> TreeScan:
    """One read-only pass: counts, bytes, and everything that makes a tree unsafe to move."""
    out = TreeScan()
    base = long_path(Path(root))
    stack = [("", base)]
    while stack:
        rel_dir, current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            out.unreadable.append(rel_dir or ".")
            continue
        with it:
            for entry in it:
                rel = rel_dir + "\\" + entry.name if rel_dir else entry.name
                try:
                    # A direct os.stat: DirEntry.stat() leaves st_nlink at 0 on Windows.
                    st = os.stat(entry.path, follow_symlinks=False)  # noqa: PTH116
                except OSError:
                    out.unreadable.append(rel)
                    continue
                attrs = st.st_file_attributes
                if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                    out.reparse_points.append(rel)
                    continue
                if entry.name.casefold() in {".venv", "venv", "site-packages", "pyvenv.cfg"}:
                    out.venv_markers.append(rel)
                if attrs & _FILE_ATTRIBUTE_ENCRYPTED:
                    out.encrypted.append(rel)
                if stat.S_ISDIR(st.st_mode):
                    out.dirs += 1
                    stack.append((rel, entry.path))
                    continue
                out.files += 1
                out.bytes += st.st_size
                out.newest_mtime = max(out.newest_mtime, st.st_mtime)
                if st.st_nlink > 1:
                    out.multi_linked.append(rel)
                if attrs & _CLOUD_BITS:
                    out.cloud_files.append(rel)
    return out


def _listing(root: str) -> tuple[dict[str, os.stat_result], set[str]]:
    files: dict[str, os.stat_result] = {}
    dirs: set[str] = set()
    for rel, entry, is_dir in _walk(root):
        if is_dir:
            dirs.add(rel)
        else:
            files[rel] = os.stat(entry.path, follow_symlinks=False)  # noqa: PTH116
    return files, dirs


def _stream_sha256(path: str) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:  # noqa: PTH123
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.digest()


def _sha256(path: str) -> str:
    """The file's content identity: the SHA-256 of its main stream, then the name and SHA-256
    of each named stream, so a dropped or altered stream is a difference like any other."""
    digest = hashlib.sha256(_stream_sha256(path))
    for name in _named_streams(path):
        digest.update(name.encode("utf-8"))
        digest.update(_stream_sha256(path + name))
    return digest.hexdigest()


def _copy_file(src: str, dst: str) -> None:
    """Copies data, timestamps and attributes. Verification is the callers' job: every copy is
    compared by SHA-256 against its source before anything depends on it.

    The timestamps are those of the source as it was opened, before any byte was read: if a
    writer changes it during the read, its mtime moves on and no longer matches the copy's,
    which is what the delta pass looks for."""
    assert_not_real_profile_under_pytest(dst, operation="write")
    if os.path.lexists(dst):
        # A re-copy: CREATE_ALWAYS refuses a read-only file, and also a hidden or system one
        # unless the same attributes are passed, so reset them first.
        _set_attributes(dst, 0)
    with open(src, "rb") as fi:  # noqa: PTH123
        st = os.fstat(fi.fileno())
        _copy_stream(fi, dst)
    streams = _named_streams(src)
    for stale in set(_named_streams(dst)) - set(streams):
        os.remove(dst + stale)  # noqa: PTH107 -- a stream of our own earlier copy, not a file
    for name in streams:
        with open(src + name, "rb") as fi:  # noqa: PTH123
            _copy_stream(fi, dst + name)
    # Last: writing a stream moves the file's mtime, and a read-only file takes no utime.
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    _set_attributes(dst, st.st_file_attributes)


def _copy_stream(fi: BinaryIO, dst: str) -> None:
    assert_not_real_profile_under_pytest(dst, operation="write")
    with open(dst, "wb") as fo:  # noqa: PTH123
        while chunk := fi.read(_CHUNK):
            fo.write(chunk)
        fo.flush()
        os.fsync(fo.fileno())


def _join(root: str, rel: str) -> str:
    return long_path(Path(root)) + "\\" + rel


def _copy_tree(
    src_root: str, dst_root: str, *, checkpoint: Callable[[str], None] | None = None
) -> None:
    for rel, entry, is_dir in _walk(src_root):
        dst = _join(dst_root, rel)
        if is_dir:
            assert_not_real_profile_under_pytest(dst, operation="create")
            os.makedirs(dst, exist_ok=True)  # noqa: PTH103
            _copy_folder_meta(entry.path, dst)
        else:
            _copy_file(entry.path, dst)
            if checkpoint is not None:
                checkpoint("copy_file")


def _copy_folder_meta(src: str, dst: str) -> None:
    """A folder's own named streams and attributes (folders are created, not copied, so these
    would otherwise be lost: B2 L2, L3). Stale streams of our own earlier copy are removed."""
    streams = _named_streams(src)
    assert_not_real_profile_under_pytest(dst, operation="remove streams of")
    for stale in set(_named_streams(dst)) - set(streams):
        os.remove(dst + stale)  # noqa: PTH107 -- a stream of our own copy of the folder
    for name in streams:
        with open(src + name, "rb") as fi:  # noqa: PTH123
            _copy_stream(fi, dst + name)
    _set_attributes(dst, os.stat(src, follow_symlinks=False).st_file_attributes)  # noqa: PTH116


def _folder_meta(path: str) -> tuple[int, str]:
    """(copied attribute bits, digest of the named streams) of a folder."""
    digest = hashlib.sha256()
    for name in _named_streams(path):
        digest.update(name.encode("utf-8"))
        digest.update(_stream_sha256(path + name))
    attrs = os.stat(path, follow_symlinks=False).st_file_attributes  # noqa: PTH116
    return attrs & _COPIED_ATTRS, digest.hexdigest()


def _gone(path: str) -> bool:
    """True only when `path` is definitely absent (not found). Any other error while
    checking (access denied, a drive that is busy) refuses, so a transient fault is never
    taken for a missing folder (fourth hostile review, B4 M1)."""
    try:
        os.stat(long_path(Path(path)), follow_symlinks=False)  # noqa: PTH116
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError as exc:
        raise RelocateError(f"{path} could not be checked ({exc}); retry") from exc
    return False


def _folder_id(path: str) -> int:
    """The NTFS file ID of a real folder at `path`, 0 if there is none (absent, or a link)."""
    st = _lstat(path)
    if (
        st is None
        or st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT
        or not stat.S_ISDIR(st.st_mode)
    ):
        return 0
    return st.st_ino


def _filesystem(path: str) -> str | None:
    """The file system name of the volume holding `path` (or its nearest existing ancestor)."""
    current = Path(_norm(path))
    while not _exists(str(current)) and current.parent != current:
        current = current.parent
    root = ctypes.create_unicode_buffer(1024)
    if not _kernel32.GetVolumePathNameW(long_path(current), root, len(root)):
        return None
    name = ctypes.create_unicode_buffer(64)
    ok = _kernel32.GetVolumeInformationW(root.value, None, 0, None, None, None, name, len(name))
    return name.value if ok else None


def _drive_type(path: str) -> int:
    """GetDriveTypeW of the volume holding `path` (or its nearest existing ancestor); 0 when
    it cannot be read, which is not a fixed drive."""
    current = Path(_norm(path))
    while not _exists(str(current)) and current.parent != current:
        current = current.parent
    root = ctypes.create_unicode_buffer(1024)
    if not _kernel32.GetVolumePathNameW(long_path(current), root, len(root)):
        return 0
    return int(_kernel32.GetDriveTypeW(root.value))


def _keep_root_flags(moved: str, link: str) -> None:
    """The original folder's hidden and system bits, on the junction that replaces it."""
    attrs = os.stat(long_path(Path(moved)), follow_symlinks=False).st_file_attributes  # noqa: PTH116
    flags = attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM)
    if flags:
        _set_attributes(long_path(Path(link)), flags)


def _differences(
    a_root: str, b_root: str, rels: Iterable[str], digests: dict[str, str] | None = None
) -> list[str]:
    """Relative paths whose SHA-256 differs between the two trees (or that `b` lacks).
    `digests`, when given, receives each `a`-side digest (the swap manifest)."""
    bad = []
    for rel in rels:
        b = _join(b_root, rel)
        a_digest = _sha256(_join(a_root, rel))
        if digests is not None:
            digests[rel] = a_digest
        if not os.path.lexists(b) or a_digest != _sha256(b):
            bad.append(rel)
    return bad


def _sample(items: Sequence[str]) -> str:
    shown = ", ".join(items[:_MAX_LISTED])
    return shown + (f" and {len(items) - _MAX_LISTED} more" if len(items) > _MAX_LISTED else "")


# ------------------------------------------------------------------ policy and candidates

_DOCKER_STEPS = (
    "Docker Desktop keeps its data in one disk image: move it with Settings > Resources > "
    "Advanced > Disk image location, not with a junction"
)
_WSL_STEPS = (
    "a WSL distribution is moved with `wsl --export <name> <file>`, `wsl --unregister <name>` "
    "and `wsl --import <name> <folder on the other drive> <file>`, not with a junction"
)


def default_policy(
    config: Config, *, environ: Mapping[str, str], app_dirs: Sequence[Path]
) -> Policy:
    """The production policy: the user's exclusions and the built-in protected roots (via
    `SafetyValidator`), the hardlink caches that must stay with their environments, Docker and
    WSL as guided moves, the profile roots as too broad, and Reclaim's own data."""
    validator = SafetyValidator(config)
    home = Path(environ.get("USERPROFILE", str(Path.home())))
    local = Path(environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    roaming = Path(environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    hardlink = (
        local / "uv",
        local / "pnpm",
        home / ".pnpm-store",
        home / "anaconda3" / "pkgs",
        home / "miniconda3" / "pkgs",
        home / ".conda" / "pkgs",
    )
    guided: list[tuple[Path, str]] = [
        (local / "Docker", _DOCKER_STEPS),
        (local / "wsl", _WSL_STEPS),
    ]
    packages = local / "Packages"
    try:
        with os.scandir(packages) as it:
            guided.extend(
                (Path(entry.path), _WSL_STEPS)
                for entry in it
                if entry.name.casefold().startswith("canonicalgrouplimited.")
            )
    except OSError:
        pass
    return Policy(
        exclusion_match=lambda path: validator.exclusion_match(path, is_dir=True),
        exclusion_match_text=lambda path: validator.exclusion_match(
            path, is_dir=True, lexical=True
        ),
        protected=validator.path_is_protected_root,
        hardlink_caches=hardlink,
        guided=tuple(guided),
        app_dirs=tuple(app_dirs),
        broad_roots=(home, home / "AppData", local, roaming),
    )


def known_locations(environ: Mapping[str, str]) -> list[Path]:
    """Folders that are often large, rebuildable or re-downloadable, and read by path: the
    usual relocation candidates. Each is still classified; this list grants nothing."""
    home = Path(environ.get("USERPROFILE", str(Path.home())))
    local = Path(environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    cache = home / ".cache"
    return [
        cache / "huggingface" / "hub",
        cache / "huggingface" / "datasets",
        cache / "huggingface" / "xet",
        cache / "torch",
        cache / "whisper",
        cache / "lm-studio",
        home / ".lmstudio" / "models",
        home / ".ollama" / "models",
        home / ".keras",
        local / "Android" / "Sdk",
        home / ".gradle" / "caches",
        home / ".m2" / "repository",
        home / ".nuget" / "packages",
        home / ".cargo" / "registry",
        local / "npm-cache",
        local / "pip" / "cache",
        local / "Yarn" / "Cache",
        local / "ms-playwright",
        local / "Docker",
        local / "wsl",
        local / "uv",
        home / ".conda" / "pkgs",
        home / "anaconda3" / "pkgs",
        home / "miniconda3" / "pkgs",
    ]


@dataclass(frozen=True, slots=True)
class Candidate:
    path: str
    classification: Classification
    suggested_target: str | None


def suggested_target(source: Path | str, volume_root: Path | str) -> str:
    r"""`C:\Users\x\.cache\hub` on `D:\` -> `D:\reclaim-relocated\C\Users\x\.cache\hub`: the whole
    original path under one folder, so two candidates can never collide."""
    src = Path(_norm(source))
    drive = src.drive.rstrip(":")
    return str(Path(_norm(volume_root)) / "reclaim-relocated" / drive / Path(*src.parts[1:]))


def propose_candidates(
    paths: Iterable[Path], policy: Policy, target_volume: Path | None
) -> list[Candidate]:
    """Classifies each existing path (read-only: no probe, which renames) and ranks them:
    movable first, then by size."""
    out = []
    for path in paths:
        if not _exists(_norm(path)):
            continue
        found = classify(path, policy)
        target = suggested_target(path, target_volume) if target_volume is not None else None
        out.append(Candidate(_norm(path), found, target))
    order = {Verdict.MOVABLE: 0, Verdict.GUIDED: 1, Verdict.ALREADY_MOVED: 2, Verdict.MUST_STAY: 3}
    out.sort(key=lambda c: (order[c.classification.verdict], -c.classification.bytes, c.path))
    return out


# ------------------------------------------------------------------ classify and plan


def classify(source: Path | str, policy: Policy) -> Classification:
    """Whether `source` may move, and why not. Exclusion is decided first and ends the
    inspection: an excluded path is never listed, sized or probed."""
    path = Path(_norm(source))
    hit = policy.exclusion_match(path)
    if hit is not None:
        return Classification(Verdict.MUST_STAY, ("it is, or contains, an excluded project",))
    reasons: list[str] = []
    for root, steps in policy.guided:
        key, root_key = _key(path), _key(root)
        if key == root_key or key.startswith(root_key + "\\"):
            return _guided(path, steps)
        if root_key.startswith(key + "\\"):
            reasons.append(f"it contains {root}, which is moved with its program's own steps")
    if policy.protected(path):
        reasons.append("it is a protected system or program location")
    for root in policy.broad_roots:
        if _key(path) == _key(root) or _key(root).startswith(_key(path) + "\\"):
            reasons.append(f"it is, or contains, {root}; move a folder inside it instead")
    if len(path.parts) <= 1:
        reasons.append("it is a drive root")
    for root in policy.app_dirs:
        if _relation(path, root):
            reasons.append(f"it is, is inside, or contains Reclaim's own data ({root})")
    for root in policy.hardlink_caches:
        if _relation(path, root):
            reasons.append(
                f"it is, is inside, or contains {root}, a cache hardlinked into environments; "
                "hardlinks cannot cross volumes, so every environment would become a full copy"
            )
    if reasons:
        return Classification(Verdict.MUST_STAY, tuple(reasons))
    st = _lstat(str(path))
    if st is None:
        return Classification(Verdict.MUST_STAY, ("it does not exist",))
    if st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        target = _junction_target(str(path))
        if target is not None and _volume_serial(target) != _volume_serial(str(path.parent)):
            return Classification(
                Verdict.ALREADY_MOVED, (f"it is already a link to {target}",), link_target=target
            )
        return Classification(
            Verdict.MUST_STAY, ("it is a link, not a folder",), link_target=target
        )
    if not stat.S_ISDIR(st.st_mode):
        return Classification(Verdict.MUST_STAY, ("it is a file, not a folder",))
    scan = scan_tree(str(path))
    if any(part.casefold() in {".venv", "venv", "site-packages"} for part in path.parts):
        scan.venv_markers.insert(0, ".")
    if scan.venv_markers:
        reasons.append(
            "it is in, or contains, a Python environment, which holds hardlinks into package "
            f"caches ({_sample(scan.venv_markers)})"
        )
    if scan.reparse_points:
        reasons.append(
            f"it contains links (junctions or symlinks) a copy would not keep "
            f"({_sample(scan.reparse_points)})"
        )
    if scan.multi_linked:
        reasons.append(
            f"it contains hardlinked files, which a copy would split ({_sample(scan.multi_linked)})"
        )
    if scan.cloud_files:
        reasons.append(
            "it contains cloud placeholders that reading would download "
            f"({_sample(scan.cloud_files)})"
        )
    if scan.encrypted:
        reasons.append(
            "it contains EFS-encrypted files or folders, which a copy would not keep encrypted "
            f"({_sample(scan.encrypted)})"
        )
    if scan.unreadable:
        reasons.append(f"some of it could not be read ({_sample(scan.unreadable)})")
    if scan.files == 0:
        reasons.append("it holds no files")
    verdict = Verdict.MUST_STAY if reasons else Verdict.MOVABLE
    return Classification(
        verdict,
        tuple(reasons),
        files=scan.files,
        dirs=scan.dirs,
        bytes=scan.bytes,
        newest_mtime=scan.newest_mtime,
    )


def _guided(path: Path, steps: str) -> Classification:
    """A folder that moves only through its program's own steps, with its size: the list and
    the page show how much a Docker or WSL move would free (B8 G1). The size is `scan_tree`'s:
    `lstat` sizes only, no file opened, no link followed. A path that is not a real folder
    (missing, or a link a scan would follow) is reported with no size."""
    st = _lstat(str(path))
    if st is None or st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        return Classification(Verdict.GUIDED, (steps,))
    if not stat.S_ISDIR(st.st_mode):
        return Classification(Verdict.GUIDED, (steps,), files=1, bytes=st.st_size)
    scan = scan_tree(str(path))
    return Classification(
        Verdict.GUIDED,
        (steps,),
        files=scan.files,
        dirs=scan.dirs,
        bytes=scan.bytes,
        newest_mtime=scan.newest_mtime,
    )


def probe(source: str) -> None:
    """Rename round-trip: Windows refuses to rename a directory while a file under it is open,
    so success means no program held one at that moment. Never forces or kills a holder."""
    probe_path = source + PROBE_SUFFIX
    if _exists(probe_path):
        raise RelocateError(f"{probe_path} already exists; inspect it before moving {source}")
    try:
        _rename(source, probe_path)
    except OSError as exc:
        raise InUseError(
            f"{source} is in use (a program has a file open under it): close it and retry"
        ) from exc
    try:
        _rename(probe_path, source)
    except OSError as exc:  # pragma: no cover -- only a racing writer can recreate `source`
        raise RelocateError(
            f"the probe left the folder at {probe_path}: rename it back to {source}"
        ) from exc


_WORKING_SUFFIXES = (MOVED_SUFFIX, PROBE_SUFFIX, RESTORE_SUFFIX)


def _os_errors_as_relocate_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """For the public entry points that take a journal path (`rollback`, `recover`,
    `delete_old_copy`): an `OSError` that no step turned into a message (a rename or a removal
    that a scanner, an open window or a permission refused) becomes a `RelocateError` naming
    the move, the path and the OS error, and saying to close what holds it and run the same
    command again. One boundary instead of a handler per call site (B8 M-C, M-D). The journal
    is left at the stage the step logged before it ran, which a rerun reads from the disk."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except OSError as exc:
            journal_path = kwargs.get("journal_path", args[0] if args else "")
            move_id = Path(str(journal_path)).stem.removeprefix("relocate-")
            command = func.__name__.replace("_old_copy", "-old")
            raise RelocateError(
                f"move {move_id}: {command} stopped at {exc.filename or 'an unknown path'} "
                f"({exc.strerror or exc}). The journal records how far it got and a rerun "
                "continues from there. Close whatever holds it open (a program, a virus scan, "
                f"an Explorer window) and run `reclaim relocate {command} {move_id}` again"
            ) from exc

    return wrapper


def _is_inert(journal: Journal) -> bool:
    """A move that was undone: ROLLED_BACK, or DELETED_OLD after a rollback (`rolled_back`). It
    holds nothing and owns nothing, and acting on it again would touch whatever a later move of
    the same folder put at the same paths (B8 M-A). A DELETED_OLD without the flag is a
    finished move, not inert."""
    return journal.stage == Stage.ROLLED_BACK or (
        journal.stage == Stage.DELETED_OLD and journal.rolled_back
    )


def _refuse_if_superseded(journal: Journal, journal_path: Path) -> None:
    """Refuses (changing nothing) while a later move of the same folder is not inert: every
    identity check of an older journal can pass against the later move's folders, so its
    rollback would undo the live one (B8 M-A). A journal folder that cannot be read refuses
    too: not knowing is not a pass."""
    try:
        others = journals(journal_path.parent)
    except (OSError, RelocateError) as exc:
        raise RelocateError(
            f"the move journals next to {journal_path} could not be read ({exc}); nothing "
            "changed. Fix that, then run the same command again"
        ) from exc
    for other in others:
        if (
            other.id != journal.id
            and _key(other.source) == _key(journal.source)
            and not _is_inert(other)
            # Ties broken by id, so two journals with one timestamp (a hand-made copy) never
            # each block the other (B9 L1).
            and (other.created_at, other.id) > (journal.created_at, journal.id)
        ):
            raise RelocateError(
                f"move {other.id} ({other.stage}) is a later, live move of {journal.source}; "
                f"acting on the older move {journal.id} would undo it. Nothing changed. Deal "
                f"with move {other.id} instead"
            )


def _kept_by(moved: str, journal_dir: Path | None) -> str | None:
    """The id of the newest journal whose old copy is `moved`, if the journals can be read."""
    if journal_dir is None:
        return None
    try:
        mine = [j for j in journals(journal_dir) if _key(j.moved) == _key(moved)]
    except (OSError, ValueError, TypeError, RelocateError):
        return None
    return max(mine, key=lambda j: j.created_at).id if mine else None


def _overlap_reasons(src: str, tgt: str, journal_dir: Path | None) -> list[str]:
    """What ties this move to Reclaim's own working folders or to another move: any of those
    would let one move's rollback or delete reach into another's data (B1 finding 3)."""
    reasons = []
    for label, path in (("source", src), ("target", tgt)):
        if any(part.casefold().endswith(s) for part in Path(path).parts for s in _WORKING_SUFFIXES):
            reasons.append(f"the {label} is inside a folder Reclaim uses while moving another")
        for ancestor in Path(path).parents:
            if _is_reparse(str(ancestor)):
                reasons.append(f"the {label} is reached through a link ({ancestor})")
                break
    if journal_dir is None:
        return reasons
    try:
        others = journals(journal_dir)
    except (OSError, ValueError, TypeError, RelocateError) as exc:
        return [*reasons, f"a move journal in {journal_dir} could not be read ({exc})"]
    for other in others:
        if _is_inert(other):
            # Undone moves hold nothing. A DELETED_OLD without the flag is a finished move: its
            # path is a live junction, so it must keep blocking (B7 L3).
            continue
        busy = (other.source, other.target, other.moved, other.source + RESTORE_SUFFIX)
        if any(_relation(mine, theirs) for mine in (src, tgt) for theirs in busy):
            reasons.append(
                f"it overlaps move {other.id} ({other.source} -> {other.target}, {other.stage}); "
                "finish or roll that one back first"
            )
    return reasons


def plan_move(
    source: Path | str,
    target: Path | str,
    policy: Policy,
    *,
    free_bytes: Callable[[str], int] = _free_bytes,
    run_probe: bool = True,
    journal_dir: Path | None = None,
) -> Plan:
    """The dry run. Changes nothing (the probe renames and renames back).

    A probe of its own that a kill left half-done (the folder at `<name>.reclaim-probe`, nothing
    at its own path) is put back first: only the probe ever makes that name."""
    src, src_spelling = _as_spelled_on_disk(_norm(source))
    tgt, tgt_spelling = _as_spelled_on_disk(_norm(target))
    moved = src + MOVED_SUFFIX
    probe_path = src + PROBE_SUFFIX
    if (
        not _exists(src)
        and _exists(probe_path)
        and not _is_reparse(probe_path)
        # Path text only (src does not exist): never rename onto an excluded path (B2 M3).
        and policy.exclusion_match(Path(src)) is None
    ):
        _rename(probe_path, src)
        logger.info("relocate_probe_restored", source=src)
    found = classify(src, policy)
    reasons = list(found.reasons) if found.verdict is not Verdict.MOVABLE else []
    if found.verdict is Verdict.ALREADY_MOVED:
        reasons = list(found.reasons)
    # The freeze renames whatever name it is given, and every journal check compares text.
    reasons.extend(r for r in (src_spelling, tgt_spelling) if r is not None)
    for label, path in (("source", src), ("target", tgt)):
        if path.startswith("\\\\"):
            # A network path, or a share that is another spelling of a local folder
            # (`\\localhost\C$\...`): relocate works on local drive paths only (B10 L3).
            reasons.append(f"the {label} {path} is a network path; use its local drive path")
    if found.verdict is Verdict.GUIDED:
        reasons.insert(0, "this folder is moved with its program's own steps")
    if _relation(src, tgt):
        reasons.append("the target is the source, inside it, or contains it")
    if policy.exclusion_match(Path(tgt)) is not None:
        reasons.append("the target is, or contains, an excluded project")
    if policy.protected(Path(tgt)):
        reasons.append("the target is a protected system or program location")
    for root in (*policy.app_dirs, *policy.hardlink_caches):
        if _relation(tgt, root):
            reasons.append(f"the target is, is inside, or contains {root}")
    if _exists(tgt):
        reasons.append(f"the target {tgt} already exists; choose a new folder")
    if _exists(moved):
        kept_by = _kept_by(moved, journal_dir)
        if kept_by is None:
            reasons.append(f"{moved} already exists (left by an earlier move?): inspect it first")
        else:
            reasons.append(
                f"{moved} is the old copy kept by move {kept_by}: delete it with "
                f"`reclaim relocate delete-old {kept_by}` (or remove it yourself) first"
            )
    for leftover in (probe_path, src + RESTORE_SUFFIX, tgt + PROBE_SUFFIX):
        if _exists(leftover):
            reasons.append(f"{leftover} already exists (left by an earlier run?): inspect it first")
    reasons.extend(_overlap_reasons(src, tgt, journal_dir))
    for label, path in (("source", src), ("target", tgt)):
        fs = _filesystem(path)
        if fs != "NTFS":
            # Folder IDs that survive a rename, named streams and junction targets are what
            # the move's safety rests on; all three are NTFS behaviour.
            reasons.append(f"the {label} is on a {fs or 'unknown'} volume; NTFS is required")
    kind = _drive_type(tgt)
    if kind != _DRIVE_FIXED:
        reasons.append(
            f"the target is on a {_DRIVE_KINDS.get(kind, 'non-fixed')} drive; only a local fixed "
            "drive is allowed, because the junction would break whenever it went away"
        )
    src_vol, tgt_vol = _volume_serial(src), _volume_serial(tgt)
    if src_vol is None or tgt_vol is None:
        reasons.append("the volume of the source or target could not be read")
    elif src_vol == tgt_vol:
        reasons.append("the target is on the same volume, so nothing would be freed")
    target_free = free_bytes(tgt)
    source_free = free_bytes(src) if _exists(src) else 0
    if journal_dir is not None and found.verdict is Verdict.MOVABLE:
        journal_free = free_bytes(str(journal_dir))
        if journal_free < JOURNAL_RESERVE_BYTES:
            reasons.append(
                f"the drive of the journal folder {journal_dir} has {journal_free / 2**20:.0f} MiB "
                f"free; the move needs {JOURNAL_RESERVE_BYTES // 2**20} MiB there for its record, "
                "or it could not be undone if it failed"
            )
    if found.verdict is Verdict.MOVABLE and target_free < SPACE_MARGIN * found.bytes:
        reasons.append(
            f"the target volume has {target_free / 2**30:.1f} GiB free; the move needs "
            f"{SPACE_MARGIN * found.bytes / 2**30:.1f} GiB ({SPACE_MARGIN}x the folder)"
        )
    if not reasons and run_probe:
        try:
            probe(src)
        except RelocateError as exc:
            reasons.append(str(exc))
    return Plan(
        source=src,
        target=tgt,
        moved=moved,
        ok=not reasons,
        reasons=tuple(reasons),
        files=found.files,
        bytes=found.bytes,
        target_free=target_free,
        source_free=source_free,
    )


# ------------------------------------------------------------------ move


def execute_move(
    source: Path | str,
    target: Path | str,
    policy: Policy,
    journal_dir: Path,
    *,
    free_bytes: Callable[[str], int] = _free_bytes,
    checkpoint: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> Outcome:
    """Moves `source` behind a junction. Re-runs the whole dry run first (the state may have
    changed since the user saw it). Any ordinary failure rolls back before returning; a process
    killed mid-way leaves its journal for `recover`. The in-use probe runs after the journal
    exists, so a kill between its two renames is recoverable too."""
    plan = plan_move(
        source, target, policy, free_bytes=free_bytes, run_probe=False, journal_dir=journal_dir
    )
    reasons = list(plan.reasons)
    if any(_relation(journal_dir, p) for p in (plan.source, plan.target, plan.moved)):
        # The journal would be renamed away with the source at the freeze (B1 finding 5).
        reasons.append(f"the journal folder {journal_dir} overlaps the move")
    if reasons:
        raise RelocateError("not moved: " + "; ".join(reasons))
    assert_not_real_profile_under_pytest(journal_dir, operation="create")
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = Journal(
        id=uuid.uuid4().hex[:12],
        source=plan.source,
        target=plan.target,
        moved=plan.moved,
        stage=Stage.PLANNED.value,
        files=plan.files,
        bytes=plan.bytes,
        created_at=clock(),
        source_free_before=plan.source_free,
        source_id=_folder_id(plan.source),
        source_dev=_folder_dev(plan.source),
    )
    journal.path = str(journal_dir / f"relocate-{journal.id}.json")
    journal.log(Stage.PLANNED, f"{plan.files} files, {plan.bytes} bytes", clock)
    journal.log(Stage.PROBING, "", clock)
    try:
        probe(plan.source)
    except RelocateError as exc:
        # Usually nothing changed (the first rename failed). But a program can recreate the
        # folder between the probe's two renames (B3 H2): the rollback decides from the disk.
        _rollback_or_explain(journal, exc, clock)
        raise RelocateError(f"not moved: {exc}") from exc
    hook = checkpoint or (lambda _name: None)
    try:
        _make_target(journal)
        journal.copy_started_ns = time.time_ns()
        journal.log(Stage.COPYING, "", clock)
        _copy_tree(plan.source, plan.target, checkpoint=hook)
        hook("after_copy")
        note = _verify_copy(plan.source, plan.target)
        journal.log(Stage.COPIED, note, clock)
        hook("after_verify")
        if _folder_id(plan.source) != journal.source_id:
            # The folder was replaced during the copy (B3 H1): the one there now is not the
            # one copied, and freezing it would hand the journal a folder it cannot recognise.
            raise RelocateError(f"{plan.source} was replaced by another folder during the copy")  # noqa: TRY301
        if policy.exclusion_match(Path(plan.source)) is not None:
            # Something the plan never saw appeared during the copy, checked at its real path
            # (path patterns) before the freeze and by name after it (B2 M2, B3 M5).
            raise RelocateError("an excluded project appeared in the folder during the copy")  # noqa: TRY301
        journal.freeze_attempted = True
        journal.log(Stage.FREEZING, "", clock)
        try:
            _rename(plan.source, plan.moved)
        except OSError as exc:
            raise InUseError(f"{plan.source} came into use during the copy: retry later") from exc
        if _folder_id(plan.moved) != journal.source_id:
            # Replaced after the ID check, before the rename: put the other folder back where
            # it was; the freeze, in effect, never happened.
            _rename(plan.moved, plan.source)
            journal.freeze_attempted = False
            journal.save()
            raise RelocateError(f"{plan.source} was replaced by another folder during the copy")  # noqa: TRY301
        journal.log(Stage.FROZEN, "", clock)
        hook("after_freeze")
        if policy.exclusion_match(Path(plan.moved)) is not None:
            raise RelocateError("an excluded project appeared in the folder during the copy")  # noqa: TRY301
        journal.log(Stage.SYNCING, "", clock)
        digests: dict[str, str] = {}
        note = _delta_sync(
            plan.moved, plan.target, journal.copy_started_ns, checkpoint=hook, digests=digests
        )
        journal.manifest = _write_manifest(journal, digests)
        journal.log(Stage.SYNCED, note, clock)
        hook("after_delta")
        journal.junction_attempted = True
        journal.log(Stage.SWAPPING, "", clock)
        _create_junction(plan.source, plan.target)
        _keep_root_flags(plan.moved, plan.source)
        hook("after_junction")
        _post_swap_check(journal)
        journal.swapped_at = clock()
        journal.log(Stage.DONE, f"old copy kept at {plan.moved}", clock)
    except Exception as exc:
        logger.warning("relocate_failed", id=journal.id, stage=journal.stage, error=str(exc))
        outcome = _rollback_or_explain(journal, exc, clock)
        raise RelocateError(f"move failed and was rolled back ({exc}). {outcome.message}") from exc
    return Outcome(journal, f"moved; {plan.source} now points to {plan.target}")


def _rollback_or_explain(
    journal: Journal, cause: BaseException, clock: Callable[[], float]
) -> Outcome:
    """The failed move's own rollback. If that fails too (a held file at the rename, the
    journal's drive full), say which move it is and what to run, never a raw error (B7 M2)."""
    try:
        return rollback(Path(journal.path), clock=clock)
    except (OSError, RelocateError) as exc:
        raise RelocateError(
            f"move {journal.id} failed ({cause}) and could not be rolled back ({exc}). Nothing "
            f"was deleted; the folder may be parked at {journal.moved}. Run "
            f"`reclaim relocate recover {journal.id}` once the cause is gone"
        ) from exc


def _write_manifest(journal: Journal, digests: Mapping[str, str]) -> str:
    """The old copy exactly as it is at the swap: every file's size, mtime and SHA-256 (from
    the full compare that just ran), every folder's attributes and streams. `delete_old_copy`
    deletes the old copy only if it still matches this record, so nothing written into it
    after the move can go with it, whatever its timestamps say (B3 H3, M1, L3)."""
    files, dirs = _listing(journal.moved)
    record = {
        "files": {
            rel: [st.st_size, st.st_mtime_ns, digests[rel]] for rel, st in sorted(files.items())
        },
        "dirs": {
            rel: list(
                _folder_meta(_join(journal.moved, rel) if rel else long_path(Path(journal.moved)))
            )
            for rel in ["", *sorted(dirs)]
        },
    }
    # Not "*.json": `journals()` and every caller glob "relocate-*.json" for journals.
    path = journal.path[: -len(".json")] + ".manifest"
    tmp = path + ".tmp"
    assert_not_real_profile_under_pytest(tmp, operation="write the swap manifest")
    with open(tmp, "w", encoding="utf-8") as fh:  # noqa: PTH123
        json.dump(record, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # noqa: PTH105
    return path


def _make_target(journal: Journal) -> None:
    parents: list[str] = []
    current = Path(journal.target).parent
    while not _exists(str(current)) and current.parent != current:
        parents.append(str(current))
        current = current.parent
    for parent in reversed(parents):
        assert_not_real_profile_under_pytest(parent, operation="create")
        os.mkdir(long_path(Path(parent)))  # noqa: PTH102
        journal.created_parents.append(parent)
        journal.save()
    assert_not_real_profile_under_pytest(journal.target, operation="create")
    # Saved first: a kill between the mkdir and the next save leaves an empty folder rollback
    # can recognise as ours (B2 L7), instead of one that blocks every later plan.
    journal.target_planned = True
    journal.save()
    os.mkdir(long_path(Path(journal.target)))  # noqa: PTH102
    journal.target_created = True
    journal.target_id = _folder_id(journal.target)
    journal.save()


def _verify_copy(src: str, dst: str) -> str:
    """After the copy, before the freeze: only the listing is summarised here. The proof is
    the full SHA-256 compare in `_delta_sync`, which runs on the frozen source: a compare
    against the live source can be fooled by a writer that restores its mtime between this
    point and the freeze (first hostile review, B1 finding 8), and it reads everything twice."""
    src_files, _ = _listing(src)
    dst_files, _ = _listing(dst)
    return f"copied {len(dst_files)} of {len(src_files)} files; verified after the freeze"


def _delta_sync(
    moved: str,
    target: str,
    copy_started_ns: int,
    *,
    checkpoint: Callable[[str], None] | None = None,
    digests: dict[str, str] | None = None,
) -> str:
    """Brings the target to the frozen source exactly, then proves it: the SHA-256 of every
    file (with its named streams) must be equal on both sides, and so must the attributes of
    every file and folder and the named streams of every folder, the root's included.

    First the cheap pass: what was created, changed (by size, mtime, or a write inside the copy
    window) or deleted during the copy. Removes from the target only files and folders the
    source no longer has (our own copies of them). A case-only rename of a folder or a file
    gives the copy the new spelling instead of copying it again and deleting the old name
    (NTFS treats both names as one). Then the full compare; a file that still differs is
    copied once more, and a second difference fails the move (it rolls back)."""
    _align_folder_case(moved, target)
    src_files, src_dirs = _listing(moved)
    dst_files, dst_dirs = _listing(target)
    by_key = {rel.casefold(): rel for rel in dst_files}
    for rel in src_files:
        spelled = by_key.get(rel.casefold())
        if spelled is not None and spelled != rel:
            _rename(_join(target, spelled), _join(target, rel))
            dst_files[rel] = dst_files.pop(spelled)
            by_key[rel.casefold()] = rel
    window = copy_started_ns - _DELTA_WINDOW_NS
    recopy = [
        rel
        for rel, st in src_files.items()
        if rel not in dst_files
        or dst_files[rel].st_size != st.st_size
        or dst_files[rel].st_mtime_ns != st.st_mtime_ns
        or st.st_mtime_ns >= window
    ]
    for rel in sorted(src_dirs - dst_dirs):
        assert_not_real_profile_under_pytest(_join(target, rel), operation="create")
        os.makedirs(_join(target, rel), exist_ok=True)  # noqa: PTH103
    for rel in recopy:
        _copy_file(_join(moved, rel), _join(target, rel))
        if checkpoint is not None:
            checkpoint("delta_file")
    gone = sorted(set(dst_files) - set(src_files))
    for rel in gone:
        path = _join(target, rel)
        assert_not_real_profile_under_pytest(path, operation="delete the copy")
        _set_attributes(path, 0)
        os.unlink(path)  # noqa: PTH108
    for rel in sorted(dst_dirs - src_dirs, key=len, reverse=True):
        path = _join(target, rel)
        assert_not_real_profile_under_pytest(path, operation="remove")
        _set_attributes(path, 0)  # a read-only folder refuses rmdir (B2 L5)
        os.rmdir(path)  # noqa: PTH106
    _verify_listing(moved, target)
    bad = _differences(moved, target, src_files, digests)
    for rel in bad:
        _copy_file(_join(moved, rel), _join(target, rel))
    still = _differences(moved, target, bad)
    if still:
        raise RelocateError(f"{len(still)} files differ after copying them twice: {_sample(still)}")
    meta = _sync_metadata(moved, target, src_files, src_dirs)
    return (
        f"re-copied {len(recopy)}, removed {len(gone)} from the copy; SHA-256 equal for all "
        f"{len(src_files)} files ({len(bad)} fixed by a second copy); {meta}"
    )


def _align_folder_case(moved: str, target: str) -> None:
    """Gives the copy's folders the frozen source's spelling, parents first (B2 L4)."""
    _, src_dirs = _listing(moved)
    _, dst_dirs = _listing(target)
    spelled = {rel.casefold(): rel for rel in dst_dirs}
    for rel in sorted(src_dirs, key=lambda r: r.count("\\")):
        other = spelled.get(rel.casefold())
        if other is None:
            continue
        parent, _, leaf = rel.rpartition("\\")
        old_leaf = other.rpartition("\\")[2]
        if old_leaf != leaf:
            here = parent + "\\" + old_leaf if parent else old_leaf
            _rename(_join(target, here), _join(target, rel))


def _sync_metadata(
    moved: str, target: str, files: Mapping[str, os.stat_result], dirs: set[str]
) -> str:
    """Attributes of every file, and attributes and named streams of every folder (and of the
    root), made equal to the frozen source's, then compared. An attribute-only change between
    the copy and the freeze is otherwise invisible to size, mtime and content (B2 L1)."""
    fixed = 0
    for rel, st in files.items():
        dst = _join(target, rel)
        have = os.stat(dst, follow_symlinks=False).st_file_attributes  # noqa: PTH116
        if (have ^ st.st_file_attributes) & _COPIED_ATTRS:
            _set_attributes(dst, st.st_file_attributes)
            fixed += 1
    folders = ["", *sorted(dirs, key=len, reverse=True)]
    for rel in folders:
        src = _join(moved, rel) if rel else long_path(Path(moved))
        dst = _join(target, rel) if rel else long_path(Path(target))
        if _folder_meta(src) != _folder_meta(dst):
            _copy_folder_meta(src, dst)
            fixed += 1
        if _folder_meta(src) != _folder_meta(dst):
            raise RelocateError(
                f"the folder {rel or '.'} could not be given its streams/attributes"
            )
    return f"attributes and folder streams equal ({fixed} set)"


def _verify_listing(a: str, b: str) -> None:
    a_files, a_dirs = _listing(a)
    b_files, b_dirs = _listing(b)
    a_bytes = sum(s.st_size for s in a_files.values())
    b_bytes = sum(s.st_size for s in b_files.values())
    if set(a_files) != set(b_files) or a_dirs != b_dirs or a_bytes != b_bytes:
        raise RelocateError(
            f"{b} does not match {a} ({len(b_files)} vs {len(a_files)} files, "
            f"{b_bytes} vs {a_bytes} bytes)"
        )


def _post_swap_check(journal: Journal) -> None:
    pointed = _junction_target(journal.source)
    if pointed is None or _key(pointed) != _key(journal.target):
        raise RelocateError(f"the junction at {journal.source} does not point to {journal.target}")
    _verify_listing(journal.moved, journal.source)


# ------------------------------------------------------------------ recover and rollback


@_os_errors_as_relocate_errors
def recover(
    journal_path: Path, *, policy: Policy, clock: Callable[[], float] = time.time
) -> Outcome:
    """After a crash: finish a move whose junction is in place and checks out, else roll back.
    `policy` is needed because an interrupted delete of the old copy is finished here, and a
    delete never goes ahead for an excluded project (B7 M3)."""
    journal = Journal.load(journal_path)
    if journal.stage in {Stage.DONE, Stage.DELETED_OLD, Stage.ROLLED_BACK}:
        return Outcome(journal, f"nothing to recover: the move is {journal.stage}")
    if journal.stage == Stage.DELETING_OLD:
        return delete_old_copy(journal_path, policy=policy, clock=clock)
    if journal.stage == Stage.SWAPPING and _junction_target(journal.source) is not None:
        try:
            _post_swap_check(journal)
            _keep_root_flags(journal.moved, journal.source)  # a kill may have landed before it
        except (RelocateError, OSError) as exc:
            logger.warning("relocate_recover_check_failed", id=journal.id, error=str(exc))
            if _only_written_through_the_junction(journal):
                # The listings differ because a program already works in its folder, through
                # the junction (B7 M4): the move is finished, rolling it back would undo it.
                with contextlib.suppress(OSError):
                    _keep_root_flags(journal.moved, journal.source)
                journal.swapped_at = clock()
                journal.log(
                    Stage.DONE,
                    "finished after an interruption; programs had already written through the "
                    "junction",
                    clock,
                )
                return Outcome(journal, f"finished the move; {journal.source} points to the copy")
        else:
            journal.swapped_at = clock()
            journal.log(Stage.DONE, "finished after an interruption", clock)
            return Outcome(journal, f"finished the move; {journal.source} points to the copy")
    return rollback(journal_path, use_old_copy=journal.use_old_copy, clock=clock)


def _only_written_through_the_junction(journal: Journal) -> bool:
    """True when the junction is ours (it points to the journaled target, which is the folder
    this move made) and the old copy is exactly as the swap manifest recorded it: the only
    way the live folder can differ from it then is a program writing through the junction.
    Any doubt (no manifest, an unreadable listing, an old copy that changed) is False, and
    the caller takes the conservative path."""
    pointed = _junction_target(journal.source)
    if pointed is None or _key(pointed) != _key(journal.target):
        return False
    if journal.target_id == 0 or _folder_id(journal.target) != journal.target_id:
        return False
    if not journal.manifest or not _is_original(journal, journal.moved):
        return False
    try:
        return not _old_copy_changes(journal, allow_missing=False)
    except (RelocateError, OSError):
        return False


@_os_errors_as_relocate_errors
def rollback(
    journal_path: Path,
    *,
    use_old_copy: bool = False,
    clock: Callable[[], float] = time.time,
    free_bytes: Callable[[str], int] = _free_bytes,
) -> Outcome:
    """Puts the folder back at its own path, from any stage, reading the disk as well as the
    journal (a crash can land between a step and its journal entry).

    "The original" is decided by identity, never by looks: only the folder whose NTFS file ID
    the journal recorded at the start (a rename keeps it), or the restore copy this rollback
    made, is ever accepted at the original path. Anything else there is removed if empty and
    otherwise left alone, with a message saying where the data is.

    - Before the freeze, the original was never moved: only our probe and copy are undone.
    - Frozen but never swapped: nothing but this code wrote to the copy, so the original is
      renamed back and the copy removed.
    - After the swap, programs have been working in the moved folder. Rollback copies it, as
      it is now, to the original path (a fresh copy, proved file by file) and leaves the copy
      from before the move, `<name>.reclaim-moved`, exactly as it is (ADR-0044 addendum 5:
      no merging of the two, so nothing in either is ever overwritten). Our moved copy is
      removed only once it is byte-identical to the restored folder.
    - `use_old_copy`, or a moved folder that is gone, puts `<name>.reclaim-moved` back instead
      and leaves the moved folder untouched.
    """
    journal = Journal.load(journal_path)
    if _is_inert(journal):
        return Outcome(journal, "already rolled back; nothing to do")
    _refuse_if_superseded(journal, journal_path)
    previous = (journal.stage, journal.use_old_copy, journal.old_copy_partial)
    entries = len(journal.history)
    if journal.stage == Stage.DELETING_OLD:
        journal.old_copy_partial = True  # Reclaim's own interrupted delete (B7 L2)
    # Saved with the first entry: a kill and `recover` must do what the user chose (B7 L1).
    journal.use_old_copy = use_old_copy
    journal.log(Stage.ROLLING_BACK, f"from {journal.stage}", clock)
    try:
        return _rollback_from_disk(journal, clock, use_old_copy, free_bytes)
    except NothingChangedError:
        # Refused before anything changed: the move is exactly what it was, so the journal
        # says so (a DONE move stays DONE, B8). Only if nothing was logged after our entry.
        if len(journal.history) == entries + 1:
            journal.history.pop()
            journal.stage, journal.use_old_copy, journal.old_copy_partial = previous
            journal.save()
        raise


def _rollback_from_disk(
    journal: Journal,
    clock: Callable[[], float],
    use_old_copy: bool,
    free_bytes: Callable[[str], int],
) -> Outcome:
    tgt = journal.target
    target_probe = tgt + PROBE_SUFFIX
    if not _exists(tgt) and journal.target_id and _folder_id(target_probe) == journal.target_id:
        _rename(target_probe, tgt)  # a kill inside rollback's probe of the target (B1 6)
    if not journal.freeze_attempted and not journal.junction_attempted:
        return _rollback_before_freeze(journal, clock)
    if not journal.junction_attempted:
        return _rollback_frozen(journal, clock)
    return _rollback_after_swap(journal, clock, use_old_copy, free_bytes)


def _rollback_frozen(journal: Journal, clock: Callable[[], float]) -> Outcome:
    """Frozen, never swapped. The freeze may have caught a folder a program put there in the
    instant after the ID check (B4 M3, B5 M1): that one is put back where it was and the move
    is undone as if it never froze."""
    src, moved = journal.source, journal.moved
    if _exists(moved) and not _is_original(journal, moved):
        if _exists(src):
            raise RelocateError(
                f"{moved} is a folder another program made, and {src} exists too; inspect both. "
                f"A copy of the original as it was copied is in {journal.target}"
            )
        _rename(moved, src)  # theirs: only our freeze ever makes this name
    if not _exists(moved):
        journal.freeze_attempted = False
        journal.save()
        return _rollback_before_freeze(journal, clock)
    _settle_source(journal)
    if not _exists(src):
        _rename(moved, src)
    if not _is_original(journal, src):
        raise RelocateError(
            f"could not restore {src}; the original is in {_where_original(journal)}"
        )
    kept = _discard_copy(journal)
    note = "restored the original folder" + (f"; kept {kept[0]} ({kept[1]})" if kept else "")
    journal.log(Stage.ROLLED_BACK, note, clock)
    return Outcome(journal, note, kept or ())


def _rollback_after_swap(
    journal: Journal,
    clock: Callable[[], float],
    use_old_copy: bool,
    free_bytes: Callable[[str], int],
) -> Outcome:
    src, tgt, moved = journal.source, journal.target, journal.moved
    restore = src + RESTORE_SUFFIX
    if _exists(moved) and not _is_original(journal, moved):
        raise NothingChangedError(
            f"{moved} is not the folder this move renamed; refusing to touch it"
        )
    if _is_reparse(src):
        pointed = _junction_target(src)
        if pointed is None or _key(pointed) != _key(tgt):
            raise NothingChangedError(f"{src} is a link, but not to {tgt}; refusing to change it")
    _settle_source(journal)
    if _is_original(journal, src):  # an earlier run put a folder back
        if journal.source_id and _folder_id(src) == journal.source_id:
            # It was the copy from before the move (use_old_copy): finish as that run would
            # have, leaving the moved folder untouched and saying so (B9 L2).
            if _gone(tgt):
                extra = f"; {tgt} was not reachable, so anything written there after the move is "
                extra += "only there"
            else:
                extra = f"; the moved folder {tgt} is left as it is, with everything written "
                extra += "there since"
            note = "put back the copy from before the move" + extra
            journal.log(Stage.ROLLED_BACK, note, clock)
            return Outcome(journal, note)
        if _gone(tgt):
            # Our verified restore copy, put back because the target was unreachable: keep
            # that run's warning about writes after the copy (B10 L1).
            return _finish_rollback(
                journal,
                clock,
                f"restored the folder from the verified copy of the moved folder; {tgt} was not "
                "reachable, so anything a program wrote through the junction after that copy "
                f"was verified is only there: reconnect {tgt} to get it",
            )
        return _finish_rollback(journal, clock, "restored the folder")
    target_here = not _gone(tgt)
    if use_old_copy or not target_here:
        return _put_back_old_copy(journal, clock, target_here)
    _probe_target(tgt)
    try:
        files, _ = _listing(tgt)
    except RelocateError as exc:
        raise NothingChangedError(
            f"{exc}. Nothing changed. Rolling back with use_old_copy puts back the copy from "
            "before the move instead, and leaves the moved folder as it is"
        ) from exc
    need = int(sum(st.st_size for st in files.values()) * 1.1)
    # A partial restore of ours is removed before the copy starts again, so its space counts as
    # free (B6 L1); the free space is the source volume's, never measured through the junction.
    partial = sum(st.st_size for st in _listing(restore)[0].values()) if _exists(restore) else 0
    if free_bytes(_volume_anchor(src)) + partial < need:
        raise NothingChangedError(
            f"copying {tgt} back needs {need / 2**30:.1f} GiB free where it came from; nothing "
            "changed. Free that much, or roll back with use_old_copy (puts back the copy from "
            "before the move, without what was written since)"
        )
    started = time.time_ns()
    try:
        _rebuild_restore(journal)
    except RelocateError as exc:
        raise RelocateError(
            f"{exc}. Nothing changed. Rolling back with use_old_copy puts back the copy from "
            "before the move instead"
        ) from exc
    except OSError as exc:
        # The target going away mid-copy is an OSError, not a RelocateError (B7 H1).
        raise RelocateError(
            f"copying {tgt} back was interrupted ({exc}); nothing changed at {src}. The moved "
            f"data is on {tgt}: reconnect it and roll back again"
        ) from exc
    removed = False
    if _is_reparse(src):
        _remove_junction(src)  # a failure here leaves the junction: the path is not empty
        removed = True
    try:
        if removed:
            # Inside the guard: a journal write failing now must not leave the path empty
            # (B9 M2).
            journal.log(Stage.ROLLING_BACK, "junction removed", clock)
        # Anything written to the moved folder between the copy and the removal; the restore
        # folder is ours, so bringing it to the target exactly removes only our own copies.
        # It changes the restore folder, so until the sync has proved it again it is not a
        # copy anything may be put back from (B7 H1).
        journal.restore_verified = False
        journal.save()
        _delta_sync(tgt, restore, started)
        repaired = _repair_from_old_copy(journal, restore)
        journal.restore_verified = True
        journal.save()
        if not _exists(src):
            _rename(restore, src)
    except Exception as exc:
        # Whatever failed after the junction went (a held file at the rename, a read error in
        # the repair), the original path must not be left empty (B6 M3).
        if not _exists(src):
            _put_junction_back(journal)
            journal.log(Stage.ROLLING_BACK, "copying back failed; junction restored", clock)
        if isinstance(exc, RelocateError):
            raise
        raise RelocateError(
            f"copying the folder back failed ({exc}); {src} points to {tgt} again and nothing is "
            "lost. Roll back again when the cause is gone"
        ) from exc
    note = "restored the folder as it is now"
    if repaired:
        note += (
            f"; {len(repaired)} files had other bytes in the moved folder without having been "
            f"changed since the move (a bad copy), so their original was used: {_sample(repaired)}"
        )
    return _finish_rollback(journal, clock, note)


def _put_back_old_copy(journal: Journal, clock: Callable[[], float], target_here: bool) -> Outcome:
    """`use_old_copy`, or a moved folder that is gone: the copy from before the move goes back
    to its path, and the moved folder (if reachable) is left exactly as it is."""
    src, tgt, moved = journal.source, journal.target, journal.moved
    restore = src + RESTORE_SUFFIX
    old = moved if _is_original(journal, moved) else None
    restore_ok = (
        not target_here
        and journal.restore_verified
        and journal.restore_id != 0
        and _is_original(journal, restore)
    )
    if old is not None and journal.old_copy_partial:
        # A delete of the old copy was interrupted: what is left is not the copy from before
        # the move (B6 M2). A verified restore copy of the moved folder is still whole, though.
        if target_here:
            raise NothingChangedError(
                f"{moved} was partly deleted, so it is not the copy from before the move any "
                "more; nothing changed. Roll back without use_old_copy to restore the moved folder"
            )
        if not restore_ok:
            raise NothingChangedError(
                f"{tgt} is not reachable and {moved} was partly deleted; nothing changed. "
                f"Reconnect the drive of {tgt} and roll back again"
            )
        old = None
    if old is None and restore_ok:
        old = restore  # a complete, verified copy an interrupted rollback made from the target
    if old is None and not target_here and _exists(restore) and _is_original(journal, restore):
        # The restore copy exists but was never proved complete (a copy-back cut short): the
        # target holds the only whole copy of the moved folder (B7 H1).
        raise NothingChangedError(
            f"{restore} is only part of the moved folder (the copy back was interrupted), so it "
            f"is not put back; nothing changed. The moved data is on {tgt}: reconnect that "
            "drive and roll back again"
        )
    if old is None:
        if target_here:
            if not _exists(src):
                # An interrupted copy-back had removed the junction: put it back, so the
                # original path is never left empty by a refusal.
                _put_junction_back(journal)
            raise NothingChangedError(
                "there is no copy from before the move left (it was deleted); nothing changed. "
                "Roll back without use_old_copy to restore the moved folder"
            )
        raise NothingChangedError(
            f"there is nothing to put back: the copy from before the move is gone and {tgt} "
            "is not reachable"
        )
    if target_here and old != restore:
        # Our own copy of the moved folder from a stopped copy-back; the moved folder it
        # copied is still there, so it holds nothing unique (B6 L2). Removed BEFORE anything
        # else changes, so a failure here leaves the junction and the journal as they were and
        # a rerun starts clean (B8 M-D).
        _remove_our_restore(journal, restore)
    junction_removed = False
    try:
        if _is_reparse(src):
            _remove_junction(src)
            junction_removed = True
        if not _exists(src):
            _rename(old, src)
    except OSError as exc:
        # Same guard as the copy-back's (B6 M3): never leave the original path empty (B7 M1).
        if junction_removed and not _exists(src):
            _recreate_junction(journal, clock, "putting the old copy back failed")
        raise RelocateError(
            f"could not put {old} back at {src} ({exc}); {src} is as it was and nothing is "
            "lost. Close whatever holds files open there and roll back again"
        ) from exc
    if not _is_original(journal, src):
        raise RelocateError(
            f"could not restore {src}; the original is in {_where_original(journal)}"
        )
    if target_here:
        extra = f"; the moved folder {tgt} is left as it is, with everything written there since"
    else:
        extra = f"; {tgt} was not reachable, so anything written there after the move is only there"
    if old == restore:
        note = (
            f"put back the verified copy of the moved folder that an interrupted rollback made "
            f"from {tgt}; the old copy of the folder had been deleted. {tgt} was not reachable, "
            "so anything a program wrote through the junction after that copy was verified is "
            f"only there: reconnect {tgt} to get it"
        )
    else:
        note = "put back the copy from before the move" + extra
    journal.log(Stage.ROLLED_BACK, note, clock)
    return Outcome(journal, note)


def _remove_our_restore(journal: Journal, restore: str) -> None:
    """Removes `<name>.reclaim-restore` when it is the folder this rollback made."""
    ours = (journal.restore_id and _is_original(journal, restore)) or (
        journal.restore_planned and journal.restore_id == 0
    )
    if not (ours and _exists(restore)):
        return
    try:
        rmtree_reparse_point_safe(long_path(Path(restore)))
    except OSError as exc:
        raise RelocateError(
            f"move {journal.id}: our unfinished copy {restore} could not be removed ({exc}); "
            f"nothing else changed. Close whatever holds it open and run the same command again"
        ) from exc


def _put_junction_back(journal: Journal) -> None:
    """Creates the junction src -> target again after a failed step. If that fails too (a
    second fault), the path is empty and nothing more can be done safely here: the error says
    so, where the data is, and that a rerun finishes the rollback (B10 M2)."""
    src, tgt = journal.source, journal.target
    try:
        _create_junction(src, tgt)
    except OSError as exc:
        raise RelocateError(
            f"move {journal.id}: putting the junction back at {src} failed too ({exc}), so {src} "
            f"is empty right now. Nothing is lost: the data is in {tgt}. Run `reclaim relocate "
            f"rollback {journal.id}` again; it finishes the rollback"
        ) from exc
    with contextlib.suppress(OSError):  # an unreachable target: the junction is what counts
        _keep_root_flags(tgt, src)


def _recreate_junction(journal: Journal, clock: Callable[[], float], why: str) -> None:
    """Puts the junction src -> target back and proves it points there (B7 M1)."""
    src, tgt = journal.source, journal.target
    _put_junction_back(journal)
    pointed = _junction_target(src)
    if pointed is None or _key(pointed) != _key(tgt):
        raise RelocateError(f"the junction at {src} could not be restored to {tgt}; inspect it")
    journal.log(Stage.ROLLING_BACK, f"{why}; junction restored", clock)


def _repair_from_old_copy(journal: Journal, restore: str) -> list[str]:
    """In the copy-back, a file with the mtime the manifest recorded but other bytes was not
    edited by a program: it is a bad copy. Its original comes from the old copy when that
    still matches the record. The old copy is only read; `restore` is ours."""
    if not journal.manifest or not _is_original(journal, journal.moved):
        return []
    record = _read_manifest(journal)
    files, _ = _listing(restore)
    repaired = []
    for rel, (size, mtime, digest) in record["files"].items():
        st = files.get(rel)
        if st is None or st.st_mtime_ns != mtime or _sha256(_join(restore, rel)) == digest:
            continue
        old = _join(journal.moved, rel)
        old_st = _lstat(old)
        if (
            old_st is not None
            and [old_st.st_size, old_st.st_mtime_ns] == [size, mtime]
            and _sha256(old) == digest
        ):
            _copy_file(old, _join(restore, rel))
            if _sha256(_join(restore, rel)) != digest:
                raise RelocateError(f"the original of {rel} did not copy back intact")
            repaired.append(rel)
    return repaired


def _finish_rollback(journal: Journal, clock: Callable[[], float], note: str) -> Outcome:
    if not _is_original(journal, journal.source):
        raise RelocateError(
            f"could not restore {journal.source}; the data is in {_where_original(journal)}"
        )
    kept = _discard_copy(journal)
    if _is_original(journal, journal.moved):
        if journal.old_copy_partial:
            note += (
                f"; the copy from before the move at {journal.moved} was partly deleted by an "
                "interrupted delete, so what is left is incomplete (and unchanged); delete-old "
                "finishes removing it"
            )
        else:
            note += (
                f"; the copy from before the move is kept unchanged at {journal.moved} (delete "
                "it with delete-old once you are sure)"
            )
    if kept:
        note += f"; kept {kept[0]} ({kept[1]})"
    journal.log(Stage.ROLLED_BACK, note, clock)
    return Outcome(journal, note, kept or ())


def _rollback_before_freeze(journal: Journal, clock: Callable[[], float]) -> Outcome:
    """Before the freeze the original was never renamed, except by the probe for a moment.
    Puts the probe back if a kill left it, and removes our copy only if the original is
    where it belongs; if a program replaced the folder meanwhile, the copy is kept."""
    src, probe_path = journal.source, journal.source + PROBE_SUFFIX
    if _is_original(journal, probe_path):
        if _exists(src) and not _is_reparse(src):
            if not _is_empty(src):
                raise RelocateError(
                    f"{src} holds a folder that something else created; the original is at "
                    f"{probe_path}. Move that folder's contents elsewhere, remove the folder, "
                    "and roll back again."
                )
            assert_not_real_profile_under_pytest(src, operation="remove the empty folder")
            os.rmdir(long_path(Path(src)))  # noqa: PTH106 -- empty, no streams, not ours
        if not _exists(src):
            _rename(probe_path, src)
    if _is_original(journal, src):
        kept = _discard_copy(journal)
        note = "not moved; the original folder is unchanged"
    else:
        # A program replaced or removed the folder during the copy: what is there now is
        # theirs, and our copy holds the only remaining copy of the old content.
        kept = (
            (journal.target, "the folder was replaced during the copy; this is a copy of it")
            if _exists(journal.target)
            else None
        )
        note = "not moved; the folder was replaced by another program during the copy"
    if kept:
        note += f"; kept {kept[0]} ({kept[1]})"
    journal.log(Stage.ROLLED_BACK, note, clock)
    return Outcome(journal, note, kept or ())


def _folder_dev(path: str) -> int:
    """The volume serial the folder at `path` is on (0 if it cannot be read)."""
    st = _lstat(path)
    return st.st_dev if st is not None else 0


def _is_original(journal: Journal, path: str) -> bool:
    """Whether `path` is the original folder itself, or the restore copy rollback made: the
    file ID must match and, when the journal knows it, so must the volume (a file ID is only
    unique within one volume, B7 L5)."""
    fid = _folder_id(path)
    if fid == 0 or fid not in {journal.source_id, journal.restore_id} - {0}:
        return False
    return not journal.source_dev or _folder_dev(path) == journal.source_dev


def _is_empty(path: str) -> bool:
    """No entries and no named streams of its own (a folder can carry data in a stream)."""
    with os.scandir(long_path(Path(path))) as it:
        if next(it, None) is not None:
            return False
    return not _named_streams(long_path(Path(path)))


def _where_original(journal: Journal) -> str:
    """Where the original data is now, for messages that tell the user what to do."""
    src = journal.source
    for path in (journal.moved, src + PROBE_SUFFIX, src + RESTORE_SUFFIX):
        if _is_original(journal, path):
            if journal.junction_attempted and path == journal.moved and _exists(journal.target):
                return (
                    f"{path} (as it was before the move) and {journal.target} (with everything "
                    "written since)"
                )
            return path
    if _exists(journal.target):
        return journal.target
    return "no place Reclaim can find (the old copy and the moved folder are both gone)"


def _settle_source(journal: Journal) -> None:
    """Something that is not the original sits at the original path (a program recreated its
    folder, or a kill landed inside the junction's creation): an empty folder is removed, one
    with content is left alone and the rollback stops, saying where the original data is."""
    src = journal.source
    if not _exists(src) or _is_reparse(src) or _is_original(journal, src):
        return
    if not stat.S_ISDIR(os.stat(long_path(Path(src)), follow_symlinks=False).st_mode):  # noqa: PTH116
        raise RelocateError(
            f"a file now sits at {src}; the original data is safe in {_where_original(journal)}. "
            "Move that file elsewhere and roll back again."
        )
    if _is_empty(src):
        assert_not_real_profile_under_pytest(src, operation="remove the empty folder")
        os.rmdir(long_path(Path(src)))  # noqa: PTH106 -- empty, no streams, not ours
        return
    raise RelocateError(
        f"{src} holds a folder that something else created after the move; the original "
        f"data is safe in {_where_original(journal)}. Move that folder's contents elsewhere, "
        "remove the folder, and roll back again."
    )


def _rebuild_restore(journal: Journal) -> None:
    """The old copy was deleted: make `<name>.reclaim-restore` a proven copy of the target.

    Never destroys what it cannot replace (B2 H3): with the target missing nothing is touched;
    a restore folder of our own that already matches the target is reused; only one that does
    not match (a partial copy from an interrupted rollback) is removed and made again."""
    tgt, restore = journal.target, journal.source + RESTORE_SUFFIX
    if journal.restore_verified:
        journal.restore_verified = False
        journal.save()
    if not _exists(tgt) or _is_reparse(tgt):
        kept = f"; {restore} is kept" if _exists(restore) else ""
        raise RelocateError(
            f"{tgt} is missing and the old copy was deleted, so there is nothing to copy back{kept}"
        )
    if _exists(restore):
        known = journal.restore_id != 0 and _folder_id(restore) == journal.restore_id
        # Killed between saving the intent and recording the new folder's ID: only an empty
        # folder can be that one (B3 L4).
        half_made = journal.restore_planned and journal.restore_id == 0 and _is_empty(restore)
        if not (known or half_made):
            raise RelocateError(f"{restore} was not made by this rollback; inspect it first")
        try:
            _verify_listing(tgt, restore)
            files, _ = _listing(tgt)
            if not _differences(tgt, restore, files):
                journal.restore_id = _folder_id(restore)
                journal.restore_verified = True
                journal.save()
                return
        except (RelocateError, OSError):
            pass
        rmtree_reparse_point_safe(long_path(Path(restore)))
    assert_not_real_profile_under_pytest(restore, operation="create")
    journal.restore_planned = True
    journal.restore_id = 0
    journal.save()
    os.mkdir(long_path(Path(restore)))  # noqa: PTH102
    journal.restore_id = _folder_id(restore)
    journal.save()
    _copy_tree(tgt, restore)
    _copy_folder_meta(long_path(Path(tgt)), long_path(Path(restore)))
    _verify_listing(tgt, restore)
    files, _ = _listing(tgt)
    bad = _differences(tgt, restore, files)
    if bad:
        raise RelocateError(f"{len(bad)} files did not copy back: {_sample(bad)}")
    journal.restore_verified = True
    journal.save()


def _probe_target(target: str) -> None:
    probe_path = target + PROBE_SUFFIX
    if not _exists(target):
        raise NothingChangedError(f"{target} is missing, so the moved data cannot be copied back")
    if _exists(probe_path):
        raise NothingChangedError(f"{probe_path} already exists; inspect it before rolling back")
    try:
        _rename(target, probe_path)
    except OSError as exc:
        raise _TargetInUseError(
            f"{target} is in use: close the program using it, then retry"
        ) from exc
    try:
        _rename(probe_path, target)
    except OSError as exc:  # pragma: no cover -- only a racing writer can recreate `target`
        raise RelocateError(
            f"the probe left the moved folder at {probe_path} ({exc}): rename it back to "
            f"{target}, then roll back again"
        ) from exc


def _discard_copy(journal: Journal) -> tuple[str, str] | None:
    """Removes our copy in the target when that is provably safe, else keeps it and says why.
    Called only once the original is back at its own path (by identity).

    Before the junction existed nothing but this code wrote to the target, so it is removed.
    After, it is removed only if every file in it is byte-identical in the restored source,
    with the same attributes, and every folder has the same attributes and streams there.
    """
    tgt = journal.target
    if _gone(tgt):
        return None
    if _is_reparse(tgt):
        return (tgt, "it is a link now, not the copy this move made")
    if not journal.target_created:
        # Killed between the target's mkdir and the save after it (B2 L7): an empty folder at
        # exactly that name, which the plan had seen absent, is the one we were making.
        if journal.target_planned and _is_empty(tgt):
            assert_not_real_profile_under_pytest(tgt, operation="remove")
            os.rmdir(long_path(Path(tgt)))  # noqa: PTH106
        return None
    if journal.target_id and _folder_id(tgt) != journal.target_id:
        return (tgt, "it is not the folder this move made")
    if journal.junction_attempted:
        try:
            files, dirs = _listing(tgt)
            bad = _differences(tgt, journal.source, files)
            for rel, st in files.items():
                here = os.stat(_join(journal.source, rel), follow_symlinks=False)  # noqa: PTH116
                if (here.st_file_attributes ^ st.st_file_attributes) & _COPIED_ATTRS:
                    bad.append(rel)
            for rel in ["", *sorted(dirs)]:
                there = _join(tgt, rel) if rel else long_path(Path(tgt))
                here_dir = _join(journal.source, rel) if rel else long_path(Path(journal.source))
                if _folder_meta(there) != _folder_meta(here_dir):
                    bad.append(rel or ".")
        except OSError as exc:
            return (tgt, f"it could not be compared with the restored folder: {exc}")
        if bad:
            return (tgt, f"{len(bad)} of its files or folders differ from the restored folder")
    try:
        rmtree_reparse_point_safe(long_path(Path(tgt)))
    except OSError as exc:
        raise RelocateError(
            f"the original is back at {journal.source}, but {tgt} could not be removed ({exc}); "
            "close what holds it open and roll back again"
        ) from exc
    for parent in reversed(journal.created_parents):
        try:
            assert_not_real_profile_under_pytest(parent, operation="remove")
            os.rmdir(long_path(Path(parent)))  # noqa: PTH106 -- only if still empty
        except OSError:
            break
    return None


# ------------------------------------------------------------------ delete the old copy


@_os_errors_as_relocate_errors
def delete_old_copy(
    journal_path: Path,
    *,
    policy: Policy,
    confirmed: bool = False,
    accept_missing: bool = False,
    clock: Callable[[], float] = time.time,
    free_bytes: Callable[[str], int] = _free_bytes,
) -> Outcome:
    """Deletes `<name>.reclaim-moved` once two things are proved.

    1. The old copy is exactly as the move left it: the same files, sizes, mtimes, SHA-256,
       folder attributes and streams as the manifest recorded at the swap. Anything written
       into it since, whatever its timestamps, refuses the delete; Reclaim then never deletes
       it, and the message says to check and remove it yourself.
    2. Every recorded file is in the live folder (the moved folder, or after a rollback the
       restored one) with the same SHA-256, or was rewritten there (a different mtime: a
       program edited it). Different bytes at the same mtime are a bad copy and refuse the
       delete. A file missing there (deleted by a program) refuses it unless `accept_missing`.

    Allowed after `KEEP_OLD_COPY_DAYS`, or earlier with `confirmed`. A delete interrupted
    half-way re-checks what is left of the old copy before carrying on (B5 M3).

    `policy` is required, with no default, so no caller can skip it: the exclusions the user
    has now are applied to the moved folder and to the old copy, and an excluded project's
    original files are never deleted (B7 M3).
    """
    journal = Journal.load(journal_path)
    if _is_inert(journal) and journal.stage == Stage.DELETED_OLD:
        return Outcome(journal, "already rolled back and its old copy deleted; nothing to do")
    if journal.stage in (Stage.DELETING_OLD, Stage.DONE, Stage.ROLLED_BACK):
        _refuse_if_superseded(journal, journal_path)
        _refuse_if_excluded(journal, policy)
    if journal.stage == Stage.DELETING_OLD:
        _require_live_folder(journal)
        changed = _old_copy_changes(journal, allow_missing=True)
        if changed:
            raise RelocateError(_changed_message(journal, changed))
        return _finish_delete(journal, clock, free_bytes)
    if journal.stage not in (Stage.DONE, Stage.ROLLED_BACK):
        raise RelocateError(f"the move is {journal.stage}; nothing to delete")
    age_days = (clock() - _kept_since(journal)) / 86400
    if not confirmed and age_days < KEEP_OLD_COPY_DAYS:
        raise RelocateError(
            f"the old copy is kept for {KEEP_OLD_COPY_DAYS} days after the move "
            f"({age_days:.1f} so far); confirm to delete it sooner"
        )
    moved, src = journal.moved, journal.source
    if not _is_original(journal, moved):
        raise RelocateError(f"{moved} is missing or not the folder this move kept; nothing deleted")
    if journal.stage == Stage.DONE:
        live = journal.target
        pointed = _junction_target(src)
        if pointed is None or _key(pointed) != _key(live):
            raise RelocateError(f"{src} no longer points to {live}; nothing deleted")
    else:
        live = src
        if not (journal.restore_id and _folder_id(src) == journal.restore_id):
            raise RelocateError(f"{src} is not the folder the rollback restored; nothing deleted")
    record = _read_manifest(journal)
    # After Reclaim's own interrupted delete, the files it removed are not changes (B7 L2).
    changed = _old_copy_changes(journal, allow_missing=journal.old_copy_partial, record=record)
    if changed:
        raise RelocateError(_changed_message(journal, changed))
    recorded: dict[str, list[Any]] = record["files"]
    new_files, _ = _listing(live)
    missing, differ = [], []
    for rel, (_size, mtime, digest) in recorded.items():
        new = new_files.get(rel)
        if new is None:
            missing.append(rel)
        elif new.st_mtime_ns == mtime and _sha256(_join(live, rel)) != digest:
            # The copy carries the source's mtime exactly, so an unchanged mtime means nothing
            # rewrote the file: different bytes are a bad copy, not an edit.
            differ.append(rel)
    empty_streams = hashlib.sha256().hexdigest()
    live_dirs = _listing(live)[1] | {""}
    for rel, (_attrs, streams) in record["dirs"].items():
        if streams == empty_streams:
            continue
        here = _join(live, rel) if rel else long_path(Path(live))
        if rel not in live_dirs or _folder_meta(here)[1] != streams:
            missing.append(f"{rel or '.'} (its named streams)")
    if differ:
        raise RelocateError(
            f"{len(differ)} files in {live} differ from the old copy without having been changed "
            f"since the move ({_sample(differ)}); nothing deleted. The old copy is intact"
        )
    if missing and not accept_missing:
        raise RelocateError(
            f"{len(missing)} files of the old copy are no longer in {live} "
            f"({_sample(missing)}); nothing deleted. Accept that to delete anyway."
        )
    journal.old_copy_partial = True
    journal.log(
        Stage.DELETING_OLD,
        f"verified {len(recorded) - len(missing)} files; {len(missing)} accepted as removed",
        clock,
    )
    return _finish_delete(journal, clock, free_bytes)


def _require_live_folder(journal: Journal) -> None:
    """Before the rest of an interrupted delete goes: the folder that holds the other copy of
    those files must be reachable and be the one this move made. The target drive may have been
    unplugged or renamed since, and then the remains of the old copy are the only copy that can
    be reached (B8 L-1). After a rollback the live folder is the restored one at the source."""
    src, tgt = journal.source, journal.target
    if journal.rolled_back:
        live, ok = src, journal.restore_id != 0 and _folder_id(src) == journal.restore_id
    else:
        live = tgt
        pointed = _junction_target(src)
        try:
            # Follows the junction on purpose: is the target reachable through it?
            st = os.stat(long_path(Path(src)))  # noqa: PTH116
        except OSError:
            ok = False
        else:
            ok = (
                pointed is not None
                and _key(pointed) == _key(tgt)
                and stat.S_ISDIR(st.st_mode)
                and journal.target_id != 0
                and st.st_ino == journal.target_id
            )
    if not ok:
        raise RelocateError(
            f"{live} is not reachable (or is not the folder this move made), so what is left of "
            f"{journal.moved} is the only copy of those files that can be reached; nothing more "
            f"is deleted. Reconnect the drive of {live} so that it is at the same path, then "
            f"run `reclaim relocate delete-old {journal.id}` again"
        )


def _refuse_if_excluded(journal: Journal, policy: Policy) -> None:
    """The plan's own test (`Policy.exclusion_match`: the path text, then the names beneath
    it), applied to the moved folder and to the old copy, as the exclusions are now.

    The original path is a junction by now, and `exclusion_match` resolves it to the target's
    path; so the journaled path text is checked as well, without following the junction, and
    so is the old copy's path with its `.reclaim-moved` suffix mapped back to it (B8 M-B)."""
    for path in (journal.source, journal.moved):
        if _exists(path) and policy.exclusion_match(Path(path)) is not None:
            raise RelocateError(
                f"{path} is, or contains, an excluded project; nothing deleted. Take it off the "
                "exclusion list first if the old copy may go"
            )
    if policy.exclusion_match_text is None:
        return
    mapped_back = journal.moved
    if _key(mapped_back).endswith(MOVED_SUFFIX):
        mapped_back = mapped_back[: -len(MOVED_SUFFIX)]
    for path in dict.fromkeys((journal.source, mapped_back, journal.moved)):
        if policy.exclusion_match_text(Path(path)) is not None:
            raise RelocateError(
                f"{path} is, or contains, an excluded project; nothing deleted. Take it off the "
                "exclusion list first if the old copy may go"
            )


def _kept_since(journal: Journal) -> float:
    """When the old copy started being kept: the swap, or, for a swap a kill interrupted
    before DONE, the moment it was journalled."""
    if journal.swapped_at:
        return journal.swapped_at
    times = [float(s[0]) for s in journal.history if s[1] == Stage.SWAPPING.value]  # type: ignore[arg-type]
    return max(times) if times else journal.created_at


def _read_manifest(journal: Journal) -> dict[str, Any]:
    try:
        with open(journal.manifest, encoding="utf-8") as fh:  # noqa: PTH123
            record: dict[str, Any] = json.load(fh)
        if not isinstance(record.get("files"), dict) or not isinstance(record.get("dirs"), dict):
            raise TypeError("not a manifest")  # noqa: TRY301 -- one handler for every bad shape
    except (OSError, ValueError, TypeError) as exc:
        raise RelocateError(
            f"the record of the old copy at the move cannot be read ({exc}); nothing deleted. "
            f"Check {journal.moved} yourself and remove it if you no longer need it"
        ) from exc
    if not record["files"]:
        raise RelocateError(f"{journal.moved} was recorded with no files; nothing deleted")
    return record


def _old_copy_changes(
    journal: Journal, *, allow_missing: bool, record: dict[str, Any] | None = None
) -> list[str]:
    """What in the old copy differs from the swap manifest. `allow_missing`: a delete that was
    interrupted half-way has already removed part of it; only what is left is compared."""
    record = record or _read_manifest(journal)
    recorded: dict[str, list[Any]] = record["files"]
    recorded_dirs: dict[str, list[Any]] = record["dirs"]
    moved = journal.moved
    if _gone(moved):
        return []
    old_files, old_dirs = _listing(moved)
    changed = sorted(set(old_files) - set(recorded))
    if not allow_missing:
        changed += sorted(set(recorded) - set(old_files))
    for rel, st in old_files.items():
        want = recorded.get(rel)
        if want is not None and (
            [st.st_size, st.st_mtime_ns] != want[:2] or _sha256(_join(moved, rel)) != want[2]
        ):
            changed.append(rel)
    for rel in sorted(set(old_dirs) | {""}):
        here = _join(moved, rel) if rel else long_path(Path(moved))
        want = recorded_dirs.get(rel)
        if want is None or list(_folder_meta(here)) != want:
            changed.append(rel or ".")
    if not allow_missing:
        changed.extend(sorted(set(recorded_dirs) - set(old_dirs) - {""}))
    return changed


def _changed_message(journal: Journal, changed: Sequence[str]) -> str:
    if journal.old_copy_partial:
        return (
            f"{len(changed)} files or folders in {journal.moved} differ from the record taken at "
            f"the move ({_sample(changed)}), beyond what Reclaim's own interrupted delete "
            "removed, so Reclaim will not delete it. Check it yourself and remove it if you no "
            "longer need it"
        )
    return (
        f"{len(changed)} files or folders in {journal.moved} changed since the move "
        f"({_sample(changed)}): something wrote into the old copy, so Reclaim will not delete "
        "it. Check it yourself and remove it if you no longer need it"
    )


def _finish_delete(
    journal: Journal, clock: Callable[[], float], free_bytes: Callable[[str], int] = _free_bytes
) -> Outcome:
    if not _gone(journal.moved):
        try:
            rmtree_reparse_point_safe(long_path(Path(journal.moved)))
        except OSError as exc:
            raise RelocateError(
                f"{journal.moved} could not be fully deleted ({exc}); what is left is still the "
                f"old copy minus what went. Close what holds it open and run `reclaim relocate "
                f"delete-old {journal.id}` again"
            ) from exc
    if not _gone(journal.moved):
        raise RelocateError(f"{journal.moved} is still there; nothing was reported deleted")
    journal.source_free_after = free_bytes(_volume_anchor(journal.source))  # not the junction
    freed = journal.source_free_after - journal.source_free_before
    journal.log(Stage.DELETED_OLD, f"volume free space changed by {freed} bytes", clock)
    return Outcome(journal, f"deleted the old copy; the volume gained {freed / 2**30:.2f} GiB")


def journals(journal_dir: Path) -> list[Journal]:
    if not journal_dir.is_dir():
        return []
    found = []
    for path in sorted(journal_dir.glob("relocate-*.json")):
        try:
            found.append(Journal.load(path))
        except (OSError, ValueError, TypeError) as exc:
            raise RelocateError(
                f"the move journal {path} cannot be read ({exc}); it is not changed. Inspect "
                "it, and move it out of the folder if it is not a journal"
            ) from exc
    return found
