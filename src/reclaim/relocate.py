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

import ctypes
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


class Verdict(StrEnum):
    MOVABLE = "movable"
    GUIDED = "guided"  # movable only through the owning program's own steps (Docker, WSL)
    MUST_STAY = "must_stay"
    ALREADY_MOVED = "already_moved"


class Stage(StrEnum):
    PLANNED = "planned"
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
    history: list[list[object]] = field(default_factory=list)
    path: str = ""

    def log(self, stage: Stage, note: str = "", clock: Callable[[], float] = time.time) -> None:
        self.stage = stage.value
        self.history.append([round(clock(), 3), stage.value, note])
        self.save()
        logger.info("relocate_step", id=self.id, stage=stage.value, note=note)

    def save(self) -> None:
        data = asdict(self)
        data.pop("path")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:  # noqa: PTH123 -- str path, see module note
            json.dump(data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)  # noqa: PTH105 -- str paths, see module note

    @classmethod
    def load(cls, path: Path | str) -> Journal:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            data = json.load(fh)
        journal = cls(**data)
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
    if text.startswith("\\\\?\\"):
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


def _free_bytes(path: str) -> int:
    current = Path(_norm(path))
    while not _exists(str(current)) and current.parent != current:
        current = current.parent
    return shutil.disk_usage(long_path(current)).free


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


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:  # noqa: PTH123
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
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
    with open(src, "rb") as fi, open(dst, "wb") as fo:  # noqa: PTH123
        st = os.fstat(fi.fileno())
        while chunk := fi.read(_CHUNK):
            fo.write(chunk)
        fo.flush()
        os.fsync(fo.fileno())
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    _set_attributes(dst, st.st_file_attributes)


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
            _set_attributes(dst, entry.stat(follow_symlinks=False).st_file_attributes)
        else:
            _copy_file(entry.path, dst)
            if checkpoint is not None:
                checkpoint("copy_file")


def _differences(a_root: str, b_root: str, rels: Iterable[str]) -> list[str]:
    """Relative paths whose SHA-256 differs between the two trees (or that `b` lacks)."""
    bad = []
    for rel in rels:
        b = _join(b_root, rel)
        if not os.path.lexists(b) or _sha256(_join(a_root, rel)) != _sha256(b):
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
            return Classification(Verdict.GUIDED, (steps,))
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


def plan_move(
    source: Path | str,
    target: Path | str,
    policy: Policy,
    *,
    free_bytes: Callable[[str], int] = _free_bytes,
    run_probe: bool = True,
) -> Plan:
    """The dry run. Changes nothing (the probe renames and renames back)."""
    src, tgt = _norm(source), _norm(target)
    moved = src + MOVED_SUFFIX
    found = classify(src, policy)
    reasons = list(found.reasons) if found.verdict is not Verdict.MOVABLE else []
    if found.verdict is Verdict.ALREADY_MOVED:
        reasons = list(found.reasons)
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
        reasons.append(f"{moved} already exists (left by an earlier move?): inspect it first")
    for leftover in (src + PROBE_SUFFIX, src + RESTORE_SUFFIX):
        if _exists(leftover):
            reasons.append(f"{leftover} already exists (left by an earlier run?): inspect it first")
    src_vol, tgt_vol = _volume_serial(src), _volume_serial(tgt)
    if src_vol is None or tgt_vol is None:
        reasons.append("the volume of the source or target could not be read")
    elif src_vol == tgt_vol:
        reasons.append("the target is on the same volume, so nothing would be freed")
    target_free = free_bytes(tgt)
    source_free = free_bytes(src) if _exists(src) else 0
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
    killed mid-way leaves its journal for `recover`."""
    plan = plan_move(source, target, policy, free_bytes=free_bytes, run_probe=True)
    if not plan.ok:
        raise RelocateError("not moved: " + "; ".join(plan.reasons))
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
    )
    journal.path = str(journal_dir / f"relocate-{journal.id}.json")
    journal.log(Stage.PLANNED, f"{plan.files} files, {plan.bytes} bytes", clock)
    hook = checkpoint or (lambda _name: None)
    try:
        _make_target(journal)
        journal.copy_started_ns = time.time_ns()
        journal.log(Stage.COPYING, "", clock)
        _copy_tree(plan.source, plan.target, checkpoint=hook)
        hook("after_copy")
        note = _verify_copy(plan.source, plan.target, journal.copy_started_ns)
        journal.log(Stage.COPIED, note, clock)
        hook("after_verify")
        journal.log(Stage.FREEZING, "", clock)
        try:
            _rename(plan.source, plan.moved)
        except OSError as exc:
            raise InUseError(f"{plan.source} came into use during the copy: retry later") from exc
        journal.log(Stage.FROZEN, "", clock)
        hook("after_freeze")
        journal.log(Stage.SYNCING, "", clock)
        note = _delta_sync(plan.moved, plan.target, journal.copy_started_ns, checkpoint=hook)
        journal.log(Stage.SYNCED, note, clock)
        hook("after_delta")
        journal.junction_attempted = True
        journal.log(Stage.SWAPPING, "", clock)
        _create_junction(plan.source, plan.target)
        hook("after_junction")
        _post_swap_check(journal)
        journal.swapped_at = clock()
        journal.log(Stage.DONE, f"old copy kept at {plan.moved}", clock)
    except Exception as exc:
        logger.warning("relocate_failed", id=journal.id, stage=journal.stage, error=str(exc))
        outcome = rollback(Path(journal.path), clock=clock)
        raise RelocateError(f"move failed and was rolled back ({exc}). {outcome.message}") from exc
    return Outcome(journal, f"moved; {plan.source} now points to {plan.target}")


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
    os.mkdir(long_path(Path(journal.target)))  # noqa: PTH102
    journal.target_created = True
    journal.save()


def _verify_copy(src: str, dst: str, copy_started_ns: int) -> str:
    """Compares the copy with the still-live source, by SHA-256 of every file.

    A file the source changed, added or removed since the copy started is not a failure: the
    delta pass re-copies and re-hashes it once the source is frozen. Any other difference (the
    same size and an older timestamp, but other bytes) is a bad copy and stops the move."""
    src_files, _ = _listing(src)
    dst_files, _ = _listing(dst)
    window = copy_started_ns - _DELTA_WINDOW_NS
    deferred, checked = [], []
    for rel, st in src_files.items():
        copy = dst_files.get(rel)
        if (
            copy is None
            or st.st_mtime_ns >= window
            or copy.st_size != st.st_size
            or copy.st_mtime_ns != st.st_mtime_ns
        ):
            deferred.append(rel)
        else:
            checked.append(rel)
    bad = _differences(src, dst, checked)
    if bad:
        raise RelocateError(f"{len(bad)} copied files differ from their source: {_sample(bad)}")
    extra = len(set(dst_files) - set(src_files))
    return (
        f"SHA-256 equal for {len(checked)} files; {len(deferred)} changed during the copy and "
        f"{extra} were removed, left to the re-sync"
    )


def _delta_sync(
    moved: str,
    target: str,
    copy_started_ns: int,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> str:
    """Brings the target to the frozen source exactly: what was created, changed or deleted
    during the copy window. Removes from the target only files the source no longer has (our
    own copies of them). Every file written since the copy started is copied and re-hashed."""
    src_files, src_dirs = _listing(moved)
    dst_files, dst_dirs = _listing(target)
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
        os.makedirs(_join(target, rel), exist_ok=True)  # noqa: PTH103
    for rel in recopy:
        _copy_file(_join(moved, rel), _join(target, rel))
        if checkpoint is not None:
            checkpoint("delta_file")
    gone = sorted(set(dst_files) - set(src_files))
    for rel in gone:
        path = _join(target, rel)
        assert_not_real_profile_under_pytest(path, operation="delete the copy")
        os.chmod(path, stat.S_IWRITE)  # noqa: PTH101
        os.unlink(path)  # noqa: PTH108
    for rel in sorted(dst_dirs - src_dirs, key=len, reverse=True):
        assert_not_real_profile_under_pytest(_join(target, rel), operation="remove")
        os.rmdir(_join(target, rel))  # noqa: PTH106
    _verify_listing(moved, target)
    bad = _differences(moved, target, recopy)
    if bad:
        raise RelocateError(f"{len(bad)} files differ after the re-sync: {_sample(bad)}")
    return f"re-copied {len(recopy)}, removed {len(gone)} from the copy"


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


def recover(journal_path: Path, *, clock: Callable[[], float] = time.time) -> Outcome:
    """After a crash: finish a move whose junction is in place and checks out, else roll back."""
    journal = Journal.load(journal_path)
    if journal.stage in {Stage.DONE, Stage.DELETED_OLD, Stage.ROLLED_BACK}:
        return Outcome(journal, f"nothing to recover: the move is {journal.stage}")
    if journal.stage == Stage.DELETING_OLD:
        return _finish_delete(journal, clock)
    if journal.stage == Stage.SWAPPING and _junction_target(journal.source) is not None:
        try:
            _post_swap_check(journal)
        except (RelocateError, OSError) as exc:
            logger.warning("relocate_recover_check_failed", id=journal.id, error=str(exc))
        else:
            journal.swapped_at = clock()
            journal.log(Stage.DONE, "finished after an interruption", clock)
            return Outcome(journal, f"finished the move; {journal.source} points to the copy")
    return rollback(journal_path, clock=clock)


def rollback(journal_path: Path, *, clock: Callable[[], float] = time.time) -> Outcome:
    """Restores the original folder at its own path, from any stage. Reads the disk, not just
    the journal's stage, because a crash can land between a step and its journal entry."""
    journal = Journal.load(journal_path)
    if journal.stage == Stage.ROLLED_BACK:
        return Outcome(journal, "already rolled back")
    journal.log(Stage.ROLLING_BACK, f"from {journal.stage}", clock)
    src, tgt = journal.source, journal.target
    probe_path, restore = src + PROBE_SUFFIX, src + RESTORE_SUFFIX
    if not _exists(src) and _exists(probe_path) and not _is_reparse(probe_path):
        _rename(probe_path, src)
    # The old copy: `<name>.reclaim-moved`, or, once that was deleted, the copy a rollback
    # makes back from the target.
    old = journal.moved if _exists(journal.moved) else restore
    if _is_reparse(src):
        pointed = _junction_target(src)
        if pointed is None or _key(pointed) != _key(tgt):
            raise RelocateError(f"{src} is a link, but not to {tgt}; refusing to change it")
        if old == restore:
            # The old copy was deleted: copy the data back first (from scratch: a restore left
            # by an interrupted rollback is our own partial copy).
            if _exists(restore):
                rmtree_reparse_point_safe(long_path(Path(restore)))
            assert_not_real_profile_under_pytest(restore, operation="create")
            os.mkdir(long_path(Path(restore)))  # noqa: PTH102
            _copy_tree(tgt, restore)
            _verify_listing(tgt, restore)
            files, _ = _listing(tgt)
            bad = _differences(tgt, restore, files)
            if bad:
                raise RelocateError(f"{len(bad)} files did not copy back: {_sample(bad)}")
        _probe_target(tgt)
        # Copy back while the junction still stands: a failure here leaves the move as it was.
        copied_back = _sync_back(tgt, old)
        _remove_junction(src)
        journal.log(Stage.ROLLING_BACK, "junction removed", clock)
        try:
            # Anything written in the moment between the probe and the removal.
            copied_back += _sync_back(tgt, old)
        except Exception:
            _create_junction(src, tgt)
            journal.log(Stage.ROLLING_BACK, "copy-back failed; junction restored", clock)
            raise
        journal.log(Stage.ROLLING_BACK, f"copied back {copied_back} files changed after the swap")
    elif journal.junction_attempted and not _exists(src) and _exists(old) and _exists(tgt):
        # Interrupted between removing the junction and renaming back (or before the junction
        # was made): copying back again is idempotent and catches writes made after the swap.
        _sync_back(tgt, old)
    if not _exists(src) and _exists(old):
        _rename(old, src)
    if not _exists(src) or _is_reparse(src):
        raise RelocateError(f"could not restore {src}; the journal is {journal.path}")
    kept = _discard_copy(journal)
    note = "restored the original folder"
    if kept:
        note += f"; kept {kept[0]} ({kept[1]})"
    journal.log(Stage.ROLLED_BACK, note, clock)
    return Outcome(journal, note, kept or ())


def _probe_target(target: str) -> None:
    probe_path = target + PROBE_SUFFIX
    try:
        _rename(target, probe_path)
    except OSError as exc:
        raise InUseError(f"{target} is in use: close the program using it, then retry") from exc
    _rename(probe_path, target)


def _sync_back(target: str, moved: str) -> int:
    """Copies into the old copy every file the target gained, or that a program rewrote through
    the junction (its size or mtime moved on: the copy carried the original's exactly), and
    proves each copy by SHA-256. A target file with the same size and mtime but other bytes
    was not edited, it is a bad copy: the old copy keeps its own version, and the target is
    then kept rather than discarded (`_discard_copy`). Deletes nothing in the old copy: a file
    removed after the swap comes back."""
    tgt_files, tgt_dirs = _listing(target)
    old_files, _ = _listing(moved)
    for rel in sorted(tgt_dirs):
        os.makedirs(_join(moved, rel), exist_ok=True)  # noqa: PTH103
    changed = [
        rel
        for rel, st in tgt_files.items()
        if rel not in old_files
        or old_files[rel].st_size != st.st_size
        or old_files[rel].st_mtime_ns != st.st_mtime_ns
    ]
    for rel in changed:
        _copy_file(_join(target, rel), _join(moved, rel))
    bad = _differences(target, moved, changed)
    if bad:
        raise RelocateError(f"{len(bad)} files could not be copied back: {_sample(bad)}")
    return len(changed)


def _discard_copy(journal: Journal) -> tuple[str, str] | None:
    """Removes our copy in the target when that is provably safe, else keeps it and says why.

    Before the junction existed nothing but this code wrote to the target, so it is removed.
    After, it is removed only if every file in it is byte-identical in the restored source.
    """
    tgt = journal.target
    if not journal.target_created or not _exists(tgt):
        return None
    if _is_reparse(tgt):
        return (tgt, "it is a link now, not the copy this move made")
    if journal.junction_attempted:
        try:
            files, _ = _listing(tgt)
            bad = _differences(tgt, journal.source, files)
        except OSError as exc:
            return (tgt, f"it could not be compared with the restored folder: {exc}")
        if bad:
            return (tgt, f"{len(bad)} of its files differ from the restored folder")
    rmtree_reparse_point_safe(long_path(Path(tgt)))
    for parent in reversed(journal.created_parents):
        try:
            assert_not_real_profile_under_pytest(parent, operation="remove")
            os.rmdir(long_path(Path(parent)))  # noqa: PTH106 -- only if still empty
        except OSError:
            break
    return None


# ------------------------------------------------------------------ delete the old copy


def delete_old_copy(
    journal_path: Path,
    *,
    confirmed: bool = False,
    accept_missing: bool = False,
    clock: Callable[[], float] = time.time,
    free_bytes: Callable[[str], int] = _free_bytes,
) -> Outcome:
    """Deletes `<name>.reclaim-moved` once every file of it is proved present in the target.

    Allowed after `KEEP_OLD_COPY_DAYS`, or earlier with `confirmed`. A file of the old copy
    passes when the target's file has the same SHA-256, or a different mtime (a program
    rewrote it through the junction). A file missing from the target (deleted through
    the junction) refuses the delete unless `accept_missing`.
    """
    journal = Journal.load(journal_path)
    if journal.stage == Stage.DELETING_OLD:
        return _finish_delete(journal, clock, free_bytes)
    if journal.stage != Stage.DONE:
        raise RelocateError(f"the move is {journal.stage}, not done; nothing to delete")
    age_days = (clock() - journal.swapped_at) / 86400
    if not confirmed and age_days < KEEP_OLD_COPY_DAYS:
        raise RelocateError(
            f"the old copy is kept for {KEEP_OLD_COPY_DAYS} days after the move "
            f"({age_days:.1f} so far); confirm to delete it sooner"
        )
    moved, src = journal.moved, journal.source
    if not _exists(moved) or _is_reparse(moved):
        raise RelocateError(f"{moved} is missing or not a folder; nothing deleted")
    pointed = _junction_target(src)
    if pointed is None or _key(pointed) != _key(journal.target):
        raise RelocateError(f"{src} no longer points to {journal.target}; nothing deleted")
    old_files, _ = _listing(moved)
    if not old_files:
        raise RelocateError(f"{moved} lists no files; refusing to treat that as verified")
    new_files, _ = _listing(journal.target)
    missing, differ = [], []
    for rel, old in old_files.items():
        new = new_files.get(rel)
        if new is None:
            missing.append(rel)
        elif new.st_mtime_ns == old.st_mtime_ns and _sha256(_join(moved, rel)) != _sha256(
            _join(journal.target, rel)
        ):
            # The copy carries the source's mtime exactly, so an unchanged mtime means nothing
            # wrote the file through the junction: different bytes are a bad copy, not an edit.
            differ.append(rel)
    if differ:
        raise RelocateError(
            f"{len(differ)} files differ from the moved copy without having been changed since "
            f"the move ({_sample(differ)}); nothing deleted. The old copy is intact: roll back to "
            "restore it"
        )
    if missing and not accept_missing:
        raise RelocateError(
            f"{len(missing)} files of the old copy are no longer in the moved folder "
            f"({_sample(missing)}); nothing deleted. Accept that to delete anyway."
        )
    journal.log(
        Stage.DELETING_OLD,
        f"verified {len(old_files) - len(missing)} files; {len(missing)} accepted as removed",
        clock,
    )
    return _finish_delete(journal, clock, free_bytes)


def _finish_delete(
    journal: Journal, clock: Callable[[], float], free_bytes: Callable[[str], int] = _free_bytes
) -> Outcome:
    if _exists(journal.moved):
        rmtree_reparse_point_safe(long_path(Path(journal.moved)))
    journal.source_free_after = free_bytes(journal.source)
    freed = journal.source_free_after - journal.source_free_before
    journal.log(Stage.DELETED_OLD, f"volume free space changed by {freed} bytes", clock)
    return Outcome(journal, f"deleted the old copy; the volume gained {freed / 2**30:.2f} GiB")


def journals(journal_dir: Path) -> list[Journal]:
    if not journal_dir.is_dir():
        return []
    return [Journal.load(p) for p in sorted(journal_dir.glob("relocate-*.json"))]
