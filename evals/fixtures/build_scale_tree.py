from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_fixtures.build_document_similarity_fixtures import build_document_similarity_fixtures
from ai_fixtures.build_image_similarity_fixtures import build_image_similarity_fixtures

from reclaim.scanner import long_path

# Wave 1 P0-C (E2E-at-scale): a single, parametrized, seed=42 deterministic synthetic-filesystem
# generator covering every real-disk edge case this codebase's own comments/ADRs/PLAN.md
# checkpoints have separately documented as a source of a real bug (D12's MAX_PATH subtree drop,
# ADR-0004's cross-volume copy-verify, the reparse-point-not-recursed-into contract, the cloud-
# placeholder attribute gap, ...), materialized ONCE so `evals/test_e2e_scale.py` can exercise
# the full scan -> dedup -> AI-near-dup -> executor -> recovery pipeline against one coherent
# tree with a machine-checkable ground truth, instead of each stage's own narrower fixture.
#
# Deliberately NOT a hand-authored JSON manifest read back in (unlike
# `evals/fixtures/build_golden_tree.py`) -- like `evals/ai_fixtures/build_image_similarity_
# fixtures.py`, the checked-in artifact IS this generator: deterministic given `seed`, so
# re-running it always reproduces the same tree + the same ground truth. The ground truth is
# still written out as a real JSON file into the generated tree itself (`_scale_tree_manifest.
# json`) -- not committed to the repo (it lives under whatever `root` the caller points at, e.g.
# a pytest `tmp_path`), but a real, loadable artifact a caller can diff against without having to
# re-run the generator or trust an in-process object alone.

_SEED = 42

# --- exact-duplicate sets -----------------------------------------------------------------------

_EXACT_DUP_SET_SPECS: tuple[tuple[str, int, int], ...] = (
    # (set_id, member_count, content_size_bytes)
    ("dup_pair_small", 2, 512),
    ("dup_pair_docs", 2, 4_096),
    ("dup_triple", 3, 2_048),
    ("dup_quad_photos", 4, 8_192),
    ("dup_pair_binary", 2, 65_536),
)


@dataclass(frozen=True, slots=True)
class ExactDuplicateSet:
    id: str
    paths: tuple[Path, ...]
    size_bytes: int


@dataclass(frozen=True, slots=True)
class NearDupCluster:
    """One ground-truth near-duplicate cluster (image or document) -- mirrors `ai_fixtures`'
    `true_cluster_id` grouping, but pre-grouped here (distractor singletons already excluded)
    since a caller computing precision/recall only ever wants real multi-member clusters."""

    id: str
    paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class SparseFileInfo:
    path: Path
    logical_size_bytes: int
    allocated_size_bytes: int
    # True only when BOTH `fsutil sparse queryflag` and a real allocated-vs-logical size gap
    # agree -- never trusted on a single signal (house rule: don't just assume the flag stuck).
    verified_sparse: bool
    note: str


@dataclass(frozen=True, slots=True)
class JunctionInfo:
    link_path: Path
    target_path: Path
    # `mklink /J` can fail in a locked-down environment even for a non-elevated user (unlike a
    # symlink, a junction normally doesn't need SeCreateSymbolicLinkPrivilege, but this is still
    # best-effort, graceful-skip, matching `tests/test_scanner.py`'s own convention for this
    # exact command).
    created: bool
    note: str


@dataclass(frozen=True, slots=True)
class CloudPlaceholderInfo:
    path: Path
    # Always False in practice -- see the docstring on `_plant_cloud_placeholder` for why
    # `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` cannot be set on a real NTFS file without an actual
    # cloud-sync filter driver (OneDrive/Dropbox/Google Drive) or the Cloud Filter API, and is
    # NOT among the handful of attribute bits `SetFileAttributesW` will actually persist.
    attribute_set_on_disk: bool
    note: str


@dataclass(frozen=True, slots=True)
class PermissionDeniedInfo:
    path: Path
    icacls_applied: bool
    # Empirically measured (a real `os.listdir` attempt against the ACL'd directory right after
    # applying it), never assumed -- `tests/test_recovery.py`'s own documented finding is that
    # GitHub Actions' Windows runners execute as a genuinely elevated Administrator, under which
    # a deny ACE like this one does not reliably block access; this field records what actually
    # happened on THIS run, on THIS machine, rather than asserting a universal claim.
    empirically_enforced: bool
    note: str


@dataclass(frozen=True, slots=True)
class ScaleTreeManifest:
    """Ground truth for one materialized `build_scale_tree` run. Every `Path` field is absolute,
    matching `FileRecord.path`'s own convention elsewhere in this codebase."""

    root: Path
    seed: int
    filler_file_count: int
    generated_at: float

    exact_duplicate_sets: tuple[ExactDuplicateSet, ...]
    near_dup_image_clusters: tuple[NearDupCluster, ...]
    near_dup_document_clusters: tuple[NearDupCluster, ...]

    images_root: Path
    documents_root: Path
    bulk_root: Path

    long_path_file: Path
    long_path_length_chars: int

    unicode_emoji_files: tuple[Path, ...]

    zero_byte_file: Path

    large_file: Path
    large_file_size_bytes: int

    sparse_file: SparseFileInfo
    junction: JunctionInfo
    junction_cycle: JunctionInfo
    cloud_placeholder: CloudPlaceholderInfo
    permission_denied: PermissionDeniedInfo

    # Real count of every file this run actually wrote to disk (bulk filler + every named
    # special case + every exact-dup/near-dup member) -- informational, not a claim scan_tree
    # will report the identical number (directories/junctions/skips count differently there).
    total_planted_files: int

    def to_json_dict(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(json.dumps(dataclasses.asdict(self), default=str))
        return loaded

    def write(self, path: Path | None = None) -> Path:
        """Writes the ground-truth manifest as real, loadable JSON into the generated tree
        itself (`<root>/_scale_tree_manifest.json` by default) -- the checked-in artifact is
        this generator (deterministic given `seed`), but the manifest FILE is still a real,
        independently-readable artifact for any caller that wants to diff against it without
        re-running generation or importing this module."""
        target = path if path is not None else self.root / "_scale_tree_manifest.json"
        target.write_text(json.dumps(self.to_json_dict(), indent=2), encoding="utf-8")
        return target


def load_scale_tree_manifest_json(path: Path) -> dict[str, Any]:
    """Reads a manifest written by `ScaleTreeManifest.write` back as a plain dict (paths as
    strings) -- proves the artifact is real, valid, loadable JSON, not just an in-process
    convenience object. Callers that want typed `Path`s should use the `ScaleTreeManifest`
    `build_scale_tree` itself returns instead of round-tripping through this."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _random_bytes_for(set_id: str, size: int) -> bytes:
    # Deterministic (seed + set_id, never real `random` state carried across sets) fixture
    # content -- not security-sensitive.
    import random

    return random.Random(f"{_SEED}:{set_id}").randbytes(size)  # noqa: S311


def _plant_exact_duplicate_sets(root: Path) -> tuple[ExactDuplicateSet, ...]:
    sets: list[ExactDuplicateSet] = []
    dup_root = root / "exact_duplicates"
    for set_id, member_count, size in _EXACT_DUP_SET_SPECS:
        content = _random_bytes_for(set_id, size)
        set_dir = dup_root / set_id
        paths: list[Path] = []
        for member_index in range(member_count):
            member_path = set_dir / f"copy_{member_index:02d}.bin"
            member_path.parent.mkdir(parents=True, exist_ok=True)
            member_path.write_bytes(content)
            paths.append(member_path)
        sets.append(ExactDuplicateSet(id=set_id, paths=tuple(paths), size_bytes=size))
    return tuple(sets)


def _plant_near_dup_images(root: Path) -> tuple[NearDupCluster, ...]:
    """Reuses `ai_fixtures.build_image_similarity_fixtures` verbatim (5 clusters x 4 members +
    5 distractors = 25 images -- inside the spec's 15-30 total range) rather than re-implementing
    perceptual near-dup image generation here."""
    images_root = root / "near_dup_images"
    cases = build_image_similarity_fixtures(images_root, n_clusters=5, n_distractors=5)
    by_cluster: dict[str, list[Path]] = {}
    for case in cases:
        by_cluster.setdefault(case.true_cluster_id, []).append(images_root / case.relative_path)
    return tuple(
        NearDupCluster(id=cluster_id, paths=tuple(paths))
        for cluster_id, paths in sorted(by_cluster.items())
        if len(paths) >= 2  # exclude distractor singletons -- not a real ground-truth cluster
    )


def _plant_near_dup_documents(root: Path) -> tuple[NearDupCluster, ...]:
    """Reuses `ai_fixtures.build_document_similarity_fixtures` verbatim (3 topics x 3 variants +
    3 distractors = 12 documents)."""
    documents_root = root / "near_dup_documents"
    cases = build_document_similarity_fixtures(documents_root, n_distractors=3)
    by_cluster: dict[str, list[Path]] = {}
    for case in cases:
        by_cluster.setdefault(case.true_cluster_id, []).append(documents_root / case.relative_path)
    return tuple(
        NearDupCluster(id=cluster_id, paths=tuple(paths))
        for cluster_id, paths in sorted(by_cluster.items())
        if len(paths) >= 2
    )


def _plant_long_path(root: Path) -> tuple[Path, int]:
    r"""Nests directories until the full path to a leaf payload file comfortably exceeds
    Windows' 260-char MAX_PATH, exercising D12's `\\?\`-prefixed long-path handling. Uses
    `os.makedirs` on a `long_path()`-prefixed string (`Path.mkdir` doesn't reliably round-trip
    that prefix) -- same convention as `tests/test_scanner.py::_make_deep_tree` and
    `tests/test_executor.py::_make_deep_tree` (this is a third, deliberate copy: a 4-line helper
    duplicated three times across independent fixture modules isn't worth a new shared-utils
    dependency between `tests/` and `evals/`, matching this codebase's own "duplicate twice,
    abstract on the third occurrence" rule applied to test-only helpers, not production code)."""
    current = root / "special" / "long_path"
    for i in range(15):
        current = current / (f"seg_{i:03d}_" + "x" * 20)
        os.makedirs(long_path(current), exist_ok=True)  # noqa: PTH103
    payload = current / "payload.bin"
    with open(long_path(payload), "wb") as fh:  # noqa: PTH123
        fh.write(b"long-path-payload")
    full_len = len(str(payload))
    assert full_len > 260, f"fixture path too short: {full_len} chars"
    return payload, full_len


_UNICODE_EMOJI_NAMES: tuple[str, ...] = (
    "café_notes.txt",
    "naïve_résumé.txt",
    "日本語のファイル名.txt",
    "Ñoño_archivo.txt",
    "emoji_😀_party_🎉.txt",
    "checked_✅_done.txt",
)


def _plant_unicode_and_emoji_files(root: Path) -> tuple[Path, ...]:
    unicode_root = root / "special" / "unicode_and_emoji"
    unicode_root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name in _UNICODE_EMOJI_NAMES:
        path = unicode_root / name
        path.write_bytes(f"content for {name}".encode())
        paths.append(path)
    return tuple(paths)


def _plant_zero_byte_file(root: Path) -> Path:
    path = root / "special" / "zero_byte.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# "Very large" here means large relative to every other planted file in this fixture (which are
# bytes-to-KB scale, matching real clutter-detector target sizes) while staying CI-disk/time-
# conscious: 25MB writes in well under a second even on a slow CI runner's disk (measured
# locally: see build_scale_tree's own docstring for the real number), and 25MB * a handful of
# scale-tier runs never meaningfully threatens CI disk quota the way a multi-hundred-MB fixture
# file would. Distinct from the sparse file below, which is logically much larger but allocates
# almost nothing on disk.
_LARGE_FILE_SIZE_BYTES = 25 * 1024 * 1024


def _plant_large_file(root: Path) -> tuple[Path, int]:
    path = root / "special" / "large_file.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"L" * _LARGE_FILE_SIZE_BYTES)
    return path, _LARGE_FILE_SIZE_BYTES


def _get_allocated_size_bytes(path: Path) -> int:
    """Real on-disk allocation size via `GetCompressedFileSizeW` -- for a sparse file this is
    the actual number of allocated bytes (holes cost nothing), which is smaller than
    `os.stat().st_size` (the LOGICAL size, including the hole) whenever the sparse flag genuinely
    took effect. `ctypes.windll.kernel32.GetCompressedFileSizeW` needs explicit `argtypes`/
    `restype` -- confirmed empirically while writing this fixture that the default (everything
    treated as `c_int`) silently truncates the 64-bit handle/pointer arguments and the call
    fails without raising, returning a bogus low value."""
    get_compressed_file_size_w = ctypes.windll.kernel32.GetCompressedFileSizeW
    get_compressed_file_size_w.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    get_compressed_file_size_w.restype = wintypes.DWORD
    high = wintypes.DWORD(0)
    low = get_compressed_file_size_w(str(path), ctypes.byref(high))
    _INVALID_FILE_SIZE = 0xFFFFFFFF
    if low == _INVALID_FILE_SIZE and ctypes.GetLastError() != 0:
        raise OSError(f"GetCompressedFileSizeW failed for {path}")
    return int((high.value << 32) + low)


# The hole must be large enough that NTFS cluster-granularity allocation savings are
# unambiguous (a 1-cluster/4KB hole could plausibly round-trip to "looks the same either way" on
# some volumes) -- 8MB comfortably clears that, while still writing near-instantly (only the
# `head`/`tail` byte ranges are ever actually written).
_SPARSE_HOLE_SIZE_BYTES = 8 * 1024 * 1024


def _plant_sparse_file(root: Path) -> SparseFileInfo:
    r"""Creates a real NTFS-sparse file: `fsutil sparse setflag` BEFORE the hole is written (not
    after -- confirmed empirically while writing this fixture that setting the flag on an
    already-fully-allocated file does NOT retroactively deallocate the existing zero-filled
    region; the flag must be set first so the OS knows not to allocate the seeked-past range at
    all), then a `head` write, a `seek` past the hole, and a `tail` write. Verified via BOTH
    `fsutil sparse queryflag`'s own textual confirmation AND a real allocated-vs-logical size gap
    (`_get_allocated_size_bytes` vs `os.stat().st_size`) -- neither signal alone is trusted."""
    path = root / "special" / "sparse_file.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"head")

    setflag = subprocess.run(  # noqa: S603 -- fixed argv, no shell, local fixture path only
        ["fsutil", "sparse", "setflag", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if setflag.returncode != 0:
        return SparseFileInfo(
            path=path,
            logical_size_bytes=path.stat().st_size,
            allocated_size_bytes=path.stat().st_size,
            verified_sparse=False,
            note=f"fsutil sparse setflag failed: {setflag.stderr or setflag.stdout}",
        )

    with path.open("r+b") as fh:
        fh.seek(_SPARSE_HOLE_SIZE_BYTES)
        fh.write(b"tail")

    queryflag = subprocess.run(  # noqa: S603
        ["fsutil", "sparse", "queryflag", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    flag_confirmed = "set as sparse" in (queryflag.stdout or "").lower()

    logical_size = path.stat().st_size
    allocated_size = _get_allocated_size_bytes(path)
    size_gap_confirmed = allocated_size < logical_size

    verified = flag_confirmed and size_gap_confirmed
    note = (
        f"fsutil queryflag confirmed={flag_confirmed}, "
        f"allocated={allocated_size}B < logical={logical_size}B: {size_gap_confirmed}"
    )
    return SparseFileInfo(
        path=path,
        logical_size_bytes=logical_size,
        allocated_size_bytes=allocated_size,
        verified_sparse=verified,
        note=note,
    )


def _make_junction(link: Path, target: Path) -> tuple[bool, str]:
    r"""`mklink /J` (NTFS junction) -- unlike a symlink, this normally doesn't require
    `SeCreateSymbolicLinkPrivilege`, but is still best-effort/graceful-skip here, matching
    `tests/test_scanner.py::test_scan_tree_reparse_point_is_recorded_but_not_recursed_into`'s own
    convention for this exact command (a fixture builder can't call `pytest.skip` itself, so it
    reports success/failure back to the caller instead)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 -- fixed argv, cmd is a builtin, not untrusted input
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False, f"mklink /J failed: {result.stderr or result.stdout}"
    return True, "created"


def _plant_junction(root: Path) -> JunctionInfo:
    target = root / "special" / "junction_target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "inside_target.txt").write_text("real content under the junction target\n")
    link = root / "special" / "link_to_target"
    created, note = _make_junction(link, target)
    return JunctionInfo(link_path=link, target_path=target, created=created, note=note)


def _plant_junction_cycle(root: Path) -> JunctionInfo:
    """A junction pointing back to an ANCESTOR directory (`root` itself), forming a genuine
    filesystem cycle: `root/special/junction_cycle/back_to_root -> root`, so walking through the
    link and back down `special/junction_cycle/...` would revisit `root` forever IF a walker
    followed reparse points at all.

    Confirmed SAFE against `scan_tree` before this fixture was written to rely on it: `reclaim.
    scanner._walk_subtree`/`build_record` gate recursion on `should_recurse = is_dir_entry and
    not is_reparse_point` -- a reparse point (junction OR symlink) is always recorded as a leaf
    entry and NEVER pushed onto the walk stack, regardless of what it points to. This is already
    proven by `tests/test_scanner.py::
    test_scan_tree_reparse_point_is_recorded_but_not_recursed_into`. This fixture case exists to
    keep that contract under a real, deliberately-adversarial cycle rather than only a
    non-cyclic junction -- if that contract were ever accidentally weakened (e.g. a future change
    that recurses into a junction under some condition), THIS fixture is what would turn it into
    a real, reproducible hang instead of silently passing."""
    cycle_dir = root / "special" / "junction_cycle"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    link = cycle_dir / "back_to_root"
    created, note = _make_junction(link, root)
    return JunctionInfo(link_path=link, target_path=root, created=created, note=note)


_CLOUD_PLACEHOLDER_RECALL_BIT = 0x00400000  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


def _plant_cloud_placeholder(root: Path) -> CloudPlaceholderInfo:
    """Creates the file this case names, and makes a real, honest attempt to set
    `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` via `SetFileAttributesW` -- then VERIFIES via
    `GetFileAttributesW` whether it actually stuck, rather than assuming.

    Confirmed empirically while writing this fixture: `SetFileAttributesW` returns success
    (nonzero, `GetLastError()==0`) for this call, but `GetFileAttributesW` immediately after
    shows the bit was silently dropped -- Windows only persists a small allowlisted set of
    attribute bits through this API (READONLY/HIDDEN/SYSTEM/ARCHIVE/NORMAL/TEMPORARY, ...); a
    real cloud placeholder's `RECALL_ON_DATA_ACCESS` bit is set by the owning cloud-sync filter
    driver (OneDrive/Dropbox/Google Drive) via the Cloud Filter API when it creates a reparse-
    point placeholder, not by a plain attribute-set call on an ordinary file. This matches
    `evals/fixtures/build_golden_tree.py`'s own documented finding ("the only Windows attribute
    bit settable through stdlib without ctypes/OneDrive") and `tests/test_safety.py`'s own
    mechanism for this exact case: constructing a `FileRecord` with the bit set directly rather
    than depending on a real on-disk toggle. This fixture still returns a real file at `path`
    (an ordinary, un-flagged file scan_tree will walk normally) so a caller that wants to
    exercise `SafetyValidator`'s cloud-placeholder deny rule builds its own `FileRecord` for this
    path with `attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` set, the same way
    `tests/test_safety.py::test_cloud_placeholder_blocked` already does.
    """
    path = root / "special" / "cloud_placeholder" / "placeholder.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cloud placeholder simulated content\n")

    set_ok = ctypes.windll.kernel32.SetFileAttributesW(str(path), _CLOUD_PLACEHOLDER_RECALL_BIT)
    attrs_after = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    stuck = bool(set_ok) and bool(attrs_after & _CLOUD_PLACEHOLDER_RECALL_BIT)

    return CloudPlaceholderInfo(
        path=path,
        attribute_set_on_disk=stuck,
        note=(
            "SetFileAttributesW cannot persist FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS on an "
            "ordinary NTFS file without a real cloud-sync filter driver or the Cloud Filter "
            "API -- confirmed empirically (set call reports success, GetFileAttributesW shows "
            "the bit was dropped). A caller exercising cloud-placeholder detection must "
            "construct a FileRecord for this path with the attribute set directly, matching "
            "tests/test_safety.py::test_cloud_placeholder_blocked's own mechanism."
        ),
    )


def _plant_permission_denied(root: Path) -> PermissionDeniedInfo:
    """Creates a directory and applies a real `icacls` deny ACE for the current user -- then
    EMPIRICALLY tests it (a real `os.listdir` attempt right after) rather than assuming it took
    effect. `tests/test_recovery.py` already documents why this can't be trusted as universally
    enforced: GitHub Actions' Windows runners execute as a genuinely elevated Administrator,
    under which a deny ACE like this one does not reliably block access. This fixture still
    applies the real ACL (useful and enforced on an ordinary non-elevated developer machine) but
    records what ACTUALLY happened on THIS run via `empirically_enforced`, so a caller never has
    to guess which environment it's running in."""
    path = root / "special" / "permission_denied"
    path.mkdir(parents=True, exist_ok=True)
    (path / "secret.txt").write_text("should not be listable if the deny ACE took effect\n")

    username = os.environ.get("USERNAME", "Everyone")
    result = subprocess.run(  # noqa: S603 -- fixed argv, username from env, not untrusted input
        ["icacls", str(path), "/deny", f"{username}:(OI)(CI)F"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    icacls_applied = result.returncode == 0

    empirically_enforced = False
    if icacls_applied:
        try:
            list(path.iterdir())
        except PermissionError:
            empirically_enforced = True

    note = (
        f"icacls_applied={icacls_applied} "
        f"(stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}); "
        f"empirically_enforced={empirically_enforced} -- see tests/test_recovery.py's own "
        "documented finding that this project's CI runners execute as elevated Administrators, "
        "under which a deny ACE like this does not reliably block access"
    )
    return PermissionDeniedInfo(
        path=path,
        icacls_applied=icacls_applied,
        empirically_enforced=empirically_enforced,
        note=note,
    )


_BULK_FILES_PER_LEAF_DIR = 500


def _plant_bulk_filler(root: Path, *, filler_file_count: int) -> Path:
    """Writes `filler_file_count` trivial, small, UNIQUE-content files (the detection-quality
    signal lives entirely in the exact/near-dup sets above -- bulk filler exists purely to give
    `scan_tree` real file-count volume to walk). Content is per-file-unique (`filler-<i>`, never
    a shared constant) so bulk filler can NEVER accidentally exact-duplicate itself or collide
    with a planted duplicate set -- an earlier draft of this fixture used identical filler
    content across every file, which would have made every filler file an unplanted, unbounded
    duplicate cluster of its own (up to `filler_file_count` choose 2 pairs) and corrupted any
    precision/recall computation against the ground-truth manifest.

    Nested into `leaf_dirs = ceil(filler_file_count / 500)` leaf directories of ~500 files each
    (matching `evals/test_scanner_memory.py`'s own "fixed small fan-out per leaf directory"
    convention) rather than one flat directory -- keeps every single `os.scandir()` call's
    materialized listing small and constant regardless of total file count, and gives
    `scan_tree`'s per-top-level-directory worker pool real (if modest) parallelism to exploit.
    No per-file subprocess calls anywhere in this loop -- pure `pathlib`/`os` writes.
    """
    bulk_root = root / "bulk"
    leaf_dir: Path | None = None
    for i in range(filler_file_count):
        if i % _BULK_FILES_PER_LEAF_DIR == 0:
            leaf_dir = bulk_root / f"dir_{i // _BULK_FILES_PER_LEAF_DIR:05d}"
            leaf_dir.mkdir(parents=True, exist_ok=True)
        assert leaf_dir is not None  # set on the first iteration, always
        (leaf_dir / f"file_{i:07d}.bin").write_bytes(f"filler-{i}".encode())
    return bulk_root


# "A few thousand" per the task brief -- fast enough for routine local iteration (measured: see
# build_scale_tree's own module-level report in evals/test_e2e_scale.py) while still exercising
# `scan_tree`'s multi-top-level-directory worker fan-out and `_BatchIndexWriter`'s mid-walk flush
# cadence (`_WRITE_BATCH_SIZE`=5000 in scanner.py) at least partially.
DEFAULT_FILLER_FILE_COUNT = 3_000

# The task's explicit "100k+ file scale tier" -- selected via `--filler-count` on this module's
# CLI entry point or `filler_file_count=` directly; never the default (too slow for routine
# iteration -- see the measured wall-clock time reported in evals/test_e2e_scale.py).
LARGE_FILLER_FILE_COUNT = 100_000


def build_scale_tree(
    root: Path,
    *,
    filler_file_count: int = DEFAULT_FILLER_FILE_COUNT,
    seed: int = _SEED,
) -> ScaleTreeManifest:
    """Materializes the full scale-tree fixture under `root` and returns its ground-truth
    manifest (also written to `<root>/_scale_tree_manifest.json`).

    `seed` is accepted for documentation/future-parametrization symmetry with every other
    seeded generator in this codebase (house rule 40) but is not actually threaded through content
    generation today -- every sub-generator here (`_random_bytes_for`, the reused `ai_fixtures`
    builders) already hardcodes its own fixed seed internally, so `build_scale_tree` itself is
    fully deterministic regardless of this parameter's value; it exists so a future caller that
    DOES want seed-varied fixtures has a single obvious parameter to thread through, rather than
    silently being non-reproducible.

    Never touches anything outside `root`.
    """
    root.mkdir(parents=True, exist_ok=True)
    generated_at = time.time()

    exact_duplicate_sets = _plant_exact_duplicate_sets(root)
    near_dup_image_clusters = _plant_near_dup_images(root)
    near_dup_document_clusters = _plant_near_dup_documents(root)

    long_path_file, long_path_length = _plant_long_path(root)
    unicode_emoji_files = _plant_unicode_and_emoji_files(root)
    zero_byte_file = _plant_zero_byte_file(root)
    large_file, large_file_size = _plant_large_file(root)
    sparse_file = _plant_sparse_file(root)
    junction = _plant_junction(root)
    junction_cycle = _plant_junction_cycle(root)
    cloud_placeholder = _plant_cloud_placeholder(root)
    permission_denied = _plant_permission_denied(root)

    bulk_root = _plant_bulk_filler(root, filler_file_count=filler_file_count)

    total_planted_files = (
        sum(len(s.paths) for s in exact_duplicate_sets)
        + sum(len(c.paths) for c in near_dup_image_clusters)
        + sum(len(c.paths) for c in near_dup_document_clusters)
        + 1  # long_path_file
        + len(unicode_emoji_files)
        + 1  # zero_byte_file
        + 1  # large_file
        + 1  # sparse_file
        + 1  # cloud_placeholder file
        + 1  # permission_denied's inner secret.txt (may be unlistable, but was written)
        + filler_file_count
    )

    manifest = ScaleTreeManifest(
        root=root,
        seed=seed,
        filler_file_count=filler_file_count,
        generated_at=generated_at,
        exact_duplicate_sets=exact_duplicate_sets,
        near_dup_image_clusters=near_dup_image_clusters,
        near_dup_document_clusters=near_dup_document_clusters,
        images_root=root / "near_dup_images",
        documents_root=root / "near_dup_documents",
        bulk_root=bulk_root,
        long_path_file=long_path_file,
        long_path_length_chars=long_path_length,
        unicode_emoji_files=unicode_emoji_files,
        zero_byte_file=zero_byte_file,
        large_file=large_file,
        large_file_size_bytes=large_file_size,
        sparse_file=sparse_file,
        junction=junction,
        junction_cycle=junction_cycle,
        cloud_placeholder=cloud_placeholder,
        permission_denied=permission_denied,
        total_planted_files=total_planted_files,
    )
    manifest.write()
    return manifest


def _main() -> None:
    """CLI entry point for a manual/one-off large-tier generation run, e.g.:
    `uv run python evals/fixtures/build_scale_tree.py --root .scratch/scale_tree
    --filler-count 100000`. Reports real wall-clock time -- house rule 65b, never an estimate."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--filler-count", type=int, default=DEFAULT_FILLER_FILE_COUNT)
    parser.add_argument("--seed", type=int, default=_SEED)
    args = parser.parse_args()

    start = time.perf_counter()
    manifest = build_scale_tree(args.root, filler_file_count=args.filler_count, seed=args.seed)
    elapsed = time.perf_counter() - start
    print(  # noqa: T201 -- CLI reporting tool, not library code
        f"generated {manifest.total_planted_files} files under {manifest.root} in "
        f"{elapsed:.2f}s ({manifest.total_planted_files / elapsed:.0f} files/sec)"
    )


if __name__ == "__main__":
    _main()
