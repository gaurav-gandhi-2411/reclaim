from __future__ import annotations

# Replays the committed real-data fixtures (evals/fixtures/growth) through reclaim.growth's pure
# analysis (detect_anomalies, growth_report: nothing re-implemented) and through a plain-threshold
# baseline, and scores both against the doc-cited events. Reads committed files only.
# `python evals/growth/growth_eval.py` rewrites reports/growth_eval.{md,json}.
import csv
import hashlib
import json
import random
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reclaim import growth as g

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "fixtures" / "growth"
SOURCES = ("attrib", "census", "watchdog", "jobs")
WINDOWS_H = g.AnomalyConfig().windows_h  # the baseline uses the detector's own windows
EXCLUDED_FIXTURE_KEY = "Excluded projects"
STEP_S = 900.0  # volume-only evaluation times are at least this far apart
SEGMENT_GAP_S = 3600.0  # a longer hole between samples ends a continuous stretch
EPISODE_GAP_S = 6 * 3600.0  # alerts closer than this on one key are one episode
MATCH_SLACK_S = 6 * 3600.0  # an alert up to this long after an event window still detects it
COVER_TOL_S = 3600.0  # evaluable: a sample within this of both ends of the window
MIN_CONTINUOUS_H = 4 * 7 * 24  # below this the per-week figures are labelled not meaningful
GIB = g.GIB

Point = tuple[float, int]
Res = dict[str, Any]


def epoch(ts: str) -> float:
    """Local wall-clock ISO text -> seconds, read as UTC so DST can never shift a window."""
    return datetime.fromisoformat(ts).replace(tzinfo=UTC).timestamp()


def sha256_text(path: Path) -> str:
    """SHA-256 with line endings normalised, so a CRLF checkout hashes like the LF original."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_rows(source: str) -> list[tuple[float, str, int]]:
    with (FIXTURES / f"{source}.csv").open(newline="") as fh:
        return [(epoch(r["ts"]), r["key"], int(r["bytes"])) for r in csv.DictReader(fh)]


@dataclass
class Dataset:
    series: dict[str, list[Point]]  # every key, all sources merged, time-ordered
    totals: dict[str, int]
    times: dict[str, list[float]]  # sorted distinct sample times per source


def build_dataset() -> Dataset:
    meta = json.loads((FIXTURES / "meta.json").read_text())
    merged: dict[str, dict[float, int]] = {}
    times: dict[str, list[float]] = {}
    for source in SOURCES:
        rows = load_rows(source)
        times[source] = sorted({t for t, _, _ in rows})
        for t, key, size in rows:
            merged.setdefault(key, {})[t] = size
    series = {k: sorted(v.items()) for k, v in merged.items()}
    return Dataset(series, meta["volume_totals_bytes"], times)


def volume_of(key: str) -> str:
    return key[0].upper() if len(key) > 2 and key[1:3] == ":\\" else "C"  # samplers watched C:


def key_series(ds: Dataset) -> dict[str, g.KeySeries]:
    out: dict[str, g.KeySeries] = {}
    for key, pts in ds.series.items():
        if key.startswith("volume:"):
            continue
        kind = "excluded" if key == EXCLUDED_FIXTURE_KEY else "folder"
        out[key] = g.KeySeries(g.Root(key, kind, key), pts)
    return out


def eval_times(ds: Dataset) -> list[float]:
    """Every instant a folder size was sampled, plus volume samples at most every STEP_S."""
    chosen = {t for k, p in ds.series.items() if not k.startswith("volume:") for t, _ in p}
    last = float("-inf")
    for t in sorted({t for k, p in ds.series.items() if k.startswith("volume:") for t, _ in p}):
        if t - last >= STEP_S:
            chosen.add(t)
            last = t
    return sorted(chosen)


# ---------------------------------------------------------------- replay


@dataclass(frozen=True)
class Alert:
    t: float
    scope: str  # key | volume
    volume: str
    key: str
    growth: int
    window_h: int
    named: str | None  # the folder the alert blames
    start_t: float  # the sample the growth is measured from
    end_t: float  # the newest sample of the series that alerted

    @property
    def start_age_h(self) -> float:
        """Age of the start sample. Above window_h the snapshots in between were never taken."""
        return (self.t - self.start_t) / 3600

    @property
    def end_age_h(self) -> float:
        """Age of the newest sample: above ~1 h the series was not part of a fresh snapshot."""
        return (self.t - self.end_t) / 3600


def _alert(
    t: float,
    scope: str,
    vol: str,
    key: str,
    growth: int,
    window_h: int,
    named: str | None,
    points: list[Point],
) -> Alert:
    res = g.window_growth(points, t, window_h * 3600.0)
    start_t = res[1][0] if res else float("nan")
    end = g.value_at_or_before(points, t)
    return Alert(t, scope, vol, key, growth, window_h, named, start_t, end[0] if end else start_t)


def replay(ds: Dataset, method: str, cfg: g.AnomalyConfig | None = None) -> list[Alert]:
    keys = key_series(ds)
    used = {v: ds.series.get(f"volume:{v}:used", []) for v in ("C", "D")}
    out: list[Alert] = []
    for now in eval_times(ds):
        if method == "baseline":
            for window_h in WINDOWS_H:
                # no freshness limits: the baseline is the unchanged plain threshold
                rows = g.growth_report(keys, window_h * 3600.0, now, None, None).rows
                if rows and rows[0].growth >= 5 * GIB:  # the largest grower, no drill-down
                    r = rows[0]
                    out.append(
                        _alert(
                            now,
                            "key",
                            volume_of(r.path),
                            r.path,
                            r.growth,
                            window_h,
                            r.path,
                            keys[r.path].points,
                        )
                    )
            continue
        for vol in ("C", "D"):
            found = g.detect_anomalies(
                keys if vol == "C" else {}, f"{vol}:\\", used[vol], ds.totals[vol], now, cfg
            )
            for a in found:
                pts = used[vol] if a.scope == "volume" else keys[a.path].points
                out.append(
                    _alert(now, a.scope, vol, a.path, a.growth, a.window_h, a.attributed, pts)
                )
    return out


def fresh_and_three_day(a: Alert) -> bool:
    """Spec 1.5 read literally: growth "within <= 3 days" measured on a fresh snapshot."""
    return a.start_age_h <= 72 and a.end_age_h <= 1


@dataclass
class Episode:
    alerts: list[Alert]

    @property
    def first(self) -> Alert:
        return self.alerts[0]


def episodes(alerts: list[Alert]) -> list[Episode]:
    """Alerts on one (scope, volume, key) merge while they are < 6 h apart or repeat the same
    growth figure (a stale key re-reports the same two samples until its window moves on)."""
    out: list[Episode] = []
    open_: dict[tuple[str, str, str], Episode] = {}
    for a in sorted(alerts, key=lambda x: x.t):
        k = (a.scope, a.volume, a.key)
        cur = open_.get(k)
        last = cur.alerts[-1] if cur else None
        if cur and last and (a.t - last.t <= EPISODE_GAP_S or a.growth == last.growth):
            cur.alerts.append(a)
        else:
            open_[k] = Episode([a])
            out.append(open_[k])
    return out


# ---------------------------------------------------------------- scoring


def cite_label(cite: Res) -> str:
    """Report label for a citation: the doc plus the line it sat on when the truth was written.

    The line is provenance only (RESUME.md keeps shifting); the test anchors on the quote.
    """
    return f"{cite['doc']}:{cite['line_at_8fc9bc6']}"


def load_events() -> Res:
    data: Res = json.loads((FIXTURES / "events.json").read_text())
    return data


def attribution(named: str | None, fixture_key: str) -> str:
    if named is None:
        return "none"
    n, f = g.norm(named), g.norm(fixture_key)
    if g.is_within(n, f):
        return "correct"
    return "coarse" if g.is_within(f, n) else "wrong"


def covers(a: Alert, ev: Res) -> bool:
    """An alert detects an event when, on the event's volume, it fires after the event began (at
    most 6 h after it ended) from a start sample no later than the event's end, so its growth
    interval overlaps the event, and its growth is within a factor 2 of the documented size (a
    16 GiB alert is not the detection of a 185 GiB copy)."""
    start, end = epoch(str(ev["start"])), epoch(str(ev["end"]))
    size = float(str(ev["size_gib"]))
    return (
        a.volume == ev["volume"]
        and a.start_t <= end
        and start < a.t <= end + MATCH_SLACK_S
        and size / 2 <= a.growth / GIB <= size * 2
    )


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S")


def describe(a: Alert) -> Res:
    return {
        "t": stamp(a.t), "scope": a.scope, "volume": a.volume, "key": a.key,
        "growth_gib": round(a.growth / GIB, 1), "window_h": a.window_h,
        "start_age_h": round(a.start_age_h, 1), "end_age_h": round(a.end_age_h, 1),
    }  # fmt: skip


def score(events: list[Res], eps: list[Episode]) -> Res:
    # Precision counts an episode true when it covers ANY documented growth event of >= 5 GiB that
    # is not called unproven, evaluable or not; recall and attribution use evaluable events only.
    reference = [
        e
        for e in events
        if e["kind"] == "growth"
        and e["attribution_status"] != "unproven"
        and float(str(e["size_gib"])) >= 5
    ]
    rows: list[Res] = []
    for ev in (e for e in events if e["evaluable"]):
        hits = sorted((a for e in eps for a in e.alerts if covers(a, ev)), key=lambda a: a.t)
        first = hits[0] if hits else None
        fk = ev["fixture_key"]
        rows.append(
            {
                "id": ev["id"],
                "detected": first is not None,
                "named": first.named if first else None,
                "alert": describe(first) if first else None,
                "attribution": attribution(first.named if first else None, str(fk))
                if fk
                else "n/a",
            }
        )
    false = [e for e in eps if not any(covers(a, ev) for a in e.alerts for ev in reference)]
    detected = sum(bool(r["detected"]) for r in rows)
    attributable = [r for r in rows if r["attribution"] != "n/a" and r["detected"]]
    return {
        "events": rows,
        "episodes": len(eps),
        "false_alarm_episodes": len(false),
        "precision": round((len(eps) - len(false)) / len(eps), 3) if eps else None,
        "recall": f"{detected}/{len(rows)}",
        "top1": f"{sum(r['attribution'] == 'correct' for r in attributable)}/{len(attributable)}",
        "false_alarm_list": [{**describe(e.first), "alerts": len(e.alerts)} for e in false],
    }


def coverage(ds: Dataset) -> Res:
    out: Res = {}
    all_times: set[float] = set()
    for source, ts in ds.times.items():
        all_times.update(ts)
        out[source] = _stretches(ts)
    out["all_sources"] = _stretches(sorted(all_times))
    return out


def _stretches(ts: list[float]) -> Res:
    segs: list[list[float]] = []
    for t in ts:
        if segs and t - segs[-1][1] <= SEGMENT_GAP_S:
            segs[-1][1] = t
        else:
            segs.append([t, t])
    hours = [(b - a) / 3600 for a, b in segs]
    return {
        "hours": round(sum(hours), 2),
        "stretches": len(segs),
        "samples": len(ts),
        "longest_h": round(max(hours), 2) if hours else 0.0,
        "span_days": round((ts[-1] - ts[0]) / 86400, 2) if ts else 0.0,
    }


def c_used_drift(ds: Dataset) -> list[Res]:
    """Net change of C: used space over each continuous stretch of >= 2 h (real data)."""
    segs: list[list[Point]] = []
    for p in ds.series["volume:C:used"]:
        if segs and p[0] - segs[-1][-1][0] <= SEGMENT_GAP_S:
            segs[-1].append(p)
        else:
            segs.append([p])
    out: list[Res] = []
    for s in segs:
        hours = (s[-1][0] - s[0][0]) / 3600
        if hours >= 2:
            delta = (s[-1][1] - s[0][1]) / GIB
            out.append({"start": stamp(s[0][0]), "hours": round(hours, 1),
                        "net_change_gib": round(delta, 1),
                        "gib_per_day": round(delta / hours * 24, 1)})  # fmt: skip
    return out


def diagnose(ds: Dataset, events: list[Res]) -> Res:
    """Why a replay can or cannot see an evaluable event: what the volume rule's windows and the
    key rule's 14-day history look like at the event's end."""
    keys = key_series(ds)
    cfg = g.AnomalyConfig()
    out: Res = {}
    for ev in (e for e in events if e["evaluable"]):
        end = epoch(ev["end"])
        used = ds.series.get(f"volume:{ev['volume']}:used", [])
        d: Res = {}
        for h in cfg.windows_h:
            res = g.window_growth(used, end, h * 3600.0)
            d[f"volume_growth_{h}h_gib"] = round(res[0] / GIB, 1) if res else None
        fk = ev["fixture_key"]
        if fk and fk in keys:
            pts = keys[fk].points
            res = g.window_growth(pts, end, 72 * 3600.0)
            z = g.key_z(pts, res[1][0], res[0] / 72, cfg) if res else None
            d["key_growth_72h_gib"] = round(res[0] / GIB, 1) if res else None
            d["key_z"] = None if z is None else round(z, 1)
            d["key_z_note"] = (
                "None = fewer than 6 thinned intervals in the 14 days before the window"
            )
        out[ev["id"]] = d
    return out


# ---------------------------------------------------------------- steady-growth control


# Alerts at snapshots before this index use the no-history fallback (no volume baseline, floor
# alone), which keeps a fresh install from going quiet: the baseline needs >= 6 daily intervals
# (min_ref_intervals) before the window start and the 72 h window starts 3 days back, so the
# fallback can alert until about day 9 (snapshot 54); 60 = day 10.
WARMUP_SNAPSHOTS = 60


def steady_growth_control(
    rate_gib_day: float, days: int = 14, every_h: int = 4, surge_gib: int = 0
) -> Res:
    """Synthetic, deterministic: used space rises at a constant rate, snapshots every 4 h. With
    surge_gib = 0 there is no event and no noise, so every volume alert is a false alarm by
    construction. With surge_gib > 0 that much extra growth is spread over one day (6 snapshots)
    ending 1 day before the series ends: the volume rule must still fire on it."""
    base, total = 100 * GIB, 500 * GIB
    # (14 days = 84 snapshots: the surge sits at snapshots 72-77, after the warm-up)
    n = days * 24 // every_h
    per_day = 24 // every_h
    surge_from = n - 2 * per_day  # first snapshot of the surge day
    pts = []
    for i in range(n):
        share = min(max(i - surge_from, 0), per_day) / per_day
        value = base + rate_gib_day * GIB * i * every_h / 24 + surge_gib * GIB * share
        pts.append((i * every_h * 3600.0, int(value)))
    fired = [
        i
        for i, (t, _) in enumerate(pts)
        if any(a.scope == "volume" for a in g.detect_anomalies({}, "C:\\", pts, total, t))
    ]
    return {
        "rate_gib_per_day": rate_gib_day,
        "surge_gib": surge_gib,
        "snapshots": len(pts),
        "alerting_snapshots": len(fired),
        "alerting_after_warmup": sum(i >= WARMUP_SNAPSHOTS for i in fired),
        "alerting_from_surge_start": sum(i >= surge_from for i in fired),
        "first_alert_day": round(pts[fired[0]][0] / 86400, 2) if fired else None,
    }


def _volume_alert_idx(pts: list[Point], total: int) -> list[int]:
    """Indices of the snapshots at which the detector raises a volume alert (prefix replay)."""
    return [
        i
        for i, (t, _) in enumerate(pts)
        if any(a.scope == "volume" for a in g.detect_anomalies({}, "C:\\", pts[: i + 1], total, t))
    ]


def bursty_steady_control(step_gib: float, days: int = 30, every_days: int = 1) -> Res:
    """Synthetic: steady growth that arrives as one step of step_gib every every_days days
    (snapshot 0 of that day), flat in between. No event: every alert after the warm-up is a false
    alarm by construction. A dispersion-scaled gate cannot pass this (per-interval MAD is 0).
    every_days = 1 is absorbed by the median daily-step baseline; a cadence of 7 days is the
    disclosed limit: most daily steps are 0, so the baseline is 0 and every burst alerts."""
    value, pts = 100 * GIB, []
    for i in range(days * 6):
        if i % (6 * every_days) == 0:
            value += int(step_gib * GIB)
        pts.append((i * 4 * 3600.0, value))
    fired = _volume_alert_idx(pts, 500 * GIB)
    return {
        "rate_gib_per_day": round(step_gib / every_days, 3),
        "step_gib": step_gib,
        "every_days": every_days,
        "snapshots": len(pts),
        "alerting_snapshots": len(fired),
        "alerting_after_warmup": sum(i >= WARMUP_SNAPSHOTS for i in fired),
    }


def noisy_surge_control(sd_gib: float, surge_gib: int = 40, days: int = 20) -> Res:
    """Synthetic, seed 42: random-walk volume (sd_gib per 4 h snapshot), optionally plus surge_gib
    spread over day 14 (6 snapshots). Reports the first alert after the surge starts, with and
    without the surge, so a surge alert can be told from noise. Noise on its own alerts too (the
    5 GiB floor is far below the walk's 24 h spread when sd is large); the rule must not hide the
    surge, and must not alert more often than the floor alone."""
    start = 14 * 6
    out: Res = {"sd_gib_per_4h": sd_gib, "surge_gib": surge_gib}
    for label, surge in (("with_surge", surge_gib), ("noise_only", 0)):
        rng = random.Random(42)  # noqa: S311 -- deterministic synthetic data, not security
        value, pts = 400 * GIB, []
        for i in range(days * 6):
            value += rng.gauss(0, sd_gib * GIB)
            if surge and start <= i < start + 6:
                value += surge * GIB / 6
            pts.append((i * 4 * 3600.0, int(value)))
        fired = [i for i in _volume_alert_idx(pts, 500 * GIB) if i >= start]
        out[f"{label}_alerts"] = len(fired)
        out[f"{label}_first_alert_h"] = (fired[0] - start) * 4 if fired else None
    return out


def sustained_leak_control(gib_per_day: float = 10.0, quiet_days: int = 14, days: int = 34) -> Res:
    """Synthetic, seed 42: flat (tiny noise) for quiet_days, then a constant leak. Reports how
    long the detector alerts before the 14-day history absorbs the leak as 'usual'. Not a
    pass/fail: the 80%-full check (R5) is the backstop and does not fire below its threshold."""
    rng = random.Random(42)  # noqa: S311 -- deterministic synthetic data, not security
    start = quiet_days * 6
    value, pts = 400 * GIB, []
    for i in range(days * 6):
        value += rng.gauss(0, 0.05 * GIB) + (gib_per_day * GIB / 6 if i >= start else 0)
        pts.append((i * 4 * 3600.0, int(value)))
    fired = [i for i in _volume_alert_idx(pts, 2000 * GIB) if i >= start]
    return {
        "gib_per_day": gib_per_day,
        "alerting_snapshots": len(fired),
        "first_alert_h_after_start": (fired[0] - start) * 4 if fired else None,
        "last_alert_h_after_start": (fired[-1] - start) * 4 if fired else None,
        "days_alerting": round((fired[-1] - fired[0] + 1) / 6, 1) if fired else 0,
    }


def floor_only_volume_alerts(ds: Dataset) -> list[Alert]:
    """The pre-fix volume decision, re-implemented here so the key rule is never touched: the
    shortest window whose growth reaches abs_floor alerts, with no baseline. Mirrors
    detect_anomalies' volume branch before the baseline was added."""
    cfg = g.AnomalyConfig()
    slack_s, end_age_s = cfg.max_start_slack_h * 3600.0, cfg.max_end_age_h * 3600.0
    keys = key_series(ds)
    out: list[Alert] = []
    for now in eval_times(ds):
        for vol in ("C", "D"):
            used = ds.series.get(f"volume:{vol}:used", [])
            for window_h in sorted(cfg.windows_h):
                window_s = window_h * 3600.0
                res = g.window_growth(used, now, window_s, slack_s, end_age_s)
                if res is None or res[0] < cfg.abs_floor:
                    continue
                folders = keys if vol == "C" else {}
                rows = g.growth_report(folders, window_s, now, slack_s, end_age_s).rows
                top = g.contributors(rows, 2)
                named = top[0].path if top else None
                out.append(_alert(now, "volume", vol, f"{vol}:\\", res[0], window_h, named, used))
                break  # the shortest qualifying window is the one reported
    return out


def dropped_alerts(ds: Dataset, events: list[Res], now_alerts: list[Alert]) -> Res:
    """Alert episodes of the pre-fix rule that the current detector no longer raises. The
    pre-fix alerts are the key alerts of the current rule (unchanged by the fix) plus the
    floor-only volume alerts of floor_only_volume_alerts, cross-checked against the product code:
    history_s = 0 leaves the baseline undefined, so detect_anomalies then equals the old volume
    decision. That replay is used for the check only, because it also switches the key rule off.
    A dropped alert is listed, not counted as a win: no documented event is not a verified
    non-event."""
    old_volume = floor_only_volume_alerts(ds)
    check = replay(ds, "detector", replace(g.AnomalyConfig(), history_s=0.0))

    def ident(a: Alert) -> tuple[float, str, int, int]:
        return (a.t, a.volume, a.growth, a.window_h)

    if sorted(map(ident, old_volume)) != sorted(ident(a) for a in check if a.scope == "volume"):
        raise RuntimeError("floor-only volume replay disagrees with detect_anomalies(history_s=0)")
    old = old_volume + [a for a in now_alerts if a.scope == "key"]
    kept = {(a.t, a.scope, a.volume, a.key) for a in now_alerts}
    reference = [
        e
        for e in events
        if e["kind"] == "growth"
        and e["attribution_status"] != "unproven"
        and float(str(e["size_gib"])) >= 5
    ]
    dropped: list[Res] = []
    for ep in episodes(old):
        if any((a.t, a.scope, a.volume, a.key) in kept for a in ep.alerts):
            continue
        hit = [str(ev["id"]) for ev in reference if any(covers(a, ev) for a in ep.alerts)]
        dropped.append(
            {
                **describe(ep.first),
                "alerts": len(ep.alerts),
                "max_growth_gib": round(max(a.growth for a in ep.alerts) / GIB, 1),
                "covers_documented_event": hit,
            }
        )
    dropped.sort(key=lambda d: -float(d["max_growth_gib"]))
    legacy = score(events, episodes(old))
    cfg = g.AnomalyConfig()
    with_baseline = not_raised = unexplained = 0
    for a in old_volume:
        used = ds.series.get(f"volume:{a.volume}:used", [])
        base = g.volume_baseline_rate_h(used, a.start_t, cfg)
        with_baseline += base is not None
        if (a.t, a.scope, a.volume, a.key) in kept:
            continue
        not_raised += 1
        # explained: a baseline existed and the growth beyond it was under the floor
        if base is None or a.growth - max(base, 0.0) * a.window_h >= cfg.abs_floor:
            unexplained += 1
    return {
        "prefix_rule_alerts": len(old),
        "prefix_rule_alerts_with_a_baseline": with_baseline,
        "prefix_rule_volume_alerts_not_raised": not_raised,
        "prefix_rule_volume_alerts_not_raised_unexplained": unexplained,
        "prefix_rule_episodes": legacy["episodes"],
        "prefix_rule_false_alarm_episodes": legacy["false_alarm_episodes"],
        "prefix_rule_recall": legacy["recall"],
        "dropped_episodes": dropped,
    }


# ---------------------------------------------------------------- report

VARIANTS = ("detector", "detector_literal", "baseline", "baseline_literal")
FALSE_ALARM_CAVEAT = (
    'A "false alarm" here means no documented event covers the episode, not a verified '
    "non-event: the docs do not list every real write."
)
# Events added to events.json AFTER the first replay had shown a D: alert (see the honesty
# section). A detection of one of these is not independent of the replay that motivated it.
POST_HOC_EVENTS = ("pagefile-to-d-2026-10-09", "hf-copy-to-d-2026-10-09")


def post_hoc_caveat(m: Res) -> list[str]:
    """Summary paragraph flagging detections that rest on post-hoc events (empty when none do)."""
    hits = {
        v: [r["id"] for r in m[v]["events"] if r["detected"] and r["id"] in POST_HOC_EVENTS]
        for v in VARIANTS
    }
    if not any(hits.values()):
        return []

    def without(v: str) -> str:
        rows = [r for r in m[v]["events"] if r["id"] not in POST_HOC_EVENTS]
        return f"{sum(bool(r['detected']) for r in rows)}/{len(rows)}"

    named = ", ".join(f"`{i}`" for i in sorted({i for ids in hits.values() for i in ids}))
    return [
        f"**Not an independent hit.** Event {named} was added to the ground truth after the first "
        "replay had shown a D: alert, and the alert-to-event match uses a factor-2 size band that "
        "was added after a 16 GiB alert was seen matching a 185 GiB copy (both described under "
        '"Two events were added after the first replay" below). Counting only the events that '
        f"existed before that replay, the detector finds {without('detector')} as replayed and "
        f"{without('detector_literal')} read literally, the baseline {without('baseline')} and "
        f"{without('baseline_literal')}. The detector's one detection is therefore not evidence "
        "that it works.",
        "",
    ]


def evaluate() -> Res:
    ds = build_dataset()
    events: list[Res] = load_events()["events"]
    cov = coverage(ds)
    longest, total_h = cov["all_sources"]["longest_h"], cov["all_sources"]["hours"]
    methods: Res = {}
    for method in ("detector", "baseline"):
        raw = replay(ds, method)
        # "literal": spec 1.5 says growth "within <= 3 days" on a snapshot taken now, so an alert
        # whose start sample is older than 72 h, or whose newest sample is older than 1 h, is out.
        for name, alerts in (
            (method, raw),
            (f"{method}_literal", [a for a in raw if fresh_and_three_day(a)]),
        ):
            res = score(events, episodes(alerts))
            res["alerts_raw"] = len(alerts)
            res["false_alarms_per_week"] = {
                "value": round(res["false_alarm_episodes"] / (total_h / 168), 2),
                "meaningful": longest >= MIN_CONTINUOUS_H,
            }
            methods[name] = res
    return {
        "fixtures_sha256": {p.name: sha256_text(p) for p in sorted(FIXTURES.iterdir())},
        "coverage": cov,
        "continuous_hours_needed": MIN_CONTINUOUS_H,
        "events_total": len(events),
        "events_evaluable": sum(bool(e["evaluable"]) for e in events),
        "methods": methods,
        "event_diagnostics": diagnose(ds, events),
        "c_used_drift": c_used_drift(ds),
        "steady_growth_control": [steady_growth_control(r) for r in (1.0, 1.5, 2.0, 3.0)],
        "surge_on_steady_control": [steady_growth_control(2.0, surge_gib=20)],
        "bursty_steady_control": [bursty_steady_control(r) for r in (2.0, 3.0)],
        "bursty_weekly_control": [bursty_steady_control(14.0, days=60, every_days=7)],
        "noisy_surge_control": [noisy_surge_control(3.0)],
        "sustained_leak_control": sustained_leak_control(),
        "false_alarm_definition": FALSE_ALARM_CAVEAT,
        "dropped_alerts": dropped_alerts(ds, events, replay(ds, "detector")),
    }


HONESTY = """\
## Honesty section

- **Not a 4-week eval.** The data are {hours} h of samples in {stretches} stretches over
  {span} days; the longest continuous stretch is {longest} h. Four continuous weeks would be
  {needed} h. Every per-week figure above is therefore **not meaningful** (it divides a handful of
  episodes by {hours} h of data).
- **Tiny n.** {n_eval} evaluable events (of {n_total} documented), of which 2 are the same census
  pair seen twice. Recall, top-1 and precision are counts to read, not rates to quote. The spec's
  success metric (recall >= 0.8, <= 1 false alarm per week) is neither met nor refuted by this data.
- **Precision is a lower bound, false alarms an upper bound.** An episode counts as true only if it
  covers a documented growth event of >= 5 GiB; the docs do not list every real write, so some
  "false alarms" may be real, undocumented growth (D: in particular).
- **The key rule never ran.** It needs >= 6 thinned (30 min) intervals of history before the
  window start and a 24/72 h window. No folder series in the data spans more than about 2 h
  except the two census snapshots (2 points), so every detector detection comes from the
  volume rule;
  key-level z-scores are all `None` (see `event_diagnostics` in the JSON).
- **Gaps make the as-is replay look busy.** The sources are fragments, so a "24 h" window often
  starts from a sample days old (`start_age_h`), and census-time alerts use a C: sample 52 h old.
  Real snapshots every 4 h would not have those holes. The *literal* columns drop alerts whose
  start sample is older than 72 h or whose newest sample is older than 1 h; they are the closer
  reading of the spec, not a tuned variant. The finding that `detect_anomalies` reported growth
  accumulated over a whole gap as growth "in 3 days" is FIXED (see the change log): a window's
  start sample may now be at most window + 6 h old and the newest sample at most 1 h old. The
  detector's "as replayed" column therefore now loses the two census-time detections of the
  9 -> 12 Oct growth: their volume alert rested on a C: sample 52 h old, which is a hole in the
  fragments, not a missed snapshot a real 4-hourly run would have. The loss comes from the data
  being fragmentary and is not evidence about the detector on real snapshots.
- **Two events were added after the first replay** (`pagefile-to-d-2026-10-09`,
  `hf-copy-to-d-2026-10-09`, both D:, both documented in RESUME.md); the first replay showed D:
  alerts and the original event list had only C: events. The alert-to-event matching also gained
  a factor-2 size band after a 16 GiB alert was seen matching a 185 GiB copy. No threshold of the
  detector or baseline was changed; spec defaults throughout.
- **Same-instant events share one alert.** The Temp and ml-projects growths (9 -> 12 Oct) are two
  documented contributors to one volume alert, whose `attributed` field names only the top one,
  so top-1 cannot exceed 1 of 2 there. The census is too shallow to hold the gg-portfolio scratch
  folder itself, so naming `Temp` is scored correct and naming `AppData` coarse.
- **Fixtures are thinned, not 4-hourly snapshots:** 60 s (watchdog, attrib) and 300 s (jobs)
  samples, folder sizes every 1.7-2.5 min in three 30-40 min windows, two census snapshots. The
  `uv` growth inside those windows (0.58 -> 20.8 GiB and similar) is a budget-truncated walk of
  the old sampler and is excluded from the ground truth; the baseline does alert on it.

### What data is needed

At least 4 weeks (672 h) of continuous 4-hourly snapshots from the shipped feature on this machine
(168 snapshots), with growth events of the documented classes inside them and attributed
afterwards by a person: overnight scratch growth by another session, an unexplained build-time C:
drop, a model or cache download, a pagefile move, steady drift. With about 10 events the recall
interval would still be wide; the spec's 0.8 target needs a larger event set
to be tested honestly.
"""


# Numbers of the report before the window-freshness fix, as published at commit 6002874 (the
# detector code at 4435381 is identical). Kept as constants: the old detector no longer exists.
BEFORE_SHA = "60028742245f969a6aebf0d62d666687711f7e80"
BEFORE_CODE_SHA = "4435381165df666cb67b5c5d5bea8b56e31e13b9"
BEFORE = {  # variant -> (recall, alert episodes, false-alarm episodes, precision)
    "detector": ("3/5", 16, 14, 0.125),
    "detector_literal": ("1/5", 10, 9, 0.1),
}
FIX_SHA = "4d497dea65ff4ecdaf0d80a6f9feb7091fabc33e"  # fix(growth): fresh start and end samples


def change_log(res: Res) -> list[str]:
    """Before/after of the detector for each fix (same fixtures and thresholds)."""
    m: Res = res["methods"]
    steady: list[Res] = res["steady_growth_control"]
    lines = [
        "## Change log",
        "",
        f"- Detector fix, window freshness (spec 1.5): before = code `{BEFORE_CODE_SHA}` "
        f"(report `{BEFORE_SHA}`), after = `{FIX_SHA}`. Fixtures, abs_floor, share_floor and z "
        "thresholds unchanged; the baseline is unchanged (it passes no freshness limits).",
        "",
        "| detector variant | recall before -> after | alert episodes | false-alarm episodes | "
        "precision (lower bound) |",
        "|---|---|---|---|---|",
    ]
    for v, (rec, eps, fa, prec) in BEFORE.items():
        a = m[v]
        lines.append(
            f"| {v} | {rec} -> {a['recall']} | {eps} -> {a['episodes']} | "
            f"{fa} -> {a['false_alarm_episodes']} | {prec} -> {a['precision']} |"
        )
    lines += [
        "",
        "Read the drop in recall with the honesty section: it is the two census-time detections "
        "that rested on a 52 h old C: sample.",
        "",
        f"- Detector fix, volume rule alerts on growth in excess of the volume's own daily "
        f"baseline (steady growth): before = code `{Z_BEFORE_CODE_SHA}` (report "
        f"`{Z_BEFORE_REPORT_SHA}`), after = `{Z_FIX_SHA}`. Fixtures, abs_floor, share_floor, "
        "z_min, windows and the key rule unchanged; the baseline is unchanged (it does not call "
        "the detector). A first attempt at this fix (`44c8a5d`, volume rate robust z >= z_min) "
        "was rejected by a falsifying verifier before merge: it compared a window-average rate "
        "with per-interval rate dispersion, so on a noisy volume a real +40 GiB / 24 h surge was "
        "suppressed (0 alerts where `e1c40b0` gave 32 on the same series) and bursty steady "
        "growth (+2 or +3 GiB once a day) still alarmed on every snapshot (MAD 0, z dominated by "
        "eps). The rule now subtracts the median daily-step rate of the previous 14 days times "
        "the window length and alerts if the excess is >= abs_floor; no new threshold.",
        "",
        "| detector variant | recall before -> after | alert episodes | false-alarm episodes | "
        "precision (lower bound) |",
        "|---|---|---|---|---|",
    ]
    for v, (rec, eps, fa, prec) in Z_BEFORE.items():
        a = m[v]
        lines.append(
            f"| {v} | {rec} -> {a['recall']} | {eps} -> {a['episodes']} | "
            f"{fa} -> {a['false_alarm_episodes']} | {prec} -> {a['precision']} |"
        )
    lines += ["", FALSE_ALARM_CAVEAT, ""]
    lines += [
        f"Synthetic controls (seed 42; `e1c40b0` and the rejected `44c8a5d` measured by running "
        f"the same control functions on the exported code, constants below; warm-up = first "
        f"{WARMUP_SNAPSHOTS} snapshots):",
        "",
        "| control | `e1c40b0` (before) | `44c8a5d` (rejected) | after |",
        "|---|---|---|---|",
    ]
    for c in steady:
        b, r = Z_BEFORE_STEADY[c["rate_gib_per_day"]], Z_REJECTED_STEADY[c["rate_gib_per_day"]]
        lines.append(
            f"| steady {c['rate_gib_per_day']} GiB/day, 84 snapshots: alerting (after warm-up) | "
            f"{b[0]} ({b[1]}) | {r[0]} ({r[1]}) | "
            f"{c['alerting_snapshots']} ({c['alerting_after_warmup']}) |"
        )
    for c in res["bursty_steady_control"]:
        b, r = Z_BEFORE_BURSTY, Z_REJECTED_BURSTY
        lines.append(
            f"| bursty steady {c['rate_gib_per_day']} GiB once a day, {c['snapshots']} snapshots: "
            f"alerting (after warm-up) | {b[0]} ({b[1]}) | {r[0]} ({r[1]}) | "
            f"{c['alerting_snapshots']} ({c['alerting_after_warmup']}) |"
        )
    for c in res["bursty_weekly_control"]:
        b, r = Z_BEFORE_BURSTY_WEEKLY, Z_REJECTED_BURSTY_WEEKLY
        lines.append(
            f"| LIMIT: bursty {c['step_gib']} GiB every {c['every_days']} days, {c['snapshots']} "
            f"snapshots: alerting (after warm-up) | {b[0]} ({b[1]}) | {r[0]} ({r[1]}) | "
            f"{c['alerting_snapshots']} ({c['alerting_after_warmup']}) |"
        )
    s = res["surge_on_steady_control"][0]
    lines.append(
        f"| {s['rate_gib_per_day']} GiB/day + {s['surge_gib']} GiB in one day: alerting from the "
        f"surge day on | {Z_BEFORE_SURGE[1]} | {Z_REJECTED_SURGE} | "
        f"{s['alerting_from_surge_start']} |"
    )
    n = res["noisy_surge_control"][0]
    lines.append(
        f"| random walk sd {n['sd_gib_per_4h']} GiB / 4 h + {n['surge_gib']} GiB over a day: "
        "alerts from the surge start (first alert, h after start) | "
        f"{Z_BEFORE_NOISY[0]} ({Z_BEFORE_NOISY[1]} h) | {Z_REJECTED_NOISY[0]} | "
        f"{n['with_surge_alerts']} ({n['with_surge_first_alert_h']} h) |"
    )
    lines.append(
        f"| the same walk without the surge: alerts | {Z_BEFORE_NOISY[2]} | "
        f"{Z_REJECTED_NOISY[1]} | {n['noise_only_alerts']} |"
    )
    lines += [
        "",
        "The rule still fires on a surge on top of steady growth and on a noisy volume; it no "
        "longer fires on the steady growth underneath. On the noisy walk the 5 GiB floor is far "
        "below the walk's own 24 h spread, so noise alone still alerts (row above): this rule "
        "cannot tell a surge from noise there, but it does not suppress the surge, which a zero "
        "would have meant (the rejected attempt's result).",
        "",
    ]
    lines += dropped_section(res["dropped_alerts"])
    lines += limits_section(res)
    return lines


def limits_section(res: Res) -> list[str]:
    """Disclosed limits of the median-excess volume rule. The weekly row is measured by this
    script; the other figures come from the falsifying verifier's probes and are NOT reproduced
    here."""
    w = res["bursty_weekly_control"][0]
    return [
        "Disclosed limits of the volume rule (median daily-step baseline):",
        "",
        "- **Steady growth is absorbed only when it lands on at least about half of the days** "
        "(the median of 14 daily steps must be non-zero). Bursts every 3, 4, 5 or 7 days alert "
        "as before the fix. Measured here: "
        f"{w['step_gib']} GiB every {w['every_days']} days (a {w['rate_gib_per_day']} GiB/day "
        f"average) gives {w['alerting_snapshots']} alerting snapshots "
        f"({w['alerting_after_warmup']} after warm-up) over {w['snapshots']}, the row marked "
        "LIMIT in the table above. Bursts every 2 days depend on their phase relative to the "
        "daily thinning: 0 to 24 alerts over 80 days in the verifier's probes (not reproduced "
        "by this script).",
        "- **Random-walk noise can delay the first alert by one or two snapshots (4-8 h)** "
        "against the pre-fix rule: 35 of 400 cases in the verifier's probe. No surge was "
        "missed. With iid level noise the alert is never later. (Verifier's probe, not "
        "reproduced here.)",
        "- **A surge of up to baseline + 5 GiB is hidden for about a week after a busy "
        "period**, while the busy days sit in the 14-day history. A sustained leak goes silent "
        "around day 10 while the volume is under 80% full; R5 is the backstop and does not "
        "fire below its threshold. (Verifier's probes; the leak control above is this "
        "script's own measurement.)",
        '- **"6 daily intervals" can be 6 samples two or more days apart.** Daily thinning '
        "keeps one sample per >= 24 h, so with gaps in the series the history can span far more "
        "than 6 days and the baseline then rests on sparse, widely spaced rates.",
        "",
    ]


def dropped_section(d: Res) -> list[str]:
    """The alert episodes of the pre-fix volume rule that the current rule no longer raises,
    listed from the replay, never counted as wins."""
    lines = [
        f"Alert episodes the pre-fix volume rule raised on the real fixtures: "
        f"{d['prefix_rule_episodes']} ({d['prefix_rule_false_alarm_episodes']} false-alarm, recall "
        f"{d['prefix_rule_recall']}); the rule no longer raises {len(d['dropped_episodes'])} of "
        "them. Any dropped alert would be DROPPED AND UNVERIFIED, not a win. " + FALSE_ALARM_CAVEAT,
        "",
        f"Of the pre-fix rule's {d['prefix_rule_alerts']} alerts on these fixtures, "
        f"{d['prefix_rule_alerts_with_a_baseline']} of its volume alerts had >= 6 daily "
        "intervals of history before their window; the rest fall back to the floor alone. "
        f"{d['prefix_rule_volume_alerts_not_raised']} pre-fix volume alerts are not raised now, "
        f"{d['prefix_rule_volume_alerts_not_raised_unexplained']} of them without a baseline "
        "explanation (a baseline existed and the excess over it was under the floor). The "
        "pre-fix alert set is the floor-only volume decision re-implemented in this script plus "
        "the key alerts of the current rule, so the key rule is not switched off in the "
        "comparison.",
        "",
    ]
    if d["dropped_episodes"]:
        lines += [
            "| first alert (UTC) | volume | growth (GiB, largest in episode) | window (h) | "
            "alerts | documented event covered |",
            "|---|---|---|---|---|---|",
        ]
    for e in d["dropped_episodes"]:
        ev = ", ".join(e["covers_documented_event"]) or "none (unverified)"
        lines.append(
            f"| {e['t']} | {e['volume']} | {e['max_growth_gib']} | {e['window_h']} | "
            f"{e['alerts']} | {ev} |"
        )
    if d["dropped_episodes"]:
        big = d["dropped_episodes"][0]
        lines += [
            "",
            f"The largest dropped alert is {big['max_growth_gib']} GiB on {big['volume']} (episode "
            f"starting {big['t']}, first alert on a {big['window_h']} h window). It is not "
            "counted as a false alarm avoided: whether it was a real, undocumented write is not "
            "known.",
        ]
    lines.append("")
    return lines


# Numbers before the volume-z fix, as published in this report at commit c768b92 (detector code
# at e1c40b0 is identical). Constants: the old detector no longer exists.
Z_BEFORE_CODE_SHA = "e1c40b0847ac11d6455f8205a0fa6534859e69c9"
Z_BEFORE_REPORT_SHA = "c768b927cedabc339bbb61be76ca352b0f260086"
Z_FIX_SHA = "53efd8563fd40c38d80d3d3aa847309dcb6995d6"  # fix(growth): excess over daily baseline
Z_BEFORE = {  # variant -> (recall, alert episodes, false-alarm episodes, precision)
    "detector": ("1/5", 5, 4, 0.2),
    "detector_literal": ("1/5", 3, 2, 0.333),
}
# Control results of e1c40b0 and of the rejected 44c8a5d, from running this file's control
# functions with that growth.py exported (git show <sha>:src/reclaim/growth.py) first on the path.
Z_BEFORE_STEADY = {1.0: (0, 0), 1.5: (0, 0), 2.0: (66, 24), 3.0: (66, 24)}  # rate -> (all, warm)
Z_REJECTED_STEADY = {1.0: (0, 0), 1.5: (0, 0), 2.0: (6, 0), 3.0: (6, 0)}
Z_BEFORE_SURGE = (66, 12)  # alerting snapshots, of which from the surge day on
Z_REJECTED_SURGE = 11
Z_BEFORE_BURSTY = (162, 120)  # 30 days of one step a day (2 or 3 GiB): all, after warm-up
Z_REJECTED_BURSTY = (162, 120)
Z_BEFORE_BURSTY_WEEKLY = (144, 126)  # 14 GiB every 7 days, 60 days: all, after warm-up
Z_REJECTED_BURSTY_WEEKLY = (144, 126)
Z_BEFORE_NOISY = (32, 4, 27)  # sd 3: surge alerts, first alert (h), noise-only alerts
Z_REJECTED_NOISY = (0, 0)  # surge alerts, noise-only alerts (the surge was suppressed)

OPEN_ISSUE = [
    "## Steady growth and the 3-day volume rule (FIXED)",
    "",
    "Steady growth of >= 1.67 GiB/day (5 GiB / 3 days) used to trip the volume rule and keep it "
    "tripped for as long as the growth lasted (2.0 and 3.0 GiB/day alerted on 66 of 84 "
    "snapshots). Fixed as applied in the change log: the volume rule now subtracts the "
    "volume's usual growth (the median of its day-to-day rates over the previous 14 days, times "
    "the window length) and alerts only if the excess is >= abs_floor. With fewer than 6 daily "
    "intervals of history there is no baseline and the floor alone still alerts, so a fresh "
    "install does not go quiet (the remaining control alerts are all in that warm-up). No "
    "threshold was changed and the fix was decided on synthetic controls, not tuned on the 5 "
    "evaluable events. Limits: a sustained leak is absorbed into the baseline once it fills "
    "half of the 14-day history (control below), and on a noisy volume noise alone still "
    "crosses the floor. Steady growth that fills a disk is still covered by the 80%-full "
    "notification (R5), not by this rule; R5 does not fire below its threshold.",
    "",
]


def render_markdown(res: Res, sha: str) -> str:
    m: Res = res["methods"]
    cov: Res = res["coverage"]
    head = [
        "metric",
        "detector (as replayed)",
        "detector (spec-literal)",
        "baseline",
        "baseline (spec-literal)",
    ]
    lines = [
        "# Disk growth attribution: eval on this machine's real logs",
        "",
        f"Produced by `python evals/growth/growth_eval.py` at commit `{sha}` (code and fixtures);",
        "machine-readable twin: `reports/growth_eval.json`. Ground truth:",
        f"`evals/fixtures/growth/events.json` ({res['events_evaluable']} evaluable of "
        f"{res['events_total']} documented events). Detector = `reclaim.growth.detect_anomalies`",
        "with spec defaults (window freshness 6 h / 1 h), replayed unchanged; baseline = alert",
        "when any key's growth over",
        "24 h or 72 h is >= 5 GiB, blamed on the largest grower. No tuning on these events.",
        "",
        f"**Summary.** As replayed, the detector finds {m['detector']['recall']} evaluable events "
        "and "
        f"the baseline {m['baseline']['recall']}; read literally (spec 1.5) the detector finds "
        f"{m['detector_literal']['recall']} and the baseline {m['baseline_literal']['recall']}. "
        f"That is {res['events_evaluable']} events from {cov['all_sources']['hours']} h of "
        "fragments: it ranks neither method and says nothing about the spec's success metric "
        "(see the honesty section).",
        "",
        *post_hoc_caveat(m),
        "| " + " | ".join(head) + " |",
        "|---|---|---|---|---|",
    ]
    rows = (
        ("recall (evaluable events detected)", "recall"),
        ("top-1 attribution (detected events with a known folder)", "top1"),
        ("alert episodes", "episodes"),
        ("false-alarm episodes", "false_alarm_episodes"),
        ("precision (lower bound)", "precision"),
    )
    for label, key in rows:
        lines.append(f"| {label} | " + " | ".join(str(m[v][key]) for v in VARIANTS) + " |")
    per_week = [
        f"{m[v]['false_alarms_per_week']['value']} (not meaningful)"
        if not m[v]["false_alarms_per_week"]["meaningful"]
        else str(m[v]["false_alarms_per_week"]["value"])
        for v in VARIANTS
    ]
    lines.append("| false-alarm episodes per week of data | " + " | ".join(per_week) + " |")
    lines += [
        "",
        FALSE_ALARM_CAVEAT,
        "",
        *change_log(res),
        "## Coverage (hours of data per source)",
        "",
    ]
    lines += [
        "| source | hours | continuous stretches | longest stretch (h) | samples | span (days) |",
        "|---|---|---|---|---|---|",
    ]
    for name, c in cov.items():
        lines.append(
            f"| {name} | {c['hours']} | {c['stretches']} | {c['longest_h']} | {c['samples']} |"
            f" {c['span_days']} |"
        )
    lines += ["", "Stretches end at a hole longer than 1 h. `census` is two snapshots, so 0 h.", ""]
    lines += ["## Evaluable events", ""]
    lines += ["| event | " + " | ".join(VARIANTS) + " |", "|---|" + "---|" * len(VARIANTS)]
    for i, row in enumerate(m["detector"]["events"]):
        cells = []
        for v in VARIANTS:
            r = m[v]["events"][i]
            if not r["detected"]:
                cells.append("missed")
                continue
            a = r["alert"]
            cells.append(
                f"detected ({a['scope']} alert {a['growth_gib']} GiB, "
                f"start {a['start_age_h']} h old, attribution {r['attribution']})"
            )
        lines.append(f"| {row['id']} | " + " | ".join(cells) + " |")
    lines += ["", "What the detector's windows saw at each evaluable event's end:", ""]
    lines += [
        "A negative volume growth means used space FELL between the window's start sample and the",
        "event's end (BELIEVED: the pagefile left C: at the 9 Oct 13:02 boot and the HF cache",
        "moved off C: 9 Oct 15:47-18:59, so a rise on top is invisible to the 5 GiB volume rule).",
        "",
    ]
    for eid, d in res["event_diagnostics"].items():
        lines.append(f"- `{eid}`: {json.dumps(d)}")
    lines += ["", "## Events not evaluable for detection", ""]
    for ev in load_events()["events"]:
        if not ev["evaluable"]:
            lines.append(f"- `{ev['id']}` ({ev['kind']}, {ev['size_gib']} GiB, "
                         f"{', '.join(map(cite_label, ev['citation']))}): "
                         f"{ev['evaluable_reason']}")  # fmt: skip
    lines += ["", "## The 3-day volume rule on steady growth", ""]
    lines += [
        "Synthetic control (deterministic, no noise, 14 days, 4-hourly snapshots). In this "
        "first table only (steady growth, no surge) every volume alert is a false alarm by "
        "construction; the surge tables further down have a real event.",
        "",
        "| growth (GiB/day) | snapshots | alerting snapshots | alerting after warm-up | "
        "first alert (day) |",
        "|---|---|---|---|---|",
    ]
    for c in res["steady_growth_control"]:
        lines.append(
            f"| {c['rate_gib_per_day']} | {c['snapshots']} | {c['alerting_snapshots']} |"
            f" {c['alerting_after_warmup']} | {c['first_alert_day']} |"
        )
    lines += [
        "",
        "Arithmetic: a steady rate r crosses the 5 GiB / 72 h floor once 3r >= 5, "
        "i.e. r >= 1.67 GiB/day. Before the volume-z fix it then stayed tripped for as long as "
        f"the growth lasted. Now only the warm-up (the first {WARMUP_SNAPSHOTS} snapshots, "
        "while the volume has < 6 daily intervals before the window and no baseline exists) can "
        "alert.",
        "",
        "Surge on top of steady growth (the fix must not blind the rule): same series, plus "
        "extra growth spread over one day ending 1 day before the end.",
        "",
        "| growth (GiB/day) | surge (GiB) | alerting snapshots | alerting after warm-up | "
        "alerting from surge day on |",
        "|---|---|---|---|---|",
    ]
    for c in res["surge_on_steady_control"]:
        lines.append(
            f"| {c['rate_gib_per_day']} | {c['surge_gib']} | {c['alerting_snapshots']} |"
            f" {c['alerting_after_warmup']} | {c['alerting_from_surge_start']} |"
        )
    lines += [
        "",
        "Bursty steady growth (one step a day, flat in between; 30 days, no event):",
        "",
        "| step (GiB/day) | snapshots | alerting snapshots | alerting after warm-up |",
        "|---|---|---|---|",
    ]
    for c in res["bursty_steady_control"]:
        lines.append(
            f"| {c['rate_gib_per_day']} | {c['snapshots']} | {c['alerting_snapshots']} |"
            f" {c['alerting_after_warmup']} |"
        )
    n = res["noisy_surge_control"][0]
    lk = res["sustained_leak_control"]
    lines += [
        "",
        f"Noisy volume (seed 42, random walk sd {n['sd_gib_per_4h']} GiB per 4 h, 20 days): "
        f"with a {n['surge_gib']} GiB surge over day 14 the detector alerts on "
        f"{n['with_surge_alerts']} snapshots from the surge start, the first "
        f"{n['with_surge_first_alert_h']} h after it; the same walk without the surge alerts on "
        f"{n['noise_only_alerts']} (first {n['noise_only_first_alert_h']} h after day 14).",
        "",
        f"Sustained leak (no pass/fail): {lk['gib_per_day']} GiB/day after 14 quiet days. The "
        f"detector alerts on {lk['alerting_snapshots']} snapshots, from "
        f"{lk['first_alert_h_after_start']} h to {lk['last_alert_h_after_start']} h after the "
        f"leak starts ({lk['days_alerting']} days between first and last), then the 14-day "
        "history absorbs it as usual. The 80%-full notification (R5) is the backstop for a leak "
        "that goes on; it does not fire below its threshold, so a leak that stays under 80% "
        "full is silent after that point.",
        "",
        "Real data: no continuous stretch reaches 72 h, so steady growth over 3 days cannot be "
        "measured on it. Net C: used change per continuous stretch of >= 2 h:",
        "",
    ]
    for d in res["c_used_drift"]:
        lines.append(
            f"- {d['start']}: {d['hours']} h, {d['net_change_gib']} GiB net,"
            f" {d['gib_per_day']} GiB/day"
        )
    stale = [
        f for f in m["detector"]["false_alarm_list"] if f["start_age_h"] > 72 or f["end_age_h"] > 1
    ]
    lines += [
        "",
        f"Of the {m['detector']['false_alarm_episodes']} as-replayed detector false-alarm "
        "episodes, "
        f"{len(stale)} rest on a start sample older than 72 h or a newest sample older than 1 h "
        "(gap artifacts); the rest are in the JSON (`methods.detector_literal`). "
        + FALSE_ALARM_CAVEAT,
        "",
        *OPEN_ISSUE,
        HONESTY.format(
            hours=cov["all_sources"]["hours"],
            stretches=cov["all_sources"]["stretches"],
            span=cov["all_sources"]["span_days"],
            longest=cov["all_sources"]["longest_h"],
            needed=res["continuous_hours_needed"],
            n_eval=res["events_evaluable"],
            n_total=res["events_total"],
        ),
        "## Fixture SHA-256 (line endings normalised to LF)",
        "",
    ]
    lines += [f"- `{k}`: `{v}`" for k, v in res["fixtures_sha256"].items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    git = ["git", "rev-parse", "HEAD"]
    sha = subprocess.run(git, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()  # noqa: S603
    res = evaluate()
    res["commit"] = sha
    out = ROOT / "reports"
    (out / "growth_eval.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    (out / "growth_eval.md").write_text(render_markdown(res, sha))
    return 0


if __name__ == "__main__":
    sys.exit(main())
