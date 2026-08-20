from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ADR reference: docs/AUDIT-2026-08.md, P0-1. `apply_batch`'s real mutation loop
# (`executor.py`) had zero pre-flight probe for either of R6's two required checks before this
# module existed: "is this path currently held open by another process" and "is this path
# hardlink-connected into a DIFFERENT live Python environment than its own". Both are read-only
# detection here -- `executor.apply_batch` owns the decision of what to DO with a positive
# result (skip the item, log it, keep going) and the structlog call; this module only answers
# the two yes/no questions.

PreflightSkipReason = Literal["file_in_use", "hardlink_shared_active_install"]

# --- (a) live-process handle probe -------------------------------------------------------------
#
# Standard Windows technique: request an exclusive-access (`dwShareMode=0`) handle via
# `CreateFileW`. If some other open handle to the same file already exists -- regardless of what
# sharing mode THAT handle itself used -- our exclusive request conflicts with it and
# `CreateFileW` fails with `ERROR_SHARING_VIOLATION` (32). Empirically verified against this
# exact mechanism on this machine before writing the codebase version of it (a plain Python
# `open()` held in the same process, which uses the C runtime's default permissive sharing, still
# reliably triggers `ERROR_SHARING_VIOLATION` against an exclusive-mode probe) -- this is not a
# documentation-only claim.

_GENERIC_READ = 0x80000000
_FILE_SHARE_NONE = 0
_OPEN_EXISTING = 3
# Required to open a HANDLE to a directory at all via CreateFileW (without it, CreateFileW
# refuses any directory target outright) -- only used for the (rare, since directories are
# sampled by probing their top-level FILES, not the directory handle itself) case where a
# caller explicitly asks to probe a directory path directly.
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_ERROR_SHARING_VIOLATION = 32

# Bound on how many of a directory candidate's own top-level files this module will probe.
# Recursively probing every file in a large tree (a `node_modules` with tens of thousands of
# files) would make this pre-flight check itself a new instance of the exact uncached-cost
# problem this audit's P0-3 finding already found elsewhere (Overview/Treemap/Review Queue) --
# so this samples only immediate children, never recurses into subdirectories, and accepts the
# resulting gap explicitly: a locked file several levels deep in a large directory candidate is
# invisible to this specific probe. This is not the only safety net for that case -- the real
# move/delete attempt in `apply_batch`'s existing per-item `try/except` still catches a locked
# file at any depth when the actual mutation is attempted; this probe is a best-effort early skip
# that avoids even trying (and thus avoids writing an intent-manifest entry) for the common case
# of a lock on one of a directory's own direct children, not an exhaustive guarantee.
_MAX_DIRECTORY_TOP_LEVEL_PROBES = 64


def _raw_probe_exclusive_open(path: str, *, is_dir: bool) -> bool:  # pragma: no cover -- real
    # Win32 call; `check_file_in_use`'s own tests exercise this for real (a file genuinely held
    # open in the test process itself), not via monkeypatching this function -- see that test
    # for why a real probe is used instead of the `drives.py`/`elevation.py` monkeypatch-the-
    # raw-call convention: there is no meaningful way to fake "the OS says ERROR_SHARING_VIOLATION"
    # other than actually causing one.
    """True if `CreateFileW(path, ..., dwShareMode=0, ...)` fails with `ERROR_SHARING_VIOLATION`
    -- i.e. some other open handle to `path` currently exists and conflicts with an exclusive
    request. False for every other outcome, INCLUDING "path doesn't exist" (`ERROR_FILE_NOT_FOUND`
    /`ERROR_PATH_NOT_FOUND`) and any other Win32 error -- a vanished or otherwise-inaccessible
    path is a different, already-handled problem (the real mutation attempt's own `try/except`
    surfaces it), not evidence of a live process holding it open.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

    flags = _FILE_FLAG_BACKUP_SEMANTICS if is_dir else 0
    handle = kernel32.CreateFileW(
        path, _GENERIC_READ, _FILE_SHARE_NONE, None, _OPEN_EXISTING, flags, None
    )
    # HANDLE marshals through ctypes as an unsigned pointer-sized int; -1 (INVALID_HANDLE_VALUE)
    # must be computed the same way ctypes itself represents it, not hardcoded, or a 32-bit vs.
    # 64-bit sign/width mismatch silently breaks this comparison. Verified empirically against a
    # live CreateFileW failure before relying on it.
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        return ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


def _top_level_file_probe_targets(path: Path, *, limit: int) -> list[Path]:
    """Up to `limit` of `path`'s own directly-contained FILES (never subdirectories, never
    recursed into) -- see `_MAX_DIRECTORY_TOP_LEVEL_PROBES` for why this is bounded and why it's
    a deliberate, documented gap rather than an oversight. Returns `[]` (not a raised exception)
    if `path` itself can't be listed (permission error, already gone) -- the real mutation
    attempt still surfaces that condition on its own; this probe simply has nothing to add.
    """
    try:
        entries = list(os.scandir(path))
    except OSError:
        return []
    files = [Path(entry.path) for entry in entries if entry.is_file(follow_symlinks=False)]
    return files[:limit]


def check_file_in_use(
    path: Path, *, is_dir: bool, max_directory_samples: int = _MAX_DIRECTORY_TOP_LEVEL_PROBES
) -> bool:
    """R6 pre-flight check (a): true if `path` (a plain file) or, for a directory candidate, one
    of its own bounded top-level file samples (see `_top_level_file_probe_targets`) is currently
    held open by another process in a way that denies exclusive access.

    Fails OPEN (returns `False`, never raises) on any probe error for an individual candidate --
    e.g. a permission error listing a directory's top level. This is deliberate, not a violation
    of "guards fail closed" (house rule 98a): this probe is a best-effort EARLY skip layered on
    top of `apply_batch`'s existing per-item `try/except`, which already treats any genuine
    filesystem error during the real move/delete as a failed (not silently succeeded) item --
    nothing here can cause a locked file to be silently reported as successfully removed.
    """
    if not is_dir:
        return _raw_probe_exclusive_open(str(path), is_dir=False)
    return any(
        _raw_probe_exclusive_open(str(candidate_file), is_dir=False)
        for candidate_file in _top_level_file_probe_targets(path, limit=max_directory_samples)
    )


# --- (b) hardlink-into-active-install probe ---------------------------------------------------
#
# `FindFirstFileNameW`/`FindNextFileNameW` enumerate every name (on the same volume) pointing at
# one file's inode -- confirmed feasible and requiring no elevation via direct testing on this
# machine before committing to this design (unlike the process-lock probe above, ordinary user
# privileges are sufficient).

_HARDLINK_NAME_BUFFER_CHARS = 32768  # NTFS's own maximum path length -- generous enough for any
# real hardlink sibling name either Win32 call can return on this volume; re-allocated fresh per
# call rather than shared/reused, since these are cheap, infrequent, per-candidate calls, not a
# hot loop.


def _raw_enumerate_hardlink_names(path: str) -> list[str]:  # pragma: no cover -- real Win32
    # call; `check_hardlink_shared_active_install`'s own tests exercise this for real (a real
    # `os.link()`-created hardlink group), same reasoning as `_raw_probe_exclusive_open` above.
    """Every name (volume-root-relative, e.g. `\\Users\\...\\file.bin`) pointing at `path`'s
    inode, via `FindFirstFileNameW`/`FindNextFileNameW`. Returns `[]` if the initial
    `FindFirstFileNameW` call fails for any reason (path vanished, not on an NTFS volume, no
    hardlinks at all) -- callers must treat an empty result as "nothing usable was enumerated",
    never as "confirmed zero siblings" (house rule 98a: a probe that can't verify must not be
    read as a positive absence)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstFileNameW.restype = ctypes.wintypes.HANDLE
    kernel32.FindFirstFileNameW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
        ctypes.wintypes.LPWSTR,
    ]
    kernel32.FindNextFileNameW.restype = ctypes.wintypes.BOOL
    kernel32.FindNextFileNameW.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ctypes.wintypes.DWORD),
        ctypes.wintypes.LPWSTR,
    ]
    kernel32.FindClose.restype = ctypes.wintypes.BOOL
    kernel32.FindClose.argtypes = [ctypes.wintypes.HANDLE]

    names: list[str] = []
    buf_len = ctypes.wintypes.DWORD(_HARDLINK_NAME_BUFFER_CHARS)
    buf = ctypes.create_unicode_buffer(_HARDLINK_NAME_BUFFER_CHARS)
    handle = kernel32.FindFirstFileNameW(path, 0, ctypes.byref(buf_len), buf)
    invalid_handle = ctypes.c_void_p(-1).value  # see `_raw_probe_exclusive_open` for why this
    # must be computed via ctypes itself rather than hardcoded.
    if handle == invalid_handle:
        return names
    try:
        names.append(buf.value)
        while True:
            next_len = ctypes.wintypes.DWORD(_HARDLINK_NAME_BUFFER_CHARS)
            next_buf = ctypes.create_unicode_buffer(_HARDLINK_NAME_BUFFER_CHARS)
            if not kernel32.FindNextFileNameW(handle, ctypes.byref(next_len), next_buf):
                # Expected termination is ERROR_HANDLE_EOF (38); any other error is treated the
                # same way (stop enumerating with whatever was found so far) since a partial,
                # honestly-partial result is safer than looping or raising here.
                break
            names.append(next_buf.value)
    finally:
        kernel32.FindClose(handle)
    return names


def _enumerate_hardlink_siblings(path: Path) -> list[Path]:
    """Real `Path` objects for every name sharing `path`'s inode, including `path`'s own name.
    `[]` if `path` has no drive letter (e.g. a relative path in a test fixture that never reaches
    real Win32 enumeration) or the underlying enumeration call found nothing usable."""
    drive = path.drive
    if not drive:
        return []
    return [Path(f"{drive}{name}") for name in _raw_enumerate_hardlink_names(str(path))]


@dataclass(frozen=True, slots=True)
class HardlinkShareCheck:
    """Outcome of `check_hardlink_shared_active_install`, kept structured (not just a bool) so
    `executor.py`'s structlog call can log which environment roots were actually found, not just
    that the check fired."""

    is_shared_with_other_environment: bool
    own_environment_root: Path | None
    # Distinct OTHER environment roots this candidate's hardlink siblings resolve into -- empty
    # whenever `is_shared_with_other_environment` is False.
    sibling_environment_roots: tuple[Path, ...]


def check_hardlink_shared_active_install(path: Path) -> HardlinkShareCheck:
    """R6 pre-flight check (b) / audit P0-1: true if `path` is hardlink-connected (same NTFS
    inode) to at least one sibling name that resolves into a DIFFERENT, currently-recognizable
    live Python environment than `path`'s own -- reusing `dedup._environment_root`'s exact same 5
    structural signals (conda-meta/, pyvenv.cfg, interpreter binary, Scripts/bin + interpreter,
    Lib/site-packages) rather than reimplementing environment detection. `path`'s own environment
    root (`None` if `path` isn't itself inside any recognized environment -- e.g. a package-cache
    blob) is compared against every sibling's; no explicit self-exclusion of `path` from its own
    sibling list is needed, since `path`'s own root trivially equals `own_environment_root` and
    is filtered out by the inequality check below.

    IMPORTANT (see this module's own docstring and `executor.py`'s call site): deleting `path`
    itself is NOT destructive to any hardlink sibling by construction -- an `unlink` only
    decrements the shared inode's reference count, every surviving name still resolves to the
    identical bytes. This check is defense-in-depth specifically for an UNATTENDED/autonomous
    apply, not because the delete itself corrupts anything -- see `executor.py`'s call site for
    why a human-confirmed apply is a different risk posture this audit does not require blocking.
    Fails safe on an unresolvable `path` (returns `is_shared_with_other_environment=False` if
    `path` can't be stat'd at all) -- an already-vanished candidate is `apply_batch`'s existing
    per-item `try/except`'s problem to report, not this probe's.
    """
    try:
        nlink = path.stat().st_nlink
    except OSError:
        return HardlinkShareCheck(False, None, ())
    if nlink <= 1:
        return HardlinkShareCheck(False, None, ())

    # Local import: `reclaim.dedup` is a heavier module (blake3/index/safety-aware duplicate
    # detection) than this small pre-flight probe needs at import time everywhere `executor.py`
    # is imported; deferred here keeps this module's own import cost to "just ctypes".
    from reclaim.dedup import _environment_root

    own_root = _environment_root(path)
    other_roots: list[Path] = []
    seen: set[Path] = set()
    for sibling in _enumerate_hardlink_siblings(path):
        sibling_root = _environment_root(sibling)
        if sibling_root is not None and sibling_root != own_root and sibling_root not in seen:
            seen.add(sibling_root)
            other_roots.append(sibling_root)

    return HardlinkShareCheck(
        is_shared_with_other_environment=bool(other_roots),
        own_environment_root=own_root,
        sibling_environment_roots=tuple(other_roots),
    )
