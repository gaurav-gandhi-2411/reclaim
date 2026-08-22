from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reclaim.models import FILE_ATTRIBUTE_REPARSE_POINT

# ADR reference: docs/AUDIT-2026-08.md, P0-1. `apply_batch`'s real mutation loop
# (`executor.py`) had zero pre-flight probe for either of R6's two required checks before this
# module existed: "is this path currently held open by another process" and "is this path
# hardlink-connected into a DIFFERENT live Python environment than its own". Both are read-only
# detection here -- `executor.apply_batch` owns the decision of what to DO with a positive
# result (skip the item, log it, keep going) and the structlog call; this module only answers
# the two yes/no questions.
#
# P0-K1a (this session, live-reproduced finding): `apply_batch` acted on stale DB-index data
# with zero re-verification against live filesystem state at mutation time -- swapping content
# at a candidate's path between scan and apply caused the swapped content to be permanently
# deleted or misrouted into the vault. `check_identity_unchanged_since_scan` below is the third
# read-only probe this module answers, same posture as the two above: it only says whether the
# live path's identity still matches what the scan recorded, never what to do about a mismatch.
#
# AE1 (this session, live-reproduced finding): identity re-verification answers "is this still
# the same file", never "does this user have any legitimate claim on it" -- a DIFFERENT question.
# PR #47 fixed SCAN-time scope (a new scan won't reach another user's files), but did nothing
# about candidates already sitting in a PERSISTED index from before that fix shipped -- a real
# `ReclaimSmokeTest` profile with an old pre-#47 whole-drive-scan index still offered a one-click
# "Clean these now" against 13,991 other-users'-files candidates, and neither identity
# re-verification nor anything else in the apply path would have refused them.
# `check_within_allowed_scope` below is the fourth read-only probe this module answers: whether
# a path falls within a set of roots the CURRENT operation is actually scoped to, independent of
# whether the path's on-disk identity matches what a scan recorded for it.

PreflightSkipReason = Literal[
    "file_in_use",
    "hardlink_shared_active_install",
    "identity_changed_since_scan",
    "outside_user_scope",
]

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


# --- (c) identity-since-scan probe (P0-K1a) ----------------------------------------------------
#
# `os.stat(path, follow_symlinks=False)`'s `(st_dev, st_ino)` pair is the file ID Windows/NTFS
# itself uses to identify a filesystem entry independent of its name -- confirmed empirically
# this session against a real NTFS junction (built and repointed with `mklink /J`, not mocked):
# `follow_symlinks=False` already gives correct, reparse-point-aware, by-handle identity
# semantics through CPython's own `os.stat` implementation, with no `ctypes`/
# `GetFileInformationByHandle` needed to get an equivalent of `FILE_FLAG_OPEN_REPARSE_POINT`.
#
# Residual gaps (house rule 98a: name every control's surface explicitly) -- disclosed here,
# verbatim, and in this PR's body, never softened:
#   1. NTFS MFT sequence-number reuse is a real-but-practically-ignorable residual risk: the
#      64-bit `st_ino` NTFS returns packs a 48-bit file-record number with a 16-bit sequence
#      number that increments each time that exact MFT record is reused for a new file; a
#      collision requires ~65,536 forced reuse cycles of the SAME MFT record inside one
#      scan-to-apply window, which is not a practical attack surface for a tool whose apply
#      typically runs minutes to hours after its scan.
#   2. Same-inode in-place content edits (no rename/recreate -- e.g. a process opens the
#      existing file and overwrites its bytes without ever unlinking it) are NOT caught by this
#      check, by design: `(dev, ino)` is unchanged by construction for an in-place edit, and
#      this is a different threat class (data-integrity-of-contents, not
#      wrong-file-deleted/misrouted) than what this fix addresses. Out of scope.
#   3. `FSCTL_SET_REPARSE_POINT`-based in-place reparse-point retargeting (rewriting an
#      existing junction/symlink's target without deleting and recreating the reparse point
#      itself, so the same MFT record -- and therefore the same `(dev, ino)` -- survives the
#      retarget) is a theoretical, untested residual gap: this check would see an unchanged
#      identity and not fire, even though the path now resolves somewhere else. Untested because
#      constructing this exact retarget (as opposed to the delete-and-recreate junction repoint
#      this session's real test used) needs a raw `DeviceIoControl` call this fix does not add.


@dataclass(frozen=True, slots=True)
class IdentityCheck:
    """Outcome of `check_identity_unchanged_since_scan` -- kept structured (not a bare bool) so
    `executor.py`'s structlog call can log the recorded-vs-live values for a human to inspect,
    not just that the check fired. `live_dev`/`live_ino`/`live_mtime`/`live_size_bytes` are
    `None` only when `path` couldn't be stat'd at all (already vanished between scan and apply)
    -- a different, already-handled condition (the real move/delete attempt's own `try/except`
    reports it), not itself a reason to flag `identity_changed=True` here.

    `recorded_mtime`/`live_mtime`/`recorded_size_bytes`/`live_size_bytes` are supplementary
    DIAGNOSTIC context only, logged for a human reviewing a skip -- never part of the
    `identity_changed` decision itself. `(dev, ino)` alone is the authoritative identity signal;
    requiring mtime to also match would incorrectly flag a same-inode in-place content edit as
    an "identity change", which residual-gap #2 above documents as deliberately OUT OF SCOPE for
    this check, not something to accidentally start catching as a side effect of a stricter
    comparison.
    """

    identity_changed: bool
    recorded_dev: int
    recorded_ino: int
    live_dev: int | None
    live_ino: int | None
    recorded_mtime: float
    live_mtime: float | None
    recorded_size_bytes: int
    live_size_bytes: int | None


def check_identity_unchanged_since_scan(
    path: Path,
    *,
    recorded_dev: int,
    recorded_ino: int,
    recorded_mtime: float,
    recorded_size_bytes: int,
) -> IdentityCheck:
    """R6/P0-K1a pre-flight check (c): true (`identity_changed=True`) if `path`'s live
    `(st_dev, st_ino)` no longer matches `recorded_dev`/`recorded_ino` -- the scan-time baseline
    a caller must supply (this module has no index/DB access of its own; see
    `executor._preflight_skip_reason` for where the baseline comes from).

    Fails safe toward "not a mismatch" (returns `identity_changed=False`) when `path` can no
    longer be stat'd at all -- a vanished path is `apply_batch`'s existing per-item
    `try/except`'s problem to report as a failed item, not this probe's; manufacturing a
    skip_reason for a condition that already has its own honest failure path would just make
    that failure harder to find in the logs, not safer.
    """
    try:
        st = path.stat(follow_symlinks=False)
    except OSError:
        return IdentityCheck(
            identity_changed=False,
            recorded_dev=recorded_dev,
            recorded_ino=recorded_ino,
            live_dev=None,
            live_ino=None,
            recorded_mtime=recorded_mtime,
            live_mtime=None,
            recorded_size_bytes=recorded_size_bytes,
            live_size_bytes=None,
        )
    return IdentityCheck(
        identity_changed=(st.st_dev != recorded_dev or st.st_ino != recorded_ino),
        recorded_dev=recorded_dev,
        recorded_ino=recorded_ino,
        live_dev=st.st_dev,
        live_ino=st.st_ino,
        recorded_mtime=recorded_mtime,
        live_mtime=st.st_mtime,
        recorded_size_bytes=recorded_size_bytes,
        live_size_bytes=st.st_size,
    )


# --- (d) ownership/scope check (AE1) ------------------------------------------------------------
#
# Distinct from identity re-verification above by design, not folded into it: identity answers
# "is this still the same file the scan recorded", which says nothing about whether the invoking
# user has any legitimate claim on it at all. A file can pass identity re-verification perfectly
# (nothing about it changed since the scan) and still belong to a different user entirely -- the
# real, live-reproduced case this check exists for: a persisted scan index from before PR #47's
# scan-scope fix still contains real candidates under other users' profiles, and nothing about
# THEIR identity ever changes just because a newer, better-scoped version of this tool shipped.


def check_within_allowed_scope(path: Path, *, allowed_roots: Sequence[Path]) -> bool:
    """True if `path` is one of `allowed_roots` itself, or nested under one of them.

    `resolve()` (not a raw string-prefix compare) so `..`/relative segments and case/short-name
    differences can't produce a false "in scope" -- same reasoning `cli._under_root` already
    established for the CLI's own `--path`-scoped apply filter. Deliberately does NOT require any
    of `path`/`allowed_roots` to exist on disk (`resolve()` doesn't require that) -- a scan-index
    row must be checkable even if the live file has since vanished; that's a different, already-
    handled failure mode (`apply_batch`'s per-item `try/except`), not this check's problem.

    `allowed_roots` is caller-supplied on purpose -- this module has no notion of "the current
    user" or "the current scan's opted-in root" of its own; see `executor._preflight_skip_reason`
    / `api.service._cached_all_candidates` for where those are actually resolved (`Path.home()`
    plus, when set, the live scan's own explicitly-confirmed root -- PR #47 already requires
    explicit confirmation before any scan reaches outside `Path.home()` in the first place, so a
    non-home `scan_status.root` being set at all IS the "explicitly opted-in" signal, not a
    separate flag this check needs to know about).
    """
    resolved_path = path.resolve()
    for root in allowed_roots:
        resolved_root = root.resolve()
        if resolved_path == resolved_root or resolved_root in resolved_path.parents:
            return True
    return False


# --- (e) batched directory-identity enumeration (P0-K1a M1 cost-budget fix) --------------------
#
# M1's full-subtree re-walk (`executor._live_subtree_records`) used to call `os.stat()` once per
# entry (via `scanner.build_record`) -- on Windows this means one real
# `CreateFile`+`GetFileInformationByHandle`+`CloseHandle` cycle PER ENTRY, since `st_ino`/`st_dev`
# require a real by-handle query that `os.DirEntry.stat()`'s cached `FindNextFile` data doesn't
# carry (see `scanner.build_record`'s own comment on this exact tradeoff). Measured real cost
# against the actual worst-case direct-delete candidate reachable on this machine
# (`%LOCALAPPDATA%\npm-cache`, 88,864 files): 17-28s added to a real apply, ~3.4x over this fix's
# own 10% cost budget (PLAN.md's 2026-08-21 checkpoint).
#
# `GetFileInformationByHandleEx`/`FileIdBothDirectoryInfo` is the batched replacement: ONE open
# directory HANDLE (`CreateFileW` + `FILE_FLAG_BACKUP_SEMANTICS`, same requirement
# `_raw_probe_exclusive_open` above already documents for opening a directory handle at all), then
# a small, buffer-growth-bounded number of `GetFileInformationByHandleEx` calls return EVERY
# child's `FileId` (the same 64-bit NTFS file-record identity `st_ino` exposes), size, timestamps,
# and attributes in one shot -- zero per-child `CreateFile`/open operations. `FileId` equivalence
# to `os.stat().st_ino` for the same file is empirically confirmed against a real fixture, not
# assumed -- see `evals/test_apply_safety_preflight.py::
# test_batch_enumerated_file_id_matches_os_stat_ino_for_real_files`.

_FILE_ID_BOTH_DIRECTORY_INFO_CLASS = 10  # FILE_INFO_BY_HANDLE_CLASS.FileIdBothDirectoryInfo
_ERROR_NO_MORE_FILES = 18
_ERROR_MORE_DATA = 234
# Deliberately permissive (unlike `_raw_probe_exclusive_open`'s exclusive `dwShareMode=0` above):
# this is a read-only directory LISTING, not a lock probe, and must not itself interfere with any
# concurrent access to the tree it's reading.
_FILE_SHARE_READ_WRITE_DELETE = 0x00000001 | 0x00000002 | 0x00000004
_DIR_ENUM_INITIAL_BUFFER_BYTES = 64 * 1024
# A single filename would need to be ~8M UTF-16 characters to still not fit at this size (NTFS's
# own max path component length is 255 characters) -- this cap exists only to bound a pathological
# retry loop, never expected to actually bind in practice.
_DIR_ENUM_MAX_BUFFER_BYTES = 16 * 1024 * 1024
# Not already defined in `models.py` (only `FILE_ATTRIBUTE_REPARSE_POINT`/
# `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` are) -- local since this is the only place in this module
# that needs it.
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010


class _FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
    """The fixed-size portion of `FILE_ID_BOTH_DIR_INFO` (winnt.h) -- `FileName` is a variable-
    length trailing array, read manually from the raw buffer at `ctypes.sizeof(this)` using
    `FileNameLength`, not declared as a ctypes field. Field layout (offsets, alignment) verified
    empirically via `ctypes.sizeof`/`.offset` before relying on it: this layout gives `FileId` at
    offset 96 and a total fixed size of 104 bytes, matching every public reference for this
    struct -- and further confirmed by the real FileId-vs-`st_ino` equivalence test this module's
    own docstring above points to (a wrong offset would read garbage, not a plausible-looking
    wrong inode, so that test is a real check on this layout too, not just on the API choice)."""

    _fields_ = [
        ("NextEntryOffset", ctypes.wintypes.ULONG),
        ("FileIndex", ctypes.wintypes.ULONG),
        ("CreationTime", ctypes.wintypes.LARGE_INTEGER),
        ("LastAccessTime", ctypes.wintypes.LARGE_INTEGER),
        ("LastWriteTime", ctypes.wintypes.LARGE_INTEGER),
        ("ChangeTime", ctypes.wintypes.LARGE_INTEGER),
        ("EndOfFile", ctypes.wintypes.LARGE_INTEGER),
        ("AllocationSize", ctypes.wintypes.LARGE_INTEGER),
        ("FileAttributes", ctypes.wintypes.ULONG),
        ("FileNameLength", ctypes.wintypes.ULONG),
        ("EaSize", ctypes.wintypes.ULONG),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", ctypes.c_wchar * 12),
        ("FileId", ctypes.wintypes.LARGE_INTEGER),
    ]


@dataclass(frozen=True, slots=True)
class DirectoryEntryIdentity:
    """One child of `enumerate_directory_identity`'s target directory -- `.`/`..` already
    filtered out. `ino` is normalized to the same UNSIGNED 64-bit range Python's
    `os.stat().st_ino` uses (Win32's `FileId` is a signed `LARGE_INTEGER`; a top-bit-set
    file-record number would otherwise come back negative and never compare equal to `st_ino`)."""

    name: str
    attributes: int
    ino: int
    size_bytes: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & _FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def enumerate_directory_identity(path: str) -> list[DirectoryEntryIdentity] | None:  # pragma: no
    # cover -- real Win32 call; exercised for real via a real directory fixture in
    # `evals/test_apply_safety_preflight.py`, same reasoning as `_raw_probe_exclusive_open`/
    # `_raw_enumerate_hardlink_names` above: there's no meaningful way to fake a real
    # `GetFileInformationByHandleEx` result other than actually calling it.
    r"""Every child of `path` (files AND subdirectories), each carrying its NTFS file-record
    identity, attributes, and size -- read via a SINGLE open directory handle and a small,
    buffer-growth-bounded number of `GetFileInformationByHandleEx` calls, never one real
    `CreateFile`/open PER CHILD. `.`/`..` (always present in this enumeration, same as
    `FindFirstFile`/`FindNextFile`) are filtered out before returning. `path` should already be
    `\\?\`-prefixed by the caller for MAX_PATH safety (see `scanner.long_path`) -- this function
    itself does no path preparation, same convention `_raw_probe_exclusive_open` above uses.

    Returns `None` if `path` itself can't be opened as a directory handle at all (permission
    error, vanished, not a directory) -- callers already treat a whole-directory listing failure
    as "skip this subtree" (see `executor._live_subtree_records`'s own docstring for why that's
    the safe direction), same as the plain `os.scandir` failure this replaces. Returns `[]` for a
    genuinely empty directory (still opens and enumerates fine -- `.`/`..` are the only entries,
    and both are filtered).
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
    kernel32.GetFileInformationByHandleEx.restype = ctypes.wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
    ]

    handle = kernel32.CreateFileW(
        path,
        _GENERIC_READ,
        _FILE_SHARE_READ_WRITE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    # See `_raw_probe_exclusive_open` above for why INVALID_HANDLE_VALUE must be computed via
    # ctypes itself rather than hardcoded.
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        return None

    results: list[DirectoryEntryIdentity] = []
    try:
        buf_size = _DIR_ENUM_INITIAL_BUFFER_BYTES
        buf = ctypes.create_string_buffer(buf_size)
        header_size = ctypes.sizeof(_FILE_ID_BOTH_DIR_INFO)
        while True:
            ok = kernel32.GetFileInformationByHandleEx(
                handle, _FILE_ID_BOTH_DIRECTORY_INFO_CLASS, buf, buf_size
            )
            if not ok:
                err = ctypes.get_last_error()
                if err == _ERROR_NO_MORE_FILES:
                    break
                if err == _ERROR_MORE_DATA and buf_size < _DIR_ENUM_MAX_BUFFER_BYTES:
                    # A single entry (almost always one with a long filename) didn't fit in the
                    # current buffer -- nothing was consumed by a failed call, so growing and
                    # retrying resumes from the exact same enumeration position, not a repeat or
                    # a skip.
                    buf_size = min(buf_size * 2, _DIR_ENUM_MAX_BUFFER_BYTES)
                    buf = ctypes.create_string_buffer(buf_size)
                    continue
                # Any other failure: stop with whatever was gathered so far -- same conservative
                # posture `_live_subtree_records`'s pre-existing `except OSError: continue` used
                # (a directory this walk can't finish listing degrades this check's coverage for
                # whatever sits below it, never silently approves a mutation).
                break

            raw = buf.raw
            offset = 0
            while True:
                entry = _FILE_ID_BOTH_DIR_INFO.from_buffer_copy(raw, offset)
                name_start = offset + header_size
                name = raw[name_start : name_start + entry.FileNameLength].decode("utf-16-le")
                if name not in (".", ".."):
                    ino = entry.FileId if entry.FileId >= 0 else entry.FileId + (1 << 64)
                    results.append(
                        DirectoryEntryIdentity(
                            name=name,
                            attributes=entry.FileAttributes,
                            ino=ino,
                            size_bytes=entry.EndOfFile,
                        )
                    )
                if entry.NextEntryOffset == 0:
                    break
                offset += entry.NextEntryOffset
    finally:
        kernel32.CloseHandle(handle)
    return results
