from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import structlog

from reclaim import growth
from reclaim.config import Config, exclusion_patterns
from reclaim.index import ScanIndex
from reclaim.safety_env import refuse_real_side_effect_under_pytest

logger = structlog.get_logger(__name__)

# The scheduled side of disk growth attribution (spec §1.1 trigger, §1.5 toast; ADR-0046): what
# the 4-hourly `check-disk-space` task does with the growth store. Every entry point NEVER raises
# (a growth failure must not break the disk-space check) and, with `[growth] enabled = false`,
# does no growth IO at all: it does not even open the store.

SnapshotFn = Callable[..., growth.Snapshot]
EnvFactory = Callable[[Config], growth.Environment]


def scheduled_snapshot(
    config: Config,
    store_path: Path,
    *,
    index_path: Path | None = None,
    now: float | None = None,
    collect: SnapshotFn = growth.collect_snapshot,
    env_factory: EnvFactory = growth.environment_from_config,
) -> str:
    """Takes one snapshot when `[growth] enabled` and the last one is at least
    `min_interval_hours` old. Returns "disabled" | "not_due" | "taken" | "failed". The snapshot
    is bounded by `[growth] budget_seconds` / `budget_entries` (default 120 s / 3M entries).
    `collect`/`env_factory` are the seams tests replace with fakes."""
    if not config.growth.enabled:
        return "disabled"
    if collect is growth.collect_snapshot and env_factory is growth.environment_from_config:
        # A test must inject fakes: the real sampler would walk this machine's drives.
        refuse_real_side_effect_under_pytest("take a real disk-growth snapshot")
    current = time.time() if now is None else now
    index: ScanIndex | None = None
    try:
        with growth.GrowthStore(store_path) as store:
            if not growth.snapshot_due(store, config.growth.min_interval_hours, current):
                return "not_due"
            env = env_factory(config)
            if index_path is not None and index_path.is_file():
                index = ScanIndex(index_path)
            snap = collect(
                env,
                last_seen=store.last_seen(),
                index=index,
                budget_s=config.growth.budget_seconds,
                budget_entries=config.growth.budget_entries,
            )
            store.write(snap)
            return "taken"
    except Exception:  # a growth failure must never break the disk-space check
        logger.warning("growth.scheduled_snapshot_failed", exc_info=True)
        return "failed"
    finally:
        if index is not None:
            index.close()


# ---------------------------------------------------------------- toast text (spec §1.5)

RECENT_WINDOW_H = 72  # "the top contributors over the last 3 days"
_MIN_LINE_BYTES = growth.GIB  # a contributor under 1 GiB does not explain a full disk
_ALERT_STATE_NAME = "growth_alert_state.json"  # the anomaly toast's own debounce, not the 80%'s
GROWTH_TOAST_TITLE = "Your disk is growing fast"


def system_volume() -> str:
    """The volume the 80% check measures (`notifications._default_drive_anchor`)."""
    return os.environ.get("SYSTEMDRIVE", "C:") + "\\"


def _same(a: str, b: str) -> bool:
    return growth.norm(a) == growth.norm(b)


def _load_visible_series(
    config: Config, store_path: Path, volume: str
) -> dict[str, growth.KeySeries] | None:
    """The stored series of `volume` with every excluded path removed, or None when there is no
    store (never created here). Excluded projects are dropped before any text is built, so they
    cannot be named even if an index-sourced row reached the store; only the aggregate label
    "Excluded projects" ever appears, and the toasts do not use it."""
    if not store_path.is_file():
        return None
    with growth.GrowthStore(store_path) as store:
        series = store.load_series()
    root = growth.norm(volume.rstrip("\\/") + "\\")
    out: dict[str, growth.KeySeries] = {}
    for path, ks in growth.hide_excluded(series, exclusion_patterns(config)).items():
        if ks.root.kind == "excluded":
            continue
        if ks.root.kind in ("volume", "volume_total"):
            if _same(path.removesuffix(growth.TOTAL_SUFFIX), volume):
                out[path] = ks
        elif growth.is_within(growth.norm(path), root):
            out[path] = ks
    return out


def _anomaly_config(config: Config) -> growth.AnomalyConfig:
    g = config.growth
    return growth.AnomalyConfig(
        abs_floor=int(g.abs_floor_gib * growth.GIB),
        share_floor=g.share_floor_pct / 100.0,
        max_start_slack_h=g.max_start_slack_h,
        max_end_age_h=g.max_end_age_h,
    )


def recent_growth_line(config: Config, store_path: Path, now: float, volume: str) -> str | None:
    """`Last 3 days: +18.0 GiB from <label>, ...` for the 80% toast, or None when the store has
    no fresh enough window (same freshness limits as the report) or nothing grew >= 1 GiB.
    Never raises."""
    if not config.growth.enabled:
        return None
    try:
        series = _load_visible_series(config, store_path, volume)
        if series is None:
            return None
        g = config.growth
        report = growth.growth_report(
            series,
            RECENT_WINDOW_H * 3600.0,
            now,
            g.max_start_slack_h * 3600.0,
            g.max_end_age_h * 3600.0,
        )
        top = [c for c in growth.contributors(report.rows, 3) if c.growth >= _MIN_LINE_BYTES]
        if not top:
            return None
    except Exception:
        logger.warning("growth.recent_line_failed", exc_info=True)
        return None
    parts = ", ".join(f"+{growth.fmt_gib(c.growth)} from {c.label}" for c in top)
    return f"Last 3 days: {parts}"


def growth_anomaly_text(config: Config, store_path: Path, now: float, volume: str) -> str | None:
    """The volume-level anomaly sentence (`C: grew 20.0 GiB in 3 days — 18.0 GiB from X`), or
    None. Never raises."""
    if not config.growth.enabled:
        return None
    try:
        series = _load_visible_series(config, store_path, volume)
        if series is None:
            return None
        used = next((ks for ks in series.values() if ks.root.kind == "volume"), None)
        total = next((ks for ks in series.values() if ks.root.kind == "volume_total"), None)
        if used is None or total is None or not total.points:
            return None
        keys = {p: ks for p, ks in series.items() if ks.root.kind not in ("volume", "volume_total")}
        found = growth.detect_anomalies(
            keys, used.root.path, used.points, total.points[-1][1], now, _anomaly_config(config)
        )
        volume_level = [a for a in found if a.scope == "volume"]
        if not volume_level:
            return None
        return max(volume_level, key=lambda a: a.growth).explanation
    except Exception:
        logger.warning("growth.anomaly_check_failed", exc_info=True)
        return None


def maybe_send_growth_toast(
    config: Config,
    store_path: Path,
    state_path: Path,
    *,
    now: float | None = None,
    volume: str | None = None,
    send: Callable[[str], bool] | None = None,
) -> str:
    """The growth-anomaly toast (spec §1.5), with its own debounce file beside `state_path`.
    Returns "disabled" | "snoozed" | "debounced" | "none" | "sent" | "not_delivered" | "failed".
    Gated by `[growth] enabled` AND `[notifications] enabled` (a toast the user never opted into
    would be a surprise); the 80% toast's snooze is honoured, its debounce is not shared. As for
    the 80% toast, the debounce is only recorded once Windows accepted the toast. Never raises."""
    from reclaim import notifications

    if not (config.growth.enabled and config.notifications.enabled):
        return "disabled"
    try:
        current = time.time() if now is None else now
        if notifications.is_snoozed(notifications.load_state(state_path), now=current):
            return "snoozed"
        alert_state_path = state_path.with_name(_ALERT_STATE_NAME)
        last = notifications.load_state(alert_state_path).last_notified_at
        renotify_s = config.notifications.renotify_after_hours * 3600.0
        if last is not None and current - last < renotify_s:
            return "debounced"
        text = growth_anomaly_text(config, store_path, current, volume or system_volume())
        if text is None:
            return "none"
        sender = send if send is not None else notifications.send_growth_toast
        if not sender(text):
            return "not_delivered"
        notifications.record_notified(alert_state_path, now=current)
    except Exception:
        logger.warning("growth.toast_failed", exc_info=True)
        return "failed"
    return "sent"
