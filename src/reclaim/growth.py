from __future__ import annotations

import bisect
import os
import shutil
import sqlite3
import stat
import statistics
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import structlog

from reclaim.config import Config, exclusion_patterns
from reclaim.dirlist import ListingUnsupported, list_directory
from reclaim.index import ScanIndex
from reclaim.models import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FILE_ATTRIBUTE_REPARSE_POINT
from reclaim.safety import first_matching_pattern
from reclaim.safety_env import assert_not_real_profile_under_pytest
from reclaim.scanner import long_path

# Disk growth attribution (docs/specs/disk-growth-attribution.md, ADR-0046): a READ-ONLY,
# budgeted sampler of folder sizes plus the pure analysis (window growth, anomaly detection) over
# the stored series. Nothing here deletes, moves or opens a file; the only write is Reclaim's own
# growth.sqlite3.

logger = structlog.get_logger(__name__)

GIB = 1024**3
RETENTION_DAYS = 400  # spec §1.6
_DIR_ATTRIBUTE = 0x10
_SYSTEM_FILES = ("pagefile.sys", "hiberfil.sys", "swapfile.sys")
# One aggregate line for everything the user excluded (spec §1.3): never walked, never named.
EXCLUDED_KEY = "<excluded projects>"
_TOTAL_SUFFIX = "|total"
TOTAL_SUFFIX = _TOTAL_SUFFIX  # the volume-total key is path + this suffix
_OPAQUE_KEYS_MAX = 40  # biggest index opaque roots kept per snapshot: bounds the store's row count
_DRILL_SHARE = 0.6  # a descendant explaining >= 60% of an ancestor's growth is the better name

Entry = tuple[str, int, int]  # (name, attributes, size) of one listed directory entry
ListFn = Callable[[str], list[Entry]]
Point = tuple[float, int]  # (unix seconds, bytes)
# Spec 1.5 window freshness (see AnomalyConfig): 6 h is 1.5x the 4-hourly snapshot cadence.
DEFAULT_START_SLACK_S = 6 * 3600.0
DEFAULT_END_AGE_S = 3600.0


def norm(path: str | Path) -> str:
    """Identity of a path for de-duplication and containment checks (case/slash-insensitive)."""
    return os.path.normcase(os.path.normpath(str(path)))


def is_within(child: str, parent: str) -> bool:
    """True when normalised `child` is `parent` or lies below it."""
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def fmt_gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


# ---------------------------------------------------------------- data classes


@dataclass(frozen=True)
class Root:
    """One thing to size: a folder (walked) or a prepared aggregate (opaque/excluded)."""

    path: str
    kind: str  # cache | scratch | toplevel | sysfile | opaque | excluded | volume
    label: str
    project: str = ""
    session: str = ""


@dataclass(frozen=True)
class Measurement:
    root: Root
    bytes: int
    partial: bool = False  # lower bound: never used as a growth endpoint
    observed_at: float | None = None  # index-sourced rows carry the index scan time


@dataclass
class Snapshot:
    taken_at: float
    budget_s: float
    complete: bool
    sizes: list[Measurement] = field(default_factory=list)


@dataclass(frozen=True)
class Environment:
    """Everything a snapshot needs to know about the machine; tests inject tmp_path roots."""

    profile: Path
    temp: Path
    volumes: list[Path]
    project_roots: list[Path]
    cache_paths: list[str]
    exclusions: list[str]


def environment_from_config(config: Config, volumes: Sequence[Path] | None = None) -> Environment:
    """Builds the real-machine Environment. Cache paths are the ones the package/model-cache
    detectors already use (so custom `[categories.*] paths` are honoured) plus Docker's data dir."""
    from reclaim.drives import list_fixed_drives

    profile = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or profile / "AppData" / "Local")
    temp = Path(tempfile.gettempdir())
    caches = [
        *config.categories.package_caches.paths,
        *config.categories.model_caches.paths,
        str(local / "Docker"),  # Docker Desktop + its WSL2 vhdx live here
        str(temp),
    ]
    return Environment(
        profile=profile,
        temp=temp,
        volumes=list(volumes) if volumes is not None else list_fixed_drives(),
        project_roots=[Path(p).expanduser() for p in config.growth.project_roots],
        cache_paths=caches,
        exclusions=exclusion_patterns(config),
    )


# ---------------------------------------------------------------- bounded walker


def make_default_lister() -> ListFn:
    """Fast NTFS listing (sizes come with it); `os.scandir` for non-NTFS or on any listing error
    that is not an access failure. The volume serial is cached per drive after the first call."""
    serials: dict[str, int] = {}

    def lister(path: str) -> list[Entry]:
        drive = os.path.splitdrive(path)[0].lower()
        try:
            serial, listed = list_directory(long_path(Path(path)), serials.get(drive))
            serials[drive] = serial
            return [(e[0], e[1], e[2]) for e in listed]
        except ListingUnsupported:
            pass
        out: list[Entry] = []
        with os.scandir(path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append((entry.name, getattr(st, "st_file_attributes", 0), st.st_size))
        return out

    return lister


class Budget:
    """One shared wall-clock and listed-entry budget for a whole snapshot."""

    def __init__(self, wall_s: float, max_entries: int, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._deadline = clock() + wall_s
        self._max_entries = max_entries
        self.entries = 0

    def spend(self, entries: int) -> None:
        self.entries += entries

    @property
    def exhausted(self) -> bool:
        return self.entries >= self._max_entries or self._clock() >= self._deadline


@dataclass
class _Frame:
    path: str
    total: int = 0
    subdirs: list[str] = field(default_factory=list)
    complete: bool = True


class SkippedRoot(OSError):
    """A root the walker refuses to enter. Subclasses OSError so it is handled exactly like an
    absent or unreadable root: no row is stored for it (not a 0-byte "complete" size, which would
    read as a measurement; not a partial lower bound either, since nothing was measured)."""


def is_reparse_point(path: str) -> bool:
    """True when `path` itself is a junction or symlink (metadata only, never follows it); a
    path that cannot be examined is treated as not a reparse point (the listing will then fail
    the same way and the root is skipped as unreadable)."""
    try:
        st = os.lstat(long_path(Path(path)))
    except OSError:
        return False
    return bool(
        getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    ) or stat.S_ISLNK(st.st_mode)


class Walker:
    """Sums file sizes below a root. Never follows a reparse point, never lists an excluded
    directory (matched by name before any IO), and stops at the shared budget. `memo` holds the
    totals of directories that are themselves roots, so nested roots are not listed twice."""

    def __init__(
        self,
        list_fn: ListFn,
        budget: Budget,
        exclusions: Sequence[str],
        reparse_fn: Callable[[str], bool] | None = None,
        resolve_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._list = list_fn
        self.budget = budget
        self._exclusions = tuple(exclusions)
        self._is_reparse = reparse_fn or is_reparse_point
        self._resolve = resolve_fn or os.path.realpath
        self.marked: set[str] = set()  # norm paths of roots: their totals are memoised
        self.memo: dict[str, int] = {}
        self.excluded: dict[str, str] = {}  # norm path -> real path, internal only (never stored)

    def is_excluded(self, path: str) -> bool:
        return bool(self._exclusions) and (
            first_matching_pattern(path, self._exclusions, is_dir=True) is not None
        )

    def note_if_excluded(self, path: str) -> bool:
        """True (and remembered for the "Excluded projects" aggregate, never stored by name) when
        `path` is excluded: the one check every kind of root goes through before any IO."""
        if self.is_excluded(path):
            self.excluded[norm(path)] = path
            return True
        return False

    def children(self, path: str) -> tuple[list[tuple[str, str]], list[Entry]]:
        """`(dirs as (name, full path), files)` of `path`; raises OSError if unreadable."""
        entries = self._list(path)
        self.budget.spend(len(entries) + 1)
        dirs: list[tuple[str, str]] = []
        files: list[Entry] = []
        for name, attrs, size in entries:
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                continue  # a junction/symlink is never followed (and is not counted)
            if attrs & _DIR_ATTRIBUTE:
                full = os.path.join(path, name)  # noqa: PTH118 -- str paths
                if self.is_excluded(full):
                    self.excluded[norm(full)] = full
                else:
                    dirs.append((name, full))
            elif not attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:  # cloud stubs use no disk
                files.append((name, attrs, size))
        return dirs, files

    def _enter(self, path: str, *, root: bool) -> _Frame:
        try:
            dirs, files = self.children(path)
        except OSError:
            if root:
                raise
            return _Frame(path)  # unreadable subtree: contributes 0 (shows as "not attributed")
        return _Frame(path, sum(f[2] for f in files), [d[1] for d in reversed(dirs)])

    def total(self, root: str) -> tuple[int, bool]:
        """`(bytes, complete)`; a root not finished within the budget is a lower bound. Raises
        `SkippedRoot` (an OSError, so the caller treats it like an absent root) when the root is
        itself a junction/symlink or lies below one that leads into an excluded path: the walk
        only skips reparse points it meets inside a tree, so the root is checked here, with
        metadata calls only and before anything is listed."""
        if (cached := self.memo.get(norm(root))) is not None:
            return cached, True
        if self._is_reparse(root):
            raise SkippedRoot(f"root is a reparse point: {root}")
        if self.is_excluded(self._resolve(root)):
            raise SkippedRoot("root resolves into an excluded path")
        stack = [self._enter(root, root=True)]
        while True:
            frame = stack[-1]
            if frame.subdirs and not self.budget.exhausted:
                child = frame.subdirs.pop()
                if (cached := self.memo.get(norm(child))) is not None:
                    frame.total += cached
                else:
                    stack.append(self._enter(child, root=False))
                continue
            stack.pop()
            if frame.subdirs:
                frame.complete = False  # budget ran out with directories still unlisted
            key = norm(frame.path)
            if frame.complete and key in self.marked:
                self.memo[key] = frame.total
            if not stack:
                return frame.total, frame.complete
            stack[-1].total += frame.total
            stack[-1].complete = stack[-1].complete and frame.complete


# ---------------------------------------------------------------- snapshot collection


def _plan_roots(env: Environment, walker: Walker) -> tuple[list[Root], list[Measurement]]:
    """Roots to walk (the most specific kind wins a duplicate path) and the volume-root system
    files, whose sizes come straight from the root listing without opening them."""
    roots: dict[str, Root] = {}
    sysfiles: list[Measurement] = []

    def add(root: Root) -> None:
        roots.setdefault(norm(root.path), root)

    for cache in env.cache_paths:
        if walker.note_if_excluded(cache):
            continue  # a cache root inside an excluded path is never walked or named (spec 1.3)
        add(Root(os.path.normpath(cache), "cache", Path(cache).name or cache))
    claude = os.path.join(env.temp, "claude")  # noqa: PTH118 -- str paths
    try:
        if walker.note_if_excluded(claude):
            raise FileNotFoundError(claude)  # same as an absent scratch dir: nothing to list
        projects, _ = walker.children(claude)
        for project, project_dir in projects:
            try:
                sessions, _ = walker.children(project_dir)
            except OSError:
                continue
            for session, session_dir in sessions:
                add(Root(session_dir, "scratch", f"{project}\\{session}", project, session))
    except OSError:
        pass
    parents = [env.profile, *env.volumes, *env.project_roots]
    for parent in parents:
        if walker.note_if_excluded(str(parent)):
            continue  # e.g. a project root that is itself an excluded project
        try:
            dirs, files = walker.children(str(parent))
        except OSError:
            continue
        for name, full in dirs:
            add(Root(full, "toplevel", name))
        if parent in env.volumes:
            for fname, _attrs, size in files:
                if fname.lower() in _SYSTEM_FILES:
                    root = Root(os.path.join(parent, fname), "sysfile", fname)  # noqa: PTH118 -- str paths
                    sysfiles.append(Measurement(root, size))
    return list(roots.values()), sysfiles


def _index_aggregates(index: ScanIndex, env: Environment, walker: Walker) -> list[Measurement]:
    """Opaque roots (node_modules, .venv...) under the profile/project roots, from the index:
    no filesystem IO, stamped with the index's own scan time. One under an excluded path is never
    a key of its own: it is remembered for the "Excluded projects" aggregate (spec 1.3)."""
    out: list[Measurement] = []
    seen: set[str] = set()
    for base in [env.profile, *env.project_roots]:
        for path, agg, scanned in index.opaque_aggregates(base, limit=_OPAQUE_KEYS_MAX):
            if walker.note_if_excluded(path):
                continue
            if norm(path) not in seen:
                seen.add(norm(path))
                out.append(Measurement(Root(path, "opaque", Path(path).name), agg, False, scanned))
    out.sort(key=lambda m: -m.bytes)
    return out[:_OPAQUE_KEYS_MAX]


def collect_snapshot(
    env: Environment,
    *,
    last_seen: Mapping[str, float] | None = None,
    index: ScanIndex | None = None,
    budget_s: float = 120.0,
    budget_entries: int = 3_000_000,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
    list_fn: ListFn | None = None,
    disk_usage: Callable[[str], tuple[int, int, int]] = shutil.disk_usage,
) -> Snapshot:
    """One bounded snapshot. Roots are sized stalest-first (never-seen first) under a single
    shared budget; a root cut off by the budget is stored `partial`, roots not reached are
    simply absent (they are first in line next time) and the snapshot is marked incomplete."""
    last_seen = last_seen or {}
    budget = Budget(budget_s, budget_entries, clock)
    snap = Snapshot(taken_at=now(), budget_s=budget_s, complete=True)
    for volume in env.volumes:
        total, used, _free = disk_usage(str(volume))
        snap.sizes.append(Measurement(Root(str(volume), "volume", str(volume)), used))
        snap.sizes.append(
            Measurement(Root(str(volume) + _TOTAL_SUFFIX, "volume_total", str(volume)), total)
        )
    walker = Walker(list_fn or make_default_lister(), budget, env.exclusions)
    walked, sysfiles = _plan_roots(env, walker)
    walker.marked.update(norm(r.path) for r in walked)
    snap.sizes.extend(sysfiles)
    kind_rank = {"cache": 0, "scratch": 0, "toplevel": 1}
    walked.sort(key=lambda r: (last_seen.get(norm(r.path), 0.0), kind_rank.get(r.kind, 2), r.path))
    for root in walked:
        if walker.budget.exhausted:
            snap.complete = False
            break
        try:
            size, complete = walker.total(root.path)
        except OSError:
            continue  # absent/unreadable root (e.g. a cache this machine does not have)
        snap.sizes.append(Measurement(root, size, partial=not complete))
        snap.complete = snap.complete and complete
    index_ok = index is not None
    if index is not None:
        try:
            # before the aggregate: it also learns which opaque roots are excluded
            snap.sizes.extend(_index_aggregates(index, env, walker))
        except sqlite3.Error:
            # a locked, busy or foreign index only costs the opaque keys (the folder sizes do not
            # need it); the excluded aggregate is then a lower bound
            logger.warning("growth.index_unreadable", exc_info=True)
            index_ok = False
    if env.exclusions:
        snap.sizes.append(_excluded_measurement(walker, index if index_ok else None))
    return snap


def _excluded_measurement(walker: Walker, index: ScanIndex | None) -> Measurement:
    root = Root(EXCLUDED_KEY, "excluded", "Excluded projects")
    if index is None:
        return Measurement(root, 0, partial=True)  # unknown, not zero
    # An excluded path nested in another (an opaque root inside an excluded project) is part of
    # the outer one's subtree size already: count each byte once.
    outer: list[str] = []
    for key in sorted(walker.excluded, key=len):
        if not any(is_within(key, o) for o in outer):
            outer.append(key)
    try:
        total = sum(index.subtree_size_bytes(Path(walker.excluded[k])) for k in outer)
    except sqlite3.Error:
        logger.warning("growth.index_unreadable", exc_info=True)
        return Measurement(root, 0, partial=True)
    return Measurement(root, total, partial=False, observed_at=None)


def hide_excluded(series: Mapping[str, KeySeries], patterns: Sequence[str]) -> dict[str, KeySeries]:
    """`series` without any key whose path is excluded: the read-side twin of the snapshot-time
    rule, applied by every consumer (API, CLI report, anomalies, toasts) so a key stored before an
    exclusion existed is never named. Volume and aggregate keys are kept (they are not paths of
    a project). The key is DROPPED, not folded into the "Excluded projects" series: that series
    comes from the index at each snapshot, and adding a key's history to it would invent a step
    in the aggregate at the moment the exclusion was added. The aggregate catches up at the next
    snapshot, which measures the now-excluded folder through the index."""
    if not patterns:
        return dict(series)
    return {
        path: ks
        for path, ks in series.items()
        if ks.root.kind in ("volume", "volume_total", "excluded")
        or first_matching_pattern(path, patterns, is_dir=True) is None
    }


# ---------------------------------------------------------------- store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE COLLATE NOCASE, kind TEXT NOT NULL,
    label TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY, taken_at REAL NOT NULL, budget_s REAL NOT NULL, complete INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sizes (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    key_id INTEGER NOT NULL REFERENCES keys(id), bytes INTEGER NOT NULL, partial INTEGER NOT NULL,
    observed_at REAL, PRIMARY KEY (snapshot_id, key_id)) WITHOUT ROWID;
"""  # noqa: E501 -- schema text, kept compact on purpose


@dataclass
class KeySeries:
    root: Root
    points: list[Point] = field(default_factory=list)  # complete samples only, time-ordered


class GrowthStore:
    """`data/growth.sqlite3` (spec §1.6). `observed_at` is an addition to the spec's columns: the
    index-sourced rows are only as fresh as the last index scan, and the series must say so."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        """`read_only` (the API and the toast text): the file is opened `mode=ro` and neither
        created nor given the schema, so an empty or foreign file stays byte-for-byte as it was;
        reading it then raises sqlite3.Error (no such table / not a database) for the caller to
        treat as "no data"."""
        assert_not_real_profile_under_pytest(path, operation="open the growth store at")
        self._path = path
        if read_only:
            self._conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> GrowthStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write(self, snap: Snapshot) -> int:
        """Stores a snapshot and drops those older than RETENTION_DAYS (and orphaned keys)."""
        assert_not_real_profile_under_pytest(self._path, operation="write the growth store at")
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO snapshots(taken_at, budget_s, complete) VALUES (?,?,?)",
                (snap.taken_at, snap.budget_s, int(snap.complete)),
            )
            snap_id = int(cur.lastrowid or 0)
            for m in snap.sizes:
                r = m.root
                self._conn.execute(
                    "INSERT INTO keys(path, kind, label, project, session) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, label=excluded.label, "
                    "project=excluded.project, session=excluded.session",
                    (r.path, r.kind, r.label, r.project, r.session),
                )
                key_id = self._conn.execute(
                    "SELECT id FROM keys WHERE path = ?", (r.path,)
                ).fetchone()[0]
                self._conn.execute(
                    "INSERT OR REPLACE INTO sizes VALUES (?,?,?,?,?)",
                    (snap_id, key_id, m.bytes, int(m.partial), m.observed_at),
                )
            cutoff = snap.taken_at - RETENTION_DAYS * 86400
            dropped = self._conn.execute("DELETE FROM snapshots WHERE taken_at < ?", (cutoff,))
            if dropped.rowcount:  # the orphan scan is only worth its cost when something went
                self._conn.execute(
                    "DELETE FROM keys WHERE id NOT IN (SELECT DISTINCT key_id FROM sizes)"
                )
        return snap_id

    def last_seen(self) -> dict[str, float]:
        """norm path -> time of the latest measurement of any kind (drives stalest-first)."""
        rows = self._conn.execute(
            "SELECT k.path, MAX(s.taken_at) FROM sizes z JOIN keys k ON k.id = z.key_id "
            "JOIN snapshots s ON s.id = z.snapshot_id GROUP BY k.id"
        )
        return {norm(p): t for p, t in rows}

    def partial_latest_keys(self) -> set[str]:
        """Paths whose most recent stored measurement was cut off by the budget (a lower bound):
        their series ends earlier than their real size, so a growth figure may lag."""
        rows = self._conn.execute(
            "SELECT k.path FROM sizes z JOIN keys k ON k.id = z.key_id "
            "JOIN snapshots s ON s.id = z.snapshot_id WHERE z.partial = 1 AND s.taken_at = "
            "(SELECT MAX(s2.taken_at) FROM sizes z2 JOIN snapshots s2 ON s2.id = z2.snapshot_id "
            "WHERE z2.key_id = z.key_id)"
        )
        return {str(p) for (p,) in rows}

    def last_snapshot_time(self) -> float | None:
        row = self._conn.execute("SELECT MAX(taken_at) FROM snapshots").fetchone()
        return None if row[0] is None else float(row[0])

    def load_series(self) -> dict[str, KeySeries]:
        """All keys' complete samples. A sample's time is its `observed_at` when it has one;
        repeats of the same observation (index rows between index scans) collapse to one."""
        out: dict[str, KeySeries] = {}
        rows = self._conn.execute(
            "SELECT k.path, k.kind, k.label, k.project, k.session, s.taken_at, z.bytes, "
            "z.observed_at FROM sizes z JOIN keys k ON k.id = z.key_id "
            "JOIN snapshots s ON s.id = z.snapshot_id WHERE z.partial = 0 "
            "ORDER BY k.id, s.taken_at"
        )
        for path, kind, label, project, session, taken, size, observed in rows:
            ks = out.setdefault(path, KeySeries(Root(path, kind, label, project, session)))
            t = float(observed if observed is not None else taken)
            if not ks.points or ks.points[-1][0] != t:
                ks.points.append((t, int(size)))
        return out


def snapshot_due(store: GrowthStore, min_interval_hours: float, now: float) -> bool:
    """True when the last snapshot is older than `min_interval_hours` (or none exists)."""
    last = store.last_snapshot_time()
    return last is None or now - last >= min_interval_hours * 3600


# ---------------------------------------------------------------- analysis (pure)


def value_at_or_before(points: Sequence[Point], t: float) -> Point | None:
    i = bisect.bisect_right([p[0] for p in points], t)
    return points[i - 1] if i else None


def window_growth(
    points: Sequence[Point],
    now: float,
    window_s: float,
    max_start_slack_s: float | None = None,
    max_end_age_s: float | None = None,
) -> tuple[int, Point] | None:
    """`(growth, start_point)`: latest complete value (<= now) minus the latest complete value
    at or before the window start; None when either endpoint does not exist. With the optional
    limits (spec 1.5: growth "within" the window, on a fresh snapshot) None is also returned
    when the start sample is older than `window_s + max_start_slack_s` or the newest sample is
    older than `max_end_age_s`: after missed snapshots the gap is not growth "in the window".
    Both limits are inclusive."""
    end = value_at_or_before(points, now)
    start = value_at_or_before(points, now - window_s)
    if end is None or start is None or end[0] <= start[0]:
        return None
    if max_start_slack_s is not None and now - start[0] > window_s + max_start_slack_s:
        return None
    if max_end_age_s is not None and now - end[0] > max_end_age_s:
        return None
    return end[1] - start[1], start


@dataclass(frozen=True)
class GrowthRow:
    path: str
    kind: str
    label: str
    project: str
    session: str
    growth: int
    current: int


@dataclass(frozen=True)
class VolumeDelta:
    volume: str
    delta: int
    attributed: int
    not_attributed: int
    status: str = "ok"  # "insufficient data": no fresh enough samples for this window


@dataclass(frozen=True)
class GrowthReport:
    window_s: float
    rows: list[GrowthRow]
    volumes: list[VolumeDelta]
    insufficient_history: int


def _attributable(ks: KeySeries) -> bool:
    return ks.root.kind not in ("volume", "volume_total", "excluded")


def non_nested(rows: Sequence[GrowthRow]) -> list[GrowthRow]:
    """Largest-first subset with no row containing or contained in another (no double counting
    of one byte through a folder and its subfolder)."""
    chosen: list[GrowthRow] = []
    for row in sorted(rows, key=lambda r: -r.growth):
        p = norm(row.path)
        if not any(is_within(p, norm(c.path)) or is_within(norm(c.path), p) for c in chosen):
            chosen.append(row)
    return chosen


def growth_report(
    series: Mapping[str, KeySeries],
    window_s: float,
    now: float,
    max_start_slack_s: float | None = DEFAULT_START_SLACK_S,
    max_end_age_s: float | None = DEFAULT_END_AGE_S,
) -> GrowthReport:
    """Ranks the keys by growth over the window. A key (or volume) whose start sample is older
    than `window_s + max_start_slack_s` or whose newest sample is older than `max_end_age_s`
    has no number for this window; a volume in that state is listed as "insufficient data"."""
    rows: list[GrowthRow] = []
    volumes: dict[str, int] = {}
    stale_volumes: list[str] = []
    insufficient = 0
    for ks in series.values():
        res = window_growth(ks.points, now, window_s, max_start_slack_s, max_end_age_s)
        if res is None:
            insufficient += 1
            if ks.root.kind == "volume":
                stale_volumes.append(ks.root.path)
            continue
        if ks.root.kind == "volume":
            volumes[ks.root.path] = res[0]
        elif _attributable(ks):
            r = ks.root
            rows.append(
                GrowthRow(r.path, r.kind, r.label, r.project, r.session, res[0], ks.points[-1][1])
            )
    rows.sort(key=lambda r: -r.growth)
    deltas: list[VolumeDelta] = []
    for vol, delta in sorted(volumes.items()):
        mine = [r for r in rows if is_within(norm(r.path), norm(vol))]
        attributed = sum(r.growth for r in non_nested(mine) if r.growth > 0)
        deltas.append(VolumeDelta(vol, delta, attributed, delta - attributed))
    deltas.extend(VolumeDelta(vol, 0, 0, 0, "insufficient data") for vol in sorted(stale_volumes))
    return GrowthReport(window_s, rows, deltas, insufficient)


def drill_down(rows: Sequence[GrowthRow], start: GrowthRow) -> GrowthRow:
    """Names the most specific folder behind `start`: while a descendant explains >= 60% of the
    current folder's growth, descend into the largest such descendant."""
    cur = start
    while True:
        p = norm(cur.path)
        inner = [
            r
            for r in rows
            if norm(r.path) != p
            and is_within(norm(r.path), p)
            and r.growth >= _DRILL_SHARE * cur.growth
        ]
        if not inner:
            return cur
        cur = max(inner, key=lambda r: (r.growth, len(r.path)))


def contributors(rows: Sequence[GrowthRow], limit: int = 3) -> list[GrowthRow]:
    """Top contributors, each drilled down to its most specific folder, no overlaps."""
    pool = [r for r in rows if r.growth > 0]
    out: list[GrowthRow] = []
    for top in sorted(pool, key=lambda r: -r.growth):
        picked = drill_down(pool, top)
        pp = norm(picked.path)
        if any(is_within(pp, norm(c.path)) or is_within(norm(c.path), pp) for c in out):
            continue
        if any(is_within(norm(c.path), norm(top.path)) for c in out):
            continue
        out.append(picked)
        if len(out) == limit:
            break
    return out


@dataclass(frozen=True)
class AnomalyConfig:
    abs_floor: int = 5 * GIB
    share_floor: float = 0.02
    z_min: float = 4.0
    eps_per_day: float = 0.1 * GIB  # a flat series must not alarm on noise
    history_s: float = 14 * 86400
    windows_h: tuple[int, ...] = (24, 72)
    thin_s: float = 1800.0  # sub-30-minute samples are thinned: rates are meant to be hourly
    min_ref_intervals: int = 6
    # A window's start sample may be at most window + this old, and the newest sample at most
    # max_end_age_h old (spec 1.5). 6 h is 1.5x the 4-hourly snapshot cadence: one missed
    # snapshot still counts, a longer gap is not growth "in 3 days". Same defaults as
    # GrowthConfig.
    max_start_slack_h: float = 6.0
    max_end_age_h: float = 1.0


@dataclass(frozen=True)
class Anomaly:
    scope: str  # "key" | "volume"
    path: str  # the key, or the volume
    growth: int
    window_h: int
    z: float | None
    attributed: str | None  # most specific folder behind the growth
    explanation: str


def _thin(points: Sequence[Point], gap_s: float) -> list[Point]:
    out: list[Point] = []
    for p in points:
        if not out or p[0] - out[-1][0] >= gap_s:
            out.append(p)
    return out


def robust_z(rate: float, reference: Sequence[float], eps: float) -> float:
    """(rate - median) / (1.4826 * MAD + eps) over the reference rates."""
    med = statistics.median(reference)
    mad = statistics.median(abs(r - med) for r in reference)
    return (rate - med) / (1.4826 * mad + eps)


def key_z(
    points: Sequence[Point], start_t: float, rate_h: float, cfg: AnomalyConfig
) -> float | None:
    """z of `rate_h` (bytes/hour) against the key's rates in the 14 days before `start_t`."""
    ref = _thin([p for p in points if start_t - cfg.history_s <= p[0] <= start_t], cfg.thin_s)
    rates = [(b[1] - a[1]) / ((b[0] - a[0]) / 3600) for a, b in pairwise(ref)]
    if len(rates) < cfg.min_ref_intervals:
        return None  # not enough history to call anything unusual
    return robust_z(rate_h, rates, cfg.eps_per_day / 24)


def detect_anomalies(
    keys: Mapping[str, KeySeries],
    volume: str,
    volume_used: Sequence[Point],
    volume_total: int,
    now: float,
    cfg: AnomalyConfig | None = None,
) -> list[Anomaly]:
    """Anomalies visible at time `now` (spec §1.5), using only complete samples <= now. Pure:
    the eval replays it over recorded series. `keys` holds attributable keys only."""
    cfg = cfg or AnomalyConfig()
    floor = max(cfg.abs_floor, cfg.share_floor * volume_total)
    slack_s, end_age_s = cfg.max_start_slack_h * 3600.0, cfg.max_end_age_h * 3600.0
    found: list[Anomaly] = []
    flagged: set[str] = set()
    vol_name = volume.rstrip("\\/") or volume
    for window_h in sorted(cfg.windows_h):
        window_s = window_h * 3600.0
        rows = growth_report(keys, window_s, now, slack_s, end_age_s).rows
        for row in rows:
            if row.growth < floor:
                break
            pts = keys[row.path].points
            res = window_growth(pts, now, window_s, slack_s, end_age_s)
            if res is None:
                continue
            z = key_z(pts, res[1][0], row.growth / window_h, cfg)
            if z is not None and z >= cfg.z_min and row.path not in flagged:
                flagged.add(row.path)
                where = drill_down(rows, row)
                text = f"{row.label} grew {fmt_gib(row.growth)} in {window_h} h"
                found.append(Anomaly("key", row.path, row.growth, window_h, z, where.path, text))
        vres = window_growth(volume_used, now, window_s, slack_s, end_age_s)
        if vres is not None and vres[0] >= cfg.abs_floor:
            top = contributors(rows, 2)
            days = window_h / 24
            parts = "; ".join(f"{fmt_gib(r.growth)} from {r.label}" for r in top)
            text = f"{vol_name} grew {fmt_gib(vres[0])} in {days:g} days"
            explanation = f"{text} — {parts}" if parts else f"{text} — not attributed"
            attributed = top[0].path if top else None
            found.append(
                Anomaly("volume", volume, vres[0], window_h, None, attributed, explanation)
            )
            break  # the shortest qualifying window is the one worth reporting
    return found
