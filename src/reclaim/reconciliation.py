from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from reclaim.index import ScanIndex, physical_size_bytes


class NotAVolumeRootError(ValueError):
    """Raised by `compute_disk_reconciliation` when the given path isn't a drive root.

    `shutil.disk_usage` reports usage for the WHOLE VOLUME a path lives on, regardless of what
    subtree that path names -- comparing it against an index scoped to anything narrower than
    the whole volume would silently manufacture a huge, meaningless "gap" (everything outside
    the scanned subtree, not the actual inaccessible-directory undercount this check exists to
    surface). Reconciliation is only a meaningful comparison for a scan that covered the WHOLE
    drive.
    """


def is_volume_root(path: Path) -> bool:
    """True if `path` is a bare drive root (`C:\\`, `D:\\`, ...) -- `resolved.parent ==
    resolved` is the standard `pathlib` idiom for "this is already the top of its own anchor",
    true for a drive root and for nothing else on Windows."""
    resolved = path.resolve()
    return resolved.parent == resolved and bool(resolved.drive)


@dataclass(frozen=True, slots=True)
class DiskReconciliationReport:
    """Compares one volume's indexed inventory (physical, hardlink-deduped bytes plus whatever
    inaccessible-directory bytes are actually knowable) against the OS's own real used-bytes
    figure for that volume (`shutil.disk_usage`) -- P0-5's answer to "does a user have any way to
    know their displayed numbers are undercounting, or by how much."

    Never fabricates precision it doesn't have: `inaccessible_unknown_count` is always reported
    alongside `delta_bytes`/`delta_pct` so a residual gap can be attributed honestly (some
    inaccessible paths have no size estimate at all -- their contribution to the true volume
    usage is real but genuinely unmeasured) rather than presented as an unexplained discrepancy.
    """

    volume: str
    indexed_bytes: int
    inaccessible_known_bytes: int
    inaccessible_path_count: int
    inaccessible_unknown_count: int
    reported_total_bytes: int
    volume_used_bytes: int
    delta_bytes: int
    delta_pct: float


def compute_disk_reconciliation(index: ScanIndex, volume_root: Path) -> DiskReconciliationReport:
    """Builds a `DiskReconciliationReport` for `volume_root`, which must be a bare drive root
    (see `NotAVolumeRootError`) that was scanned IN FULL -- this function has no way to tell a
    genuine inaccessible-directory undercount apart from "the index simply never covered this
    volume", and does not try to; a partial-subtree scan will show a large, honest-looking but
    scope-driven delta, not a meaningful reconciliation.

    `indexed_bytes` reuses the exact same `physical_size_bytes(index.full_inventory(...))` call
    `api.service.build_summary` already uses for `SummaryResponse.total_indexed_bytes` (a known,
    separately-tracked whole-index-materialization cost -- see the P1 finding in
    `docs/AUDIT-2026-08.md` -- not a new regression introduced here) so the two numbers stay
    directly comparable.
    """
    if not is_volume_root(volume_root):
        raise NotAVolumeRootError(
            f"{volume_root} is not a drive root -- reconciliation compares against "
            "shutil.disk_usage, which reports whole-VOLUME usage; pass a bare drive root "
            "(e.g. C:\\) that was scanned in full."
        )
    indexed_bytes = physical_size_bytes(index.full_inventory(under=volume_root))
    inaccessible = index.inaccessible_summary(under=volume_root)
    reported_total_bytes = indexed_bytes + inaccessible.known_bytes
    usage = shutil.disk_usage(volume_root)
    delta_bytes = usage.used - reported_total_bytes
    delta_pct = (delta_bytes / usage.used * 100) if usage.used > 0 else 0.0
    return DiskReconciliationReport(
        volume=str(volume_root),
        indexed_bytes=indexed_bytes,
        inaccessible_known_bytes=inaccessible.known_bytes,
        inaccessible_path_count=inaccessible.path_count,
        inaccessible_unknown_count=inaccessible.unknown_count,
        reported_total_bytes=reported_total_bytes,
        volume_used_bytes=usage.used,
        delta_bytes=delta_bytes,
        delta_pct=delta_pct,
    )
