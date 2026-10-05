from __future__ import annotations

import itertools
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace as _dataclass_replace
from datetime import UTC, datetime
from pathlib import Path

import blake3
import structlog

from reclaim.config import Config
from reclaim.freshstat import fresh_stat_signature
from reclaim.index import HashCacheEntry, ScanIndex, cached_full_hash, cached_partial_hash
from reclaim.linkinfo import estimate_reclaimable_bytes
from reclaim.models import (
    Candidate,
    DuplicateCluster,
    FileRecord,
    HashSkip,
    MaterialityExclusionStats,
    Mode,
    Tier,
    Verdict,
)
from reclaim.safety import SafetyValidator, first_matching_pattern
from reclaim.scanner import long_path

logger = structlog.get_logger(__name__)

_PARTIAL_HASH_CHUNK_BYTES = 64 * 1024
# Files at or below this size are hashed whole in one read rather than as two 64KB chunks —
# reading first+last 64KB of a <=128KB file would double-read overlapping bytes (spec: "don't
# double-read overlapping regions").
_PARTIAL_HASH_WHOLE_FILE_THRESHOLD = 2 * _PARTIAL_HASH_CHUNK_BYTES
_FULL_HASH_READ_CHUNK_BYTES = 1024 * 1024

_KEEP_HEURISTIC_LOCATION_SEGMENTS = frozenset({"downloads", "temp"})

_CATEGORY = "exact_duplicate"
_CATEGORY_GROUP = "duplicates"

# Observability: a multi-hour hash pass that prints nothing is indistinguishable from a hang
# (the real-disk-run incident this guards against — a 3.1M-file scan whose hash stage produced
# zero output and zero incrementing SQLite rows for as long as anyone watched it). One heartbeat
# line at most every this many seconds, plus flushing hash writes in batches rather than one
# giant commit at the very end, so `SELECT COUNT(*) FROM files WHERE partial_hash IS NOT NULL`
# actually moves while a run is in progress.
_HEARTBEAT_INTERVAL_SECONDS = 5.0
_WRITE_BATCH_SIZE = 500

# Per-file hang guard: a locked system file or a pathological read must never wedge the whole
# pipeline (known cloud-placeholder files are already excluded upstream by
# `ScanIndex.candidate_inventory()`'s `is_cloud_placeholder` filter, but this is the backstop
# for everything that filter doesn't catch). The read runs on a worker thread so a stuck
# syscall only costs one pool slot, not the calling thread — Python has no cross-platform way
# to preempt a blocked `read()`, so a timed-out thread is abandoned, not killed.
_HASH_READ_TIMEOUT_SECONDS = 30.0
# Measured on the owner's real index (docs: perf/dedup-warmup report): a COLD open() costs ~10 ms
# (real-time AV scan on first open) while the bytes themselves are noise, so hashing is
# latency-bound, not CPU- or bandwidth-bound -- 8 threads gave ~7x and 32 threads ~11x the serial
# files/s. 16 = the machine's logical core count; the pool is I/O-wait dominated, not CPU.
_HASH_TIMEOUT_WORKERS = 16
# Buckets are hashed a window at a time (not bucket-by-bucket) so the pool stays full even though
# most size buckets are small. Memory stays bounded: a window holds ~this many FileRecords (more
# only when one single bucket is larger).
_WINDOW_FILES = 2048

# Mirrors `config.DuplicatesConfig.min_reclaim_bytes`'s default — the value real callers
# (`generate_duplicate_candidates`, driven by `config.categories.duplicates.min_reclaim_bytes`)
# actually use. Kept as a literal default here too so `find_duplicate_clusters`/
# `materiality_exclusion_stats` stay usable without a `Config` object (evals, the API service
# layer's direct `find_duplicate_clusters` call) without silently reverting to "hash
# everything" if a caller forgets to pass it.
_DEFAULT_MIN_RECLAIM_BYTES = 1024 * 1024


class DedupCancelled(Exception):
    """Raised by a caller-supplied `checkpoint` to stop `find_duplicate_clusters` at the next
    batch boundary (ADR-0040). Every hash already flushed to the index stays -- that is the resume
    mechanism -- but no clusters are returned."""


def _is_downloads_or_temp(path: Path) -> bool:
    """Spec's keep-heuristic rule 1: "prefer copy outside Downloads/Temp"."""
    return any(part.lower() in _KEEP_HEURISTIC_LOCATION_SEGMENTS for part in path.parts)


def _location_rank(record: FileRecord) -> int:
    """ADR-0007: lower ranks first (preferred as keep). `0`: inside a git repository / active
    project — the strongest "this is the real, working copy" signal available. `1`: neither in
    a git repo nor in Downloads/Temp — a generic, undetermined location. `2`: inside
    Downloads/Temp — exactly the places an incidental/junk copy lives.

    A plain "not Downloads/Temp" boolean (this project's original rule 1) doesn't distinguish a
    genuine project copy from an arbitrary non-Downloads/Temp location — a copy sitting in, say,
    a random `Documents/backup` folder would tie with a git-repo copy on that check alone and
    fall through to the ctime/depth tiebreaks, which could pick the non-project copy as keep and
    propose the git-repo copy for deletion. This three-way rank makes git-repo membership a
    POSITIVE, first-class preference instead of just the absence of a Downloads/Temp penalty —
    a git-repo member is never outranked by a non-git, non-Downloads/Temp member.
    """
    if record.git_repo_root is not None:
        return 0
    if _is_downloads_or_temp(record.path):
        return 2
    return 1


def _is_risky_sole_survivor_location(record: FileRecord) -> bool:
    """ADR-0007: true if `record` sits in Downloads/Temp or is a cloud-sync placeholder (not
    fully synced locally) — the two location classes unsuitable to be the SOLE surviving copy
    of a duplicate cluster once every other member is deleted. Distinct from `_location_rank`'s
    keep-heuristic ranking: this asks "is the location we ended up keeping actually durable,"
    not "which candidate should we prefer."""
    return _is_downloads_or_temp(record.path) or record.is_cloud_placeholder


def cluster_needs_manual_review(cluster: DuplicateCluster) -> bool:
    """ADR-0007: true if the kept copy would be the cluster's sole survivor in a risky location
    (Downloads/Temp/cloud-placeholder) while at least one about-to-be-deleted member sits
    somewhere more durable. Auto-applying a cluster like this would strand the only surviving
    copy somewhere less durable than what's being thrown away — flagged for manual review
    (forced Tier B) instead of auto-picked, regardless of `config.categories.duplicates.enabled`.

    Honest reachability note: given `select_keep`'s location-rank ordering (`_location_rank` —
    Downloads/Temp always ranks worst), a Downloads/Temp copy can only win as keep if every
    OTHER cluster member is ALSO Downloads/Temp, in which case there is no more-durable member
    being thrown away and this function correctly returns `False` regardless. Given
    `ScanIndex.duplicate_size_candidates`'s pre-existing `is_cloud_placeholder = 0` filter, a
    cloud-placeholder file never becomes a cluster member (keep or duplicate) through the real
    pipeline at all. So this function's `True` branch is not reachable via
    `generate_duplicate_candidates` as the codebase stands today — it exists as defense-in-depth
    should either of those two upstream guarantees ever change, and is verified directly at the
    unit level (see `test_cluster_needs_manual_review_when_keep_is_risky_and_a_deleted_copy_is_
    stable`) rather than an end-to-end scenario that can't currently be constructed.
    """
    if not _is_risky_sole_survivor_location(cluster.keep):
        return False
    return any(not _is_risky_sole_survivor_location(d) for d in cluster.duplicates)


# ADR-0008: HF Hub's on-disk cache layout stores one logical model/dataset "object" as a
# content-addressed blob under `<repo-dir>/blobs/<sha>` plus a human-readable revision tree under
# `<repo-dir>/snapshots/<rev>/...` (normally a symlink back to the blob; without symlink-creation
# privilege, huggingface_hub falls back to a real, separate copy instead — confirmed on this
# project's real disk via direct `os.stat()`: distinct inodes, `nlink == 1` each, no
# `FILE_ATTRIBUTE_REPARSE_POINT`). Blob and snapshot are the SAME HF-managed object by design
# regardless of which storage mechanism produced them — treating them as independent duplicates
# and proposing one side for deletion can break model loading (application code reads the
# snapshot path, not the blob path directly) or leave a stray, unreferenced blob behind.
_HF_CACHE_REPO_PREFIXES = ("models--", "datasets--", "spaces--")
_HF_CACHE_OBJECT_SUBDIRS = frozenset({"blobs", "snapshots", "refs"})


def _hf_cache_object_root(path: Path) -> Path | None:
    """Returns the shared HF-repo cache directory (e.g. `.../hub/models--org--name`) if `path`
    sits under its `blobs/`, `snapshots/`, or `refs/` subtree — recognized structurally by the
    HF cache's own naming convention, independent of where that cache physically lives (not
    limited to `config.categories.model_caches.paths`'s configured default location)."""
    parts = path.parts
    for i, part in enumerate(parts[:-1]):
        if part.lower().startswith(_HF_CACHE_REPO_PREFIXES) and parts[i + 1].lower() in (
            _HF_CACHE_OBJECT_SUBDIRS
        ):
            return Path(*parts[: i + 1])
    return None


def _is_under_any(path: Path, roots: Sequence[Path]) -> bool:
    path_posix = path.as_posix().lower()
    for root in roots:
        root_posix = root.as_posix().lower()
        if path_posix == root_posix or path_posix.startswith(f"{root_posix}/"):
            return True
    return False


def _is_model_cache_path(path: Path, model_cache_roots: Sequence[Path]) -> bool:
    """ADR-0008: true if `path` sits under a configured model-cache root
    (`config.categories.model_caches.paths` — HF hub, torch hub, Ollama) OR matches the HF
    blob/snapshot cache structure directly, regardless of location. `exact_duplicate` must never
    propose a path like this for deletion — model-weight caches are reviewed as one unit under
    `model_caches` (ADR-0003's cost-aware retention), never piecemeal from the duplicate-
    detection side."""
    return _is_under_any(path, model_cache_roots) or _hf_cache_object_root(path) is not None


# ADR-0008: conda/venv environments intentionally duplicate their own copy of shared dependency
# binaries and data (CUDA DLLs, vendored tzdata, Tcl/Tk libraries, ...) for isolation between
# environments — deleting one environment's own copy because ANOTHER environment happens to
# carry a byte-identical copy can break that environment, even though the bytes really are
# identical right now. Real-disk validation (ADR-0008) found this isn't limited to
# `Lib/site-packages`: `envs/<name>/DLLs/`, `envs/<name>/Library/lib/`, and similar
# non-site-packages subtrees carry the exact same risk and are just as populated with
# byte-identical cross-env "duplicates" (1,456 DLL/exe/pyd/lib candidates alone, across many
# distinct conda envs, on the machine this ADR was written against) — a `Lib/site-packages`-only
# check would have left nearly all of them unprotected.
_CONDA_MARKER_DIRNAME = "conda-meta"
_VENV_MARKER_FILENAME = "pyvenv.cfg"
_PYTHON_EXECUTABLE_NAMES = ("python.exe", "pythonw.exe")
# ADR-0010: `bin/Scripts`-style layouts put the interpreter in a subdirectory, not the
# environment root itself (the real, Windows-standard `venv` layout: `<venv>/Scripts/python.exe`,
# not `<venv>/python.exe` — confirmed against this project's OWN `.venv`, which has no
# interpreter binary at its root at all, only `Scripts/` and `pyvenv.cfg`). `bin/` is the POSIX
# name for the same concept, checked too since some cross-platform/embedded distributions use it
# even under Windows.
_INTERPRETER_BIN_DIRNAMES = ("Scripts", "bin")
_POSIX_PYTHON_EXECUTABLE_NAMES = ("python", "python3")
_PYTHON_STDLIB_DIRNAME = "Lib"
_SITE_PACKAGES_DIRNAME = "site-packages"


def _has_python_executable(directory: Path) -> bool:
    """ADR-0009/0010: `python.exe`/`pythonw.exe` directly in `directory` — the layout every
    standalone CPython installation this project has found on real disk uses (conda base/env,
    a uv-managed build, `gcloud`'s bundled Python, the Android NDK's toolchain Python, and a
    plain `python.org` installer all put the interpreter directly at their own root)."""
    try:
        return any((directory / name).is_file() for name in _PYTHON_EXECUTABLE_NAMES)
    except OSError:
        return False


def _has_interpreter_bin_dir(directory: Path) -> bool:
    """ADR-0010: a `Scripts/`or `bin/` child directory that itself holds an interpreter binary —
    the layout `venv`-created environments use on Windows (`Scripts/`) and POSIX (`bin/`).
    Catches an environment whose `pyvenv.cfg` is missing, corrupted, or was never written by a
    tool this function already recognizes by marker file — structural detection as the fallback,
    not the norm."""
    for bin_dirname in _INTERPRETER_BIN_DIRNAMES:
        bin_dir = directory / bin_dirname
        try:
            if not bin_dir.is_dir():
                continue
            if any((bin_dir / name).is_file() for name in _PYTHON_EXECUTABLE_NAMES):
                return True
            if any((bin_dir / name).is_file() for name in _POSIX_PYTHON_EXECUTABLE_NAMES):
                return True
        except OSError:
            continue
    return False


def _has_site_packages(directory: Path) -> bool:
    """ADR-0010: a `site-packages/` directory at its canonical nested location —
    `Lib/site-packages` (Windows) or `lib/site-packages` (POSIX-style embedded layouts) —
    directly under `directory`. `site-packages` existing at all is itself proof `directory` is
    (or was built as) a Python environment root, independent of whether its interpreter binary
    is still present, findable, or in a location this function otherwise recognizes."""
    for lib_dirname in (_PYTHON_STDLIB_DIRNAME, _PYTHON_STDLIB_DIRNAME.lower()):
        try:
            if (directory / lib_dirname / _SITE_PACKAGES_DIRNAME).is_dir():
                return True
        except OSError:
            continue
    return False


def _is_environment_root(ancestor: Path, cache: dict[Path, bool] | None) -> bool:
    """One directory's verdict for `_environment_root`'s walk, memoised in `cache` when given.
    A directory's markers do not change within one candidate-generation pass, and the ancestors
    of a cluster's members are overwhelmingly shared (every file under one venv walks the same
    ~10 parents), so the un-memoised walk cost ~58 stat calls per duplicate member -- 91% of
    `generate_duplicate_candidates`'s time on the owner's real index. Callers scope the cache to
    one pass (never module-level), so a venv created later is still seen on the next pass."""
    if cache is not None and ancestor in cache:
        return cache[ancestor]
    try:
        result = (
            (ancestor / _CONDA_MARKER_DIRNAME).is_dir()
            or (ancestor / _VENV_MARKER_FILENAME).is_file()
            or _has_python_executable(ancestor)
            or _has_interpreter_bin_dir(ancestor)
            or _has_site_packages(ancestor)
        )
    except OSError:
        result = False
    if cache is not None:
        cache[ancestor] = result
    return result


def _environment_root(path: Path, cache: dict[Path, bool] | None = None) -> Path | None:
    """The root directory of the Python environment `path` lives inside (a conda base install, a
    named conda `envs/<name>`, a `venv`/`.venv`, or a standalone Python installation), if any.
    Walks UP from `path` looking for the nearest ancestor that IS an environment/installation
    root, identified by any ONE of several signals, in priority order: a `conda-meta/`
    subdirectory (conda base or named env), a `pyvenv.cfg` file (venv) — both canonical marker
    files each tool writes for itself — or, ADR-0010, one of three STRUCTURAL signals that don't
    depend on any tool having written a marker at all: a Python executable directly in the
    directory, a `Scripts/`/`bin/` subdirectory holding one, or a `Lib/site-packages` (or
    `lib/site-packages`) subdirectory. Marker-file detection alone is NOT a complete defense —
    see ADR-0009's own incident and ADR-0010's honest accounting of it — so structural detection
    is the default here, not an opt-in fallback. Bounded to the ancestors of one file already in
    a small candidate cluster — never a directory walk over the whole disk.

    Conda's own `pkgs/` extraction cache is deliberately excluded up front (short-circuited
    before the walk, via the well-known `pkgs` path segment): it's a package-manager cache
    (analogous to pip/uv's own caches), not a live environment, and reclaiming a duplicate out of
    it is exactly as safe as any other package-cache reclaim already handled by `package_caches`.
    Without this, a `pkgs/<extracted-package>/...` path would otherwise walk up into the conda
    installation's own `conda-meta/` (or match its own bundled `Lib/site-packages`/interpreter
    layout) and be misidentified as a live environment rather than a cache entry.
    """
    if any(part.lower() == "pkgs" for part in path.parts):
        return None
    for ancestor in path.parents:
        if _is_environment_root(ancestor, cache):
            return ancestor
    return None


def _is_cross_environment_duplicate(
    duplicate: FileRecord, keep: FileRecord, cache: dict[Path, bool] | None = None
) -> bool:
    """ADR-0008: true if `duplicate` lives inside a recognized live Python environment (conda
    base, a named `envs/<name>`, or a venv) and `keep` does NOT live inside that SAME
    environment — whether `keep` is in a different environment, or not in any recognized
    environment at all.

    The second half matters as much as the first: real-disk validation found clusters where the
    keep-heuristic picked a conda `pkgs/`-cache copy as keep (`_environment_root` correctly
    returns `None` for `pkgs/`) while EVERY OTHER member was a different named environment's own
    copy of the same file. A naive "both must be in DIFFERENT environments" check would have
    let every one of those environment copies through, because comparing "environment A" against
    "not an environment" never satisfied a not-equal-and-both-non-None test — silently stranding
    every one of those environments' own copies behind a `pkgs/` cache entry that
    `package_caches` may independently and legitimately delete at any time. An environment must
    keep its OWN copy of its own files; an identical copy existing somewhere else (another
    environment, or a cache) does not make deleting the environment's own copy safe, regardless
    of what's kept."""
    duplicate_root = _environment_root(duplicate.path, cache)
    if duplicate_root is None:
        return False
    return _environment_root(keep.path, cache) != duplicate_root


def _dedup_ineligibility_reason(
    duplicate: FileRecord,
    keep: FileRecord,
    model_cache_roots: Sequence[Path],
    env_root_cache: dict[Path, bool] | None = None,
) -> str | None:
    """ADR-0008: why `duplicate` can never be an `exact_duplicate` deletion candidate,
    regardless of `SafetyValidator`'s verdict — `None` if there's no such reason. Checked before
    a duplicate ever reaches safety evaluation/tiering, so an excluded path never even shows up
    as a `BLOCKED`/`ELIGIBLE` candidate — it simply isn't one."""
    if _is_model_cache_path(duplicate.path, model_cache_roots):
        return "model_cache_managed"
    if _is_cross_environment_duplicate(duplicate, keep, env_root_cache):
        return "cross_environment"
    return None


def _compute_partial_hash(path: Path, size_bytes: int) -> str:
    hasher = blake3.blake3()
    with path.open("rb") as fh:
        if size_bytes <= _PARTIAL_HASH_WHOLE_FILE_THRESHOLD:
            hasher.update(fh.read())
        else:
            hasher.update(fh.read(_PARTIAL_HASH_CHUNK_BYTES))
            fh.seek(size_bytes - _PARTIAL_HASH_CHUNK_BYTES)
            hasher.update(fh.read(_PARTIAL_HASH_CHUNK_BYTES))
    return hasher.hexdigest()


def _compute_full_hash(path: Path) -> str:
    hasher = blake3.blake3()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_FULL_HASH_READ_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _due(*, last: float, now: float, interval: float) -> bool:
    """Pure predicate behind the heartbeat gate — split out from the hashing loops so the
    timing logic itself is unit-testable without needing a multi-second real sleep."""
    return (now - last) >= interval


def _hash_with_guard(
    executor: ThreadPoolExecutor,
    fn: Callable[..., str],
    path: Path,
    *args: object,
    timeout_seconds: float = _HASH_READ_TIMEOUT_SECONDS,
) -> tuple[str | None, str | None]:
    """Runs `fn(path, *args)` on `executor` and returns `(digest, skip_reason)` — exactly one
    is `None`. Never raises: a timeout or `OSError` both become a skip reason instead of
    propagating and killing the whole dedup run over one bad file."""
    return _await_digest(executor.submit(fn, path, *args), timeout_seconds)


def _await_digest(
    future: Future[str], timeout_seconds: float = _HASH_READ_TIMEOUT_SECONDS
) -> tuple[str | None, str | None]:
    try:
        return future.result(timeout=timeout_seconds), None
    except FutureTimeoutError:
        return None, "timeout"
    except OSError as exc:
        return None, str(exc)


def select_keep(members: Sequence[FileRecord]) -> FileRecord:
    """Picks the one cluster member to keep. Ranking, in order (ADR-0007): (1) location rank —
    prefer a copy inside a git repository/active project, then a copy in neither a git repo nor
    Downloads/Temp, then last a copy under Downloads/Temp (see `_location_rank`); (2) shortest
    path depth (fewest path segments from the drive root); (3) oldest `ctime` — Windows creation
    time, the "which copy existed first" signal, not POSIX change time; (4) lexicographic path
    sort as a final, deterministic tiebreak so output is reproducible run-to-run.

    Content survival was never the risk here — every cluster member is byte-identical by
    construction. Keeping the WRONG copy is: this ordering guarantees a git-repo/project member
    is never the one proposed for deletion as long as at least one exists in the cluster, even
    if a Downloads/Temp copy happens to have an older creation time or shallower path (the
    failure mode rank (1) alone, without git-repo awareness, could otherwise produce).
    """
    return min(
        members,
        key=lambda record: (
            _location_rank(record),
            len(record.path.parts),
            record.ctime,
            record.path.as_posix(),
        ),
    )


def _cached_partial_lookup(entry: HashCacheEntry | None, size: int, mtime: float) -> str | None:
    return cached_partial_hash(entry, current_size=size, current_mtime=mtime)


def _cached_full_lookup(entry: HashCacheEntry | None, size: int, mtime: float) -> str | None:
    return cached_full_hash(entry, current_size=size, current_mtime=mtime)


def _resolve_one(
    record: FileRecord,
    *,
    stage: str,
    hash_cache: dict[str, HashCacheEntry],
    cached_lookup: Callable[[HashCacheEntry | None, int, float], str | None],
    compute: Callable[..., str],
    compute_args: tuple[object, ...],
) -> tuple[str, bool]:
    """Worker-thread body for one hash (partial or full): `(digest, came_from_cache)`. Raises
    `OSError` from the read, which `_await_digest` turns into a skip reason."""
    entry = hash_cache.get(record.path.as_posix())
    digest = cached_lookup(entry, record.size_bytes, record.mtime)
    if digest is not None:
        # ADR-0035: the cache is keyed on the listing's (size, mtime), which lags for a file
        # open for write -- confirm against a live stat before trusting the cached digest. Only
        # a would-be cache HIT pays the stat (misses hash the file anyway).
        fresh = fresh_stat_signature(record.path)
        if fresh is None or (fresh.size, fresh.mtime) == (record.size_bytes, record.mtime):
            return digest, True
        logger.info("dedup.hash_cache_stale_listing", stage=stage, path=str(record.path))
    return compute(record.path, *compute_args), False


def _same_file_now(record: FileRecord) -> bool:
    """True iff `record`'s name still resolves, right now, to the very inode (and size) the index
    recorded -- the proof a hardlink sibling may borrow its group's hash. A stat is ~0.7 ms where
    a cold open is ~10 ms. Never raises: any doubt (missing, replaced, unreadable) is False and
    the caller hashes that name on its own, exactly as it would without the optimisation."""
    try:
        st = Path(long_path(record.path)).stat()
    except OSError:
        return False
    return (st.st_dev, st.st_ino, st.st_size) == (record.dev, record.ino, record.size_bytes)


def _drain_on_cancel(
    remaining: Sequence[tuple[list[FileRecord], Future[tuple[str, bool]], list[Future[bool]]]],
    pending_writes: list[tuple[Path, int, float, str]],
) -> None:
    """Cancel path of `_hash_stage`: stop every read that has not started, wait for the ones in
    flight, and keep their finished digests (a read that errored is simply not kept)."""
    in_flight: list[tuple[FileRecord, Future[tuple[str, bool]]]] = []
    for group, future, sibling_checks in remaining:
        for check in sibling_checks:
            check.cancel()
        if not future.cancel():
            in_flight.append((group[0], future))
    for first, future in in_flight:
        try:
            digest, from_cache = future.result()
        except Exception:  # noqa: S112 -- an unreadable file is just not committed
            continue
        if not from_cache:
            pending_writes.append((first.path, first.size_bytes, first.mtime, digest))


def _hash_stage(
    executor: ThreadPoolExecutor,
    records: Sequence[FileRecord],
    *,
    stage: str,
    hash_cache: dict[str, HashCacheEntry],
    cached_lookup: Callable[[HashCacheEntry | None, int, float], str | None],
    compute: Callable[..., str],
    with_size_arg: bool,
    pending_writes: list[tuple[Path, int, float, str]],
    skips: list[HashSkip] | None,
    collapse_hardlinks: bool = True,
    checkpoint: Callable[[], None] | None = None,
) -> dict[Path, str]:
    """Hashes `records` concurrently on `executor` (cache check, else compute, else skip) and
    returns `{path: digest}` for every record that produced one.

    Hardlinks: records sharing a non-zero `(dev, ino)` are one file with one content, so only
    the first of each such group is read; every other name borrows that digest ONLY after a live
    stat confirms it still points at the same inode (`_same_file_now`). A name that fails that
    check -- or whose group's first member is unreadable -- is hashed on its own in a second
    pass, so the skips, cache rows and clusters equal what hashing every name separately gives.
    `ino == 0` means "identity unknown" (synthetic/test records) and is never collapsed.

    `checkpoint` (ADR-0040) is called before collecting each group's result. If it raises
    `DedupCancelled`, every not-yet-started read is cancelled, the reads already in flight (at most
    the pool size) are awaited, every digest computed so far is appended to `pending_writes` for
    the caller to flush, and the exception propagates -- so a cancel's latency is bounded by the
    in-flight reads, not by the rest of a 2048-file window of large files."""
    by_identity: dict[object, list[FileRecord]] = {}
    for record in records:
        key: object = (record.dev, record.ino) if record.ino and collapse_hardlinks else record.path
        by_identity.setdefault(key, []).append(record)

    submitted = [
        (
            group,
            executor.submit(
                _resolve_one,
                group[0],
                stage=stage,
                hash_cache=hash_cache,
                cached_lookup=cached_lookup,
                compute=compute,
                compute_args=(group[0].size_bytes,) if with_size_arg else (),
            ),
            [executor.submit(_same_file_now, sibling) for sibling in group[1:]],
        )
        for group in by_identity.values()
    ]
    digests: dict[Path, str] = {}
    fallback: list[FileRecord] = []
    for position, (group, future, sibling_checks) in enumerate(submitted):
        if checkpoint is not None:
            try:
                checkpoint()
            except DedupCancelled:
                _drain_on_cancel(submitted[position:], pending_writes)
                raise
        first = group[0]
        reason: str | None = None
        try:
            digest, from_cache = future.result(timeout=_HASH_READ_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            digest, from_cache, reason = None, False, "timeout"
        except OSError as exc:
            digest, from_cache, reason = None, False, str(exc)
        if digest is None:
            logger.warning(
                "dedup.hash_unreadable", stage=stage, path=str(first.path), reason=reason
            )
            if skips is not None:
                skips.append(HashSkip(path=first.path, stage=stage, reason=reason or ""))
            fallback.extend(group[1:])
            continue
        digests[first.path] = digest
        if not from_cache:
            pending_writes.append((first.path, first.size_bytes, first.mtime, digest))
        for sibling, check in zip(group[1:], sibling_checks, strict=True):
            try:
                same = check.result(timeout=_HASH_READ_TIMEOUT_SECONDS)
            except FutureTimeoutError:
                same = False
            if not same:
                fallback.append(sibling)
                continue
            digests[sibling.path] = digest
            # The sibling never touched the disk, but a restart should still find its row.
            own = hash_cache.get(sibling.path.as_posix())
            if cached_lookup(own, sibling.size_bytes, sibling.mtime) is None:
                pending_writes.append((sibling.path, sibling.size_bytes, sibling.mtime, digest))
    if fallback:
        digests.update(
            _hash_stage(
                executor,
                fallback,
                stage=stage,
                hash_cache=hash_cache,
                cached_lookup=cached_lookup,
                compute=compute,
                with_size_arg=with_size_arg,
                pending_writes=pending_writes,
                skips=skips,
                collapse_hardlinks=False,
                checkpoint=checkpoint,
            )
        )
    return digests


def materiality_exclusion_stats(
    index: ScanIndex, *, min_reclaim_bytes: int = _DEFAULT_MIN_RECLAIM_BYTES
) -> MaterialityExclusionStats:
    """How many duplicate-size buckets `find_duplicate_clusters` will skip for falling below
    `min_reclaim_bytes`, and their summed theoretical (never measured, always labeled as an
    upper bound) reclaim — a cheap, independent SQL aggregate query, safe to call any time
    (e.g. from the CLI report) without running the hash pass itself."""
    bucket_count, theoretical_bytes = index.immaterial_duplicate_bucket_stats(
        min_reclaim_bytes=min_reclaim_bytes
    )
    return MaterialityExclusionStats(
        excluded_bucket_count=bucket_count, theoretical_bytes=theoretical_bytes
    )


def find_duplicate_clusters(
    index: ScanIndex,
    *,
    min_reclaim_bytes: int = _DEFAULT_MIN_RECLAIM_BYTES,
    skips: list[HashSkip] | None = None,
    exclusion_patterns: Sequence[str] = (),
    checkpoint: Callable[[], None] | None = None,
    worker_initializer: Callable[[], None] | None = None,
) -> list[DuplicateCluster]:
    """Size bucket -> 64KB partial hash -> full BLAKE3 hash, exactly in that order, reusing
    cached hashes from a prior run wherever a file's (size, mtime) hasn't changed since.

    `skips` is an optional out-param (append-to list, default `None` = don't bother collecting)
    rather than a second return value, so existing callers (`generate_duplicate_candidates`,
    the API service layer, evals) keep working unchanged against the plain
    `list[DuplicateCluster]` return type; callers that care about the skipped/unreadable files
    pass their own list and read it back after the call.

    `min_reclaim_bytes` is the materiality gate (2026-07-17 real-disk finding): a bucket whose
    theoretical best-case reclaim — `(distinct_inodes - 1) * size`, the names of one hardlinked
    file counting once — falls below this floor is never even queried for its members, let
    alone hashed. See
    `ScanIndex.duplicate_size_candidates`'s docstring and `materiality_exclusion_stats` (the
    reporting counterpart of this same filter).

    SQL-pushdown, streamed one size bucket at a time: `index.duplicate_size_candidates()`
    returns rows in size order (see its docstring), so `itertools.groupby` groups each size
    bucket's members together as they stream past — partial-hashed, and (for survivors)
    full-hashed, immediately, before the next bucket is even read from SQLite. This replaced an
    earlier design that materialized *every* candidate row into one `dict[size, list[...]]`
    before hashing anything: correct, but on a real disk where the size-uniqueness prefilter
    barely narrows anything (measured: 80% of files on one real `C:\\` shared a size with at
    least one other file), that meant building millions of `FileRecord` objects — and holding
    them all at once — before a single hash ran. Peak memory here is bounded by the *largest
    single size bucket*, not the total candidate count.

    `exclusion_patterns` (ADR-0039, `SafetyValidator.exclusion_patterns`): a file whose path
    matches one is dropped from its size bucket BEFORE any hashing -- never read, never hashed,
    never a cluster member, and so never the keeper either. Deliberate: an excluded file is
    treated as if it did not exist, so no other copy can be proposed for deletion "because a
    copy survives in the excluded tree".

    `checkpoint` (ADR-0040): called on the calling thread at every batch boundary -- before each
    window of up to `_WINDOW_FILES` files and between a window's partial and full stages. It may
    raise `DedupCancelled` to stop; pending hashes are flushed first, so the next pass over the
    same index hashes only the remainder (hashes are durable per batch, ADR-0038). It may also do
    housekeeping (the API layer leaves background priority there when a user asks for the result).

    `worker_initializer` (ADR-0040): run once in each hashing pool thread as it starts. The pool
    is created per window (threads are lazy, so this is cheap), so an initializer evaluated at
    that moment sees the CURRENT state -- e.g. enter OS background mode only while the warm-up
    has not been promoted to a user request.
    """
    candidate_count = index.duplicate_size_candidate_count(min_reclaim_bytes=min_reclaim_bytes)
    if candidate_count == 0:
        return []

    hash_cache = index.load_hash_cache()
    excluded = materiality_exclusion_stats(index, min_reclaim_bytes=min_reclaim_bytes)
    logger.info(
        "dedup.start",
        candidate_files=candidate_count,
        min_reclaim_bytes=min_reclaim_bytes,
        materiality_excluded_buckets=excluded.excluded_bucket_count,
        materiality_excluded_theoretical_bytes=excluded.theoretical_bytes,
    )

    clusters: list[DuplicateCluster] = []
    partial_writes: list[tuple[Path, int, float, str]] = []
    full_writes: list[tuple[Path, int, float, str]] = []
    counters = {"partial": 0, "full": 0, "buckets": 0}
    last_heartbeat = time.monotonic()

    def heartbeat() -> None:
        nonlocal last_heartbeat
        now = time.monotonic()
        if _due(last=last_heartbeat, now=now, interval=_HEARTBEAT_INTERVAL_SECONDS):
            logger.info(
                "dedup.progress",
                buckets_seen=counters["buckets"],
                partial_hashed=counters["partial"],
                full_hashed=counters["full"],
                candidate_files=candidate_count,
                clusters_found=len(clusters),
            )
            last_heartbeat = now

    def flush_writes() -> None:
        if partial_writes:
            index.store_partial_hashes(partial_writes)
            partial_writes.clear()
        if full_writes:
            index.store_full_hashes(full_writes)
            full_writes.clear()

    def check_cancel() -> None:
        if checkpoint is None:
            return
        try:
            checkpoint()
        except DedupCancelled:
            flush_writes()  # keep what this window already hashed: the resume mechanism
            raise

    def run_window(window: list[tuple[int, list[FileRecord]]]) -> None:
        check_cancel()
        try:
            with ThreadPoolExecutor(
                max_workers=_HASH_TIMEOUT_WORKERS, initializer=worker_initializer
            ) as executor:
                process_window(executor, window)
        except DedupCancelled:
            flush_writes()  # a cancel raised from inside a hash stage: keep its drained digests
            raise

    def process_window(
        executor: ThreadPoolExecutor, window: list[tuple[int, list[FileRecord]]]
    ) -> None:
        # Stage 1: 64KB head+tail partial hash of every file in the window, concurrently.
        partial_digests = _hash_stage(
            executor,
            [record for _, members in window for record in members],
            stage="partial",
            hash_cache=hash_cache,
            cached_lookup=_cached_partial_lookup,
            compute=_compute_partial_hash,
            with_size_arg=True,
            pending_writes=partial_writes,
            skips=skips,
            checkpoint=checkpoint,
        )
        counters["partial"] += len(partial_digests)
        heartbeat()
        check_cancel()

        # Same-size files that also share a partial hash are the only full-hash candidates.
        bucket_subsets: list[tuple[int, list[list[FileRecord]]]] = []
        full_needed: list[FileRecord] = []
        full_digests: dict[Path, str] = {}
        for size, members in window:
            partial_groups: dict[str, list[FileRecord]] = defaultdict(list)
            for record in members:
                digest = partial_digests.get(record.path)
                if digest is not None:
                    partial_groups[digest].append(record)
            subsets = [subset for subset in partial_groups.values() if len(subset) >= 2]
            bucket_subsets.append((size, subsets))
            for subset in subsets:
                for record in subset:
                    if size <= _PARTIAL_HASH_WHOLE_FILE_THRESHOLD:
                        # A file this small is hashed WHOLE by `_compute_partial_hash`, so its
                        # partial digest already IS the full-file BLAKE3 digest; reading it a
                        # second time cannot learn anything. Larger files still get a real
                        # full read below -- a partial match alone never makes a duplicate.
                        full_digests[record.path] = partial_digests[record.path]
                        entry = hash_cache.get(record.path.as_posix())
                        if _cached_full_lookup(entry, record.size_bytes, record.mtime) is None:
                            full_writes.append(
                                (
                                    record.path,
                                    record.size_bytes,
                                    record.mtime,
                                    full_digests[record.path],
                                )
                            )
                    else:
                        full_needed.append(record)
        # Stage 2: full BLAKE3 of the remaining candidates (the final arbiter), concurrently.
        full_digests.update(
            _hash_stage(
                executor,
                full_needed,
                stage="full",
                hash_cache=hash_cache,
                cached_lookup=_cached_full_lookup,
                compute=lambda path: _compute_full_hash(path),
                with_size_arg=False,
                pending_writes=full_writes,
                skips=skips,
                checkpoint=checkpoint,
            )
        )
        counters["full"] += len(full_digests)
        flush_writes()
        heartbeat()

        # Cluster assembly, in the same bucket / partial-group / digest order as always.
        for size, subsets in bucket_subsets:
            for subset in subsets:
                full_groups: dict[str, list[FileRecord]] = defaultdict(list)
                for record in subset:
                    digest = full_digests.get(record.path)
                    if digest is not None:
                        full_groups[digest].append(record)
                for full_hash, final_members in full_groups.items():
                    if len(final_members) < 2:
                        continue
                    keep = select_keep(final_members)
                    duplicates = tuple(m for m in final_members if m.path != keep.path)
                    clusters.append(
                        DuplicateCluster(
                            full_hash=full_hash, size_bytes=size, keep=keep, duplicates=duplicates
                        )
                    )

    window: list[tuple[int, list[FileRecord]]] = []
    window_files = 0
    rows = index.duplicate_size_candidates(min_reclaim_bytes=min_reclaim_bytes)
    try:
        for size, members_iter in itertools.groupby(rows, key=lambda record: record.size_bytes):
            counters["buckets"] += 1
            members = list(members_iter)  # bounded by this one bucket, not the whole candidate set
            if exclusion_patterns:
                members = [
                    m for m in members if first_matching_pattern(m.path, exclusion_patterns) is None
                ]
            window.append((size, members))
            window_files += len(members)
            if window_files >= _WINDOW_FILES:
                run_window(window)
                window, window_files = [], 0
        if window:
            run_window(window)
    finally:
        # Close the SELECT cursor NOW, not whenever the traceback releases the generator: a
        # cancel (or any error) unwinding out of the loop otherwise leaves the cursor open and
        # ScanIndex.close()'s WAL checkpoint then fails with "database table is locked".
        rows.close()
    flush_writes()
    buckets_seen = counters["buckets"]

    logger.info(
        "dedup.done",
        buckets_seen=buckets_seen,
        clusters=len(clusters),
        skipped=len(skips) if skips is not None else 0,
    )
    return clusters


def _keep_rationale(cluster: DuplicateCluster) -> str:
    """Concrete, honest rationale naming the kept path and the factual heuristic-relevant
    properties that led to it being kept — never a fabricated claim about which single rule
    was decisive, since that depends on the other members it was compared against."""
    if cluster.keep.git_repo_root is not None:
        location = f"inside git repository '{cluster.keep.git_repo_root}'"
    elif _is_downloads_or_temp(cluster.keep.path):
        location = "under a Downloads/Temp directory (shared by every member of this cluster)"
    else:
        location = "outside Downloads/Temp (not in a git repository)"
    created = datetime.fromtimestamp(cluster.keep.ctime, tz=UTC).isoformat()
    return (
        f"Exact duplicate of '{cluster.keep.path}' (byte-identical, BLAKE3 full-hash match); "
        f"kept copy is {location}, created {created}, path depth "
        f"{len(cluster.keep.path.parts)} — selected by the keep-heuristic (ADR-0007: prefer "
        "git-repo/project membership, then shallowest path, then oldest creation time, then "
        "lexicographic order)."
    )


def generate_duplicate_candidates(
    index: ScanIndex,
    config: Config,
    safety: SafetyValidator,
    *,
    skips: list[HashSkip] | None = None,
    clusters: Sequence[DuplicateCluster] | None = None,
    checkpoint: Callable[[], None] | None = None,
    worker_initializer: Callable[[], None] | None = None,
) -> list[Candidate]:
    """Mirrors `detectors.py::generate_candidates()`'s contract/shape: runs every non-keep
    cluster member through `SafetyValidator.evaluate()` before it is ever tagged a tier.
    `REVIEW_ONLY` -> forced Tier B; `ELIGIBLE` -> Tier A only if `config.categories.duplicates`
    is enabled, else Tier B. The kept member of a cluster is never evaluated for a tier and
    never appears in the output — it isn't being proposed for any action.

    `skips`, `checkpoint` and `worker_initializer` are forwarded to `find_duplicate_clusters`
    unchanged — see its docstring (ADR-0040 for the latter two).
    `min_reclaim_bytes` comes from `config.categories.duplicates.min_reclaim_bytes` — the
    materiality gate is config-driven, not hardcoded, same as every other category threshold.

    `clusters`: pass an already-computed cluster list (e.g. from a caller that also needs the
    keep/delete shape directly, like the dashboard's cluster-review endpoint) to skip a second
    full `find_duplicate_clusters` pass — hashing every candidate file twice on a multi-million-
    file real disk index is expensive, and a stale-quarantine-path `hash_unreadable` warning gets
    logged once per pass, so calling this twice back-to-back doubles both the runtime and the
    log noise for no benefit. Defaults to `None`, computing clusters here exactly as before.

    ADR-0006: each cluster's non-keep members also go through `linkinfo.
    estimate_reclaimable_bytes` (one direct `os.stat()` per member, bounded by cluster size —
    never the whole inventory). A "duplicate" that's actually a hardlink to the kept copy
    already shares the same on-disk blocks and reclaims 0 bytes if deleted; byte-identical
    content — this category's entire selection criterion — is exactly what a hardlink produces,
    so this is not a rare edge case here. Exposed as `Candidate.reclaimable_bytes`, distinct
    from `size_bytes`'s logical size and never silently substituted for it.

    ADR-0007, two safety checks beyond the per-member evaluation:
    1. If ANY non-kept member is `BLOCKED` (a protected root, git-repo membership, a protected
       extension, ...), the WHOLE cluster is excluded — not just that one member, silently
       proposing the rest as if nothing were unusual about this byte-identical group. A cluster
       with a protected member deserves a human looking at the whole group, not a partial
       candidate list with no visibility into what got left out.
    2. If the kept copy would be the cluster's sole surviving location and that location is
       risky (Downloads/Temp/a cloud-sync placeholder) while a more durable copy is being
       deleted, every surviving candidate in the cluster is forced to Tier B (review), regardless
       of `config.categories.duplicates.enabled` — see `cluster_needs_manual_review`.

    ADR-0008, one more filter BEFORE the above two, on a per-member basis (not whole-cluster):
    a path under a model-cache root, matching the HF blob/snapshot cache structure, or that
    would be deleted from one live conda/venv environment to keep another's byte-identical copy
    is dropped from `cluster.duplicates` entirely — it never reaches safety evaluation or
    tiering, because it was never a legitimate `exact_duplicate` candidate in the first place
    (see `_dedup_ineligibility_reason`). A cluster left with zero remaining duplicates after this
    filter contributes no candidates at all.
    """
    candidates: list[Candidate] = []
    min_reclaim_bytes = config.categories.duplicates.min_reclaim_bytes
    resolved_clusters = (
        clusters
        if clusters is not None
        else find_duplicate_clusters(
            index,
            min_reclaim_bytes=min_reclaim_bytes,
            skips=skips,
            exclusion_patterns=safety.exclusion_patterns,
            checkpoint=checkpoint,
            worker_initializer=worker_initializer,
        )
    )
    model_cache_roots = [Path(p) for p in config.categories.model_caches.paths]
    env_root_cache: dict[Path, bool] = {}  # one pass only -- see `_is_environment_root`
    for cluster in resolved_clusters:
        eligible_duplicates: list[FileRecord] = []
        for duplicate in cluster.duplicates:
            reason = _dedup_ineligibility_reason(
                duplicate, cluster.keep, model_cache_roots, env_root_cache
            )
            if reason is not None:
                logger.info(
                    "dedup.member_excluded",
                    path=str(duplicate.path),
                    keep=str(cluster.keep.path),
                    reason=reason,
                )
                continue
            eligible_duplicates.append(duplicate)
        if not eligible_duplicates:
            continue
        cluster = _dataclass_replace(cluster, duplicates=tuple(eligible_duplicates))

        member_results = {
            duplicate.path: safety.evaluate(duplicate) for duplicate in cluster.duplicates
        }
        if any(result.verdict == Verdict.BLOCKED for result in member_results.values()):
            blocked_paths = [
                str(path)
                for path, result in member_results.items()
                if result.verdict == Verdict.BLOCKED
            ]
            logger.info(
                "dedup.cluster_excluded_protected_member",
                keep=str(cluster.keep.path),
                blocked_paths=blocked_paths,
            )
            continue

        rationale = _keep_rationale(cluster)
        needs_review = cluster_needs_manual_review(cluster)
        if needs_review:
            rationale = (
                f"{rationale} FLAGGED FOR REVIEW: the kept copy sits in a Downloads/Temp/"
                "cloud-placeholder location while at least one deleted copy sat somewhere more "
                "durable — auto-applying this cluster would strand the sole survivor somewhere "
                "less durable than what was thrown away."
            )
        reclaim_estimates = estimate_reclaimable_bytes(
            [(duplicate.path, duplicate.size_bytes) for duplicate in cluster.duplicates]
        )
        for duplicate in cluster.duplicates:
            result = member_results[duplicate.path]
            # Stage 2 safety boundary: forced to Tier B whenever config.mode is Mode.SAFE,
            # independent of the REVIEW_ONLY/duplicates.enabled logic below — a second,
            # redundant layer on top of `config.apply_safe_mode_category_overrides` already
            # forcing `duplicates.enabled=False` upstream (detectors.py's generate_candidates
            # docstring explains the same "don't depend on only one layer" reasoning).
            if config.mode == Mode.SAFE or result.verdict == Verdict.REVIEW_ONLY or needs_review:
                tier = Tier.B
            else:
                tier = Tier.A if config.categories.duplicates.enabled else Tier.B
            estimate = reclaim_estimates[duplicate.path]
            member_rationale = rationale
            if estimate.resolved and estimate.reclaimable_bytes == 0:
                member_rationale = (
                    f"{member_rationale} Already deduplicated at the filesystem level (this "
                    "path is a hardlink sharing the same on-disk blocks as another surviving "
                    "copy) — 0 bytes reclaimable if deleted, excluded from the reclaimable "
                    "total."
                )
            candidates.append(
                Candidate(
                    path=duplicate.path,
                    is_dir=False,
                    category=_CATEGORY,
                    category_group=_CATEGORY_GROUP,
                    size_bytes=duplicate.size_bytes,
                    tier=tier,
                    rationale=member_rationale,
                    rebuild_instruction=None,
                    safety_verdict=result.verdict,
                    safety_reason_code=result.reason_code,
                    retention_days=config.categories.duplicates.retention_days,
                    reclaimable_bytes=estimate.reclaimable_bytes,
                    # P0-K1a: scan-time identity baseline -- `duplicate` is the FileRecord this
                    # candidate is built from, already read above; no new os.stat().
                    dev=duplicate.dev,
                    ino=duplicate.ino,
                    mtime=duplicate.mtime,
                )
            )
    return candidates
