from __future__ import annotations

import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from reclaim import growth
from reclaim.api.schemas import (
    GrowthAnomalyOut,
    GrowthExcludedOut,
    GrowthResponse,
    GrowthRowOut,
    GrowthVolumeOut,
)
from reclaim.api.state import AppState
from reclaim.app_paths import data_root
from reclaim.config import Config

# Read-only view over the growth store (ADR-0046, spec 1.4): nothing here writes, lists a
# directory or opens a file other than the store itself.

Window = Literal["1d", "7d"]
INSUFFICIENT = "insufficient data"
_WINDOW_S: dict[str, float] = {"1d": 86400.0, "7d": 7 * 86400.0}
MAX_ROWS = 50  # the biggest growers only: the response stays small and the screen readable
_DEFAULT_REVIEW_TIER = "both"
_OPAQUE_CATEGORY = "dev_artifacts"  # node_modules / .venv roots are the dev-artifacts detector's


def default_store_path() -> Path:
    """Same default as the `reclaim growth` CLI. Resolved at use (not import) so a test that
    chdirs sees its own directory, see `app_paths.data_root`."""
    return data_root() / "data" / "growth.sqlite3"


def _category_by_path(config: Config) -> dict[str, str]:
    """norm path -> category_group for the cache roots the snapshot sizes, taken from the same
    config lists the detectors use."""
    out: dict[str, str] = {}
    for p in config.categories.package_caches.paths:
        out[growth.norm(Path(p).expanduser())] = "package_caches"
    for p in config.categories.model_caches.paths:
        out[growth.norm(Path(p).expanduser())] = "model_caches"
    out[growth.norm(tempfile.gettempdir())] = "temp_and_browser_caches"
    return out


def review_category(kind: str, path: str, by_path: dict[str, str]) -> str | None:
    """The review-queue category that owns a growth row, or None when no category does (a
    top-level folder, a system file, Docker's disk image). Scratch sessions live under %TEMP%, so
    they map to the temp category; BELIEVED, not checked against what that detector lists."""
    if kind == "opaque":
        return _OPAQUE_CATEGORY
    if kind == "scratch":
        return "temp_and_browser_caches"
    if kind == "cache":
        return by_path.get(growth.norm(path))
    return None


def review_link(category: str | None) -> str | None:
    """The review queue's own query (what the Review tab's category filter requests). The
    dashboard has no URL route and no path filter, so a link is category-level only."""
    if category is None:
        return None
    return f"/api/candidates?tier={_DEFAULT_REVIEW_TIER}&category={category}"


def _same_volume(stored: str, wanted: str) -> bool:
    return stored.rstrip("\\/").lower() == wanted.rstrip("\\/").lower()


def empty_response(window: str, now: float, volume: str | None) -> GrowthResponse:
    volumes = []
    if volume:
        volumes.append(
            GrowthVolumeOut(
                volume=volume,
                change_bytes=0,
                attributed_bytes=0,
                not_attributed_bytes=0,
                status=INSUFFICIENT,
            )
        )
    return GrowthResponse(
        window=window,
        generated_at=datetime.fromtimestamp(now, UTC).isoformat(),
        status=INSUFFICIENT,
        volumes=volumes,
        rows=[],
        excluded_aggregate=GrowthExcludedOut(label="Excluded projects", bytes=0),
        anomalies=[],
    )


def build_growth_response(
    state: AppState, window: Window, volume: str | None, now: float | None = None
) -> GrowthResponse:
    """Window report plus anomalies from the growth store; an absent store is the empty,
    "insufficient data" response (never an error, never a created file)."""
    now = time.time() if now is None else now
    path = state.growth_db_path if state.growth_db_path is not None else default_store_path()
    if not path.is_file():
        return empty_response(window, now, volume)
    cfg = state.config.growth
    slack_s, end_age_s = cfg.max_start_slack_h * 3600.0, cfg.max_end_age_h * 3600.0
    with growth.GrowthStore(path) as store:
        series = store.load_series()
        partial = store.partial_latest_keys()
    report = growth.growth_report(series, _WINDOW_S[window], now, slack_s, end_age_s)

    volumes = [
        GrowthVolumeOut(
            volume=v.volume,
            change_bytes=v.delta,
            attributed_bytes=v.attributed,
            not_attributed_bytes=v.not_attributed,
            status=v.status,
        )
        for v in report.volumes
        if volume is None or _same_volume(v.volume, volume)
    ]
    if volume is not None and not volumes:
        return empty_response(window, now, volume)

    by_path = _category_by_path(state.config)
    scope = [r for r in report.rows if r.growth > 0]
    if volume is not None:
        root = growth.norm(volume.rstrip("\\/") + "\\")
        scope = [r for r in scope if growth.is_within(growth.norm(r.path), root)]
    rows = [
        GrowthRowOut(
            path=r.path,
            label=r.label,
            kind=r.kind,
            project=r.project,
            session=r.session,
            growth_bytes=r.growth,
            partial=r.path in partial,
            review_link=review_link(review_category(r.kind, r.path, by_path)),
        )
        for r in scope[:MAX_ROWS]
    ]

    excluded = 0
    for ks in series.values():
        if ks.root.kind == "excluded":
            res = growth.window_growth(ks.points, now, _WINDOW_S[window], slack_s, end_age_s)
            excluded = max(res[0], 0) if res else 0

    return GrowthResponse(
        window=window,
        generated_at=datetime.fromtimestamp(now, UTC).isoformat(),
        status="ok" if any(v.status == "ok" for v in volumes) else INSUFFICIENT,
        volumes=volumes,
        rows=rows,
        excluded_aggregate=GrowthExcludedOut(label="Excluded projects", bytes=excluded),
        anomalies=_anomalies(series, volume, now, state.config),
    )


def _anomalies(
    series: dict[str, growth.KeySeries], volume: str | None, now: float, config: Config
) -> list[GrowthAnomalyOut]:
    """Anomalies per fixed volume in the store, with the configured floors and freshness."""
    cfg = growth.AnomalyConfig(
        abs_floor=int(config.growth.abs_floor_gib * growth.GIB),
        share_floor=config.growth.share_floor_pct / 100.0,
        max_start_slack_h=config.growth.max_start_slack_h,
        max_end_age_h=config.growth.max_end_age_h,
    )
    out: list[GrowthAnomalyOut] = []
    for ks in series.values():
        if ks.root.kind != "volume" or (volume and not _same_volume(ks.root.path, volume)):
            continue
        total_ks = series.get(ks.root.path + growth.TOTAL_SUFFIX)
        total = total_ks.points[-1][1] if total_ks and total_ks.points else 0
        root = growth.norm(ks.root.path)
        keys = {
            p: k
            for p, k in series.items()
            if k.root.kind not in ("volume", "volume_total", "excluded")
            and growth.is_within(growth.norm(p), root)
        }
        for a in growth.detect_anomalies(keys, ks.root.path, ks.points, total, now, cfg):
            out.append(
                GrowthAnomalyOut(
                    scope=a.scope,
                    path=a.path,
                    growth_bytes=a.growth,
                    window_h=a.window_h,
                    z=a.z,
                    attributed=a.attributed,
                    explanation=a.explanation,
                )
            )
    return out
