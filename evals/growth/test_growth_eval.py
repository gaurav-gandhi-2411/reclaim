from __future__ import annotations

# Gate for the disk-growth eval: it asserts INVARIANTS only (fixtures load and respect their size
# and exclusion rules, every event is cited and its evaluable flag agrees with the data, metrics
# are computed with provenance). It deliberately does not assert metric VALUES: the numbers are a
# measurement reported in reports/growth_eval.md, not a pass/fail threshold set on 30 hours of
# data. Committed fixtures only: no D: access, no machine state.
import re
import time
from pathlib import Path

import growth_eval as ge
import pytest

ROOT = ge.ROOT
FORBIDDEN = ("fr-en-transformer", "shipdoc-extract", "intent-router")  # hard exclusion list


@pytest.fixture(scope="module")
def result() -> ge.Res:
    return ge.evaluate()


def test_fixtures_load_within_size_budget() -> None:
    for source in ge.SOURCES:
        path = ge.FIXTURES / f"{source}.csv"
        assert path.stat().st_size < 300_000, path
        assert ge.load_rows(source), f"{source} is empty"


def test_fixtures_never_name_an_excluded_project() -> None:
    for path in ge.FIXTURES.iterdir():
        text = path.read_text().lower()
        assert not any(name in text for name in FORBIDDEN), path.name


def test_every_event_is_cited_to_an_existing_doc_line() -> None:
    # Citations anchor on a verbatim QUOTE, not a line number: docs/RESUME.md is a living
    # checkpoint whose lines shift every session (the old line-number check passed on lines that
    # had drifted to unrelated text). `line_at_8fc9bc6` is provenance only.
    events = ge.load_events()
    cites = [c for ev in events["events"] for c in ev["citation"]]
    cites += [c for ev in events["not_events"] for c in ev["citation"]]
    assert events["events"]
    docs: dict[str, str] = {}
    for ev in events["events"]:
        assert ev["citation"], ev["id"]
        assert ev["kind"] in ("growth", "shrink")
        assert ev["evaluable_reason"], ev["id"]
    for cite in cites:
        assert isinstance(cite["line_at_8fc9bc6"], int), cite
        assert 8 <= len(cite["quote"]) <= 130, cite
        text = docs.setdefault(
            cite["doc"], (ROOT / cite["doc"]).read_text(encoding="utf-8").replace("\r\n", "\n")
        )
        assert text.count(cite["quote"]) == 1, cite


def test_evaluable_flag_agrees_with_fixture_coverage() -> None:
    ds = ge.build_dataset()
    for ev in ge.load_events()["events"]:
        used = [t for t, _ in ds.series.get(f"volume:{ev['volume']}:used", [])]

        def near(t: float, used: list[float] = used) -> bool:
            return any(abs(u - t) <= ge.COVER_TOL_S for u in used)

        covered = near(ge.epoch(ev["start"])) and near(ge.epoch(ev["end"]))
        if ev["evaluable"]:
            assert ev["kind"] == "growth", ev["id"]
            has_key = ev["fixture_key"] is None or ev["fixture_key"] in ds.series
            # A census-only event has no volume sample at its ends; its key series stands in.
            assert covered or ev["fixture_key"], ev["id"]
            assert has_key, ev["id"]


def test_evaluate_computes_every_metric_with_provenance(result: ge.Res) -> None:
    assert set(result["methods"]) == set(ge.VARIANTS)
    n_eval = result["events_evaluable"]
    assert n_eval > 0
    for name, m in result["methods"].items():
        assert m["recall"].endswith(f"/{n_eval}"), name
        assert m["episodes"] >= m["false_alarm_episodes"] >= 0, name
        assert len(m["events"]) == n_eval, name
    assert set(result["fixtures_sha256"]) == {p.name for p in ge.FIXTURES.iterdir()}
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in result["fixtures_sha256"].values())
    for source in ge.SOURCES:
        assert result["coverage"][source]["samples"] > 0


def test_per_week_figures_are_labelled_by_coverage(result: ge.Res) -> None:
    longest = result["coverage"]["all_sources"]["longest_h"]
    for m in result["methods"].values():
        assert m["false_alarms_per_week"]["meaningful"] == (longest >= ge.MIN_CONTINUOUS_H)


def test_replay_is_fast_and_deterministic() -> None:
    t0 = time.perf_counter()
    first = ge.evaluate()
    assert time.perf_counter() - t0 < 10
    assert first == ge.evaluate()


def test_steady_growth_control_matches_the_arithmetic() -> None:
    # 3 days of r GiB/day cross 5 GiB iff r >= 1.67: below it never alerts; above it only the
    # no-history warm-up alerts (volume z is None), never once the volume has history.
    assert ge.steady_growth_control(1.0)["alerting_snapshots"] == 0
    steady = ge.steady_growth_control(3.0)
    assert steady["alerting_snapshots"] > 0
    assert steady["alerting_after_warmup"] == 0


def test_surge_on_steady_growth_still_alerts() -> None:
    surge = ge.steady_growth_control(2.0, surge_gib=20)
    assert surge["alerting_from_surge_start"] > 0


def _alert(t: float, key: str = "k", growth: int = 6 * ge.GIB) -> ge.Alert:
    return ge.Alert(t, "key", "C", key, growth, 24, key, t - 86400, t)


def test_episodes_merge_close_alerts_and_repeats_but_not_distant_ones() -> None:
    h = 3600.0
    near = ge.episodes([_alert(0), _alert(2 * h), _alert(4 * h)])
    assert len(near) == 1 and len(near[0].alerts) == 3
    repeat = ge.episodes([_alert(0), _alert(20 * h)])  # same growth figure: stale re-report
    assert len(repeat) == 1
    apart = ge.episodes([_alert(0), _alert(20 * h, growth=7 * ge.GIB)])
    assert len(apart) == 2
    assert len(ge.episodes([_alert(0, "a"), _alert(0, "b")])) == 2


def test_covers_requires_volume_overlap_and_size_band() -> None:
    ev = {
        "volume": "C",
        "start": "2026-10-09T10:00:00",
        "end": "2026-10-09T11:00:00",
        "size_gib": 10.0,
    }
    t = ge.epoch("2026-10-09T12:00:00")
    ok = ge.Alert(t, "volume", "C", "C:\\", 10 * ge.GIB, 24, None, t - 86400, t)
    assert ge.covers(ok, ev)
    assert not ge.covers(ge.Alert(t, "volume", "D", "D:\\", 10 * ge.GIB, 24, None, t - 1, t), ev)
    assert not ge.covers(ge.Alert(t, "volume", "C", "C:\\", 30 * ge.GIB, 24, None, t - 1, t), ev)
    late = ge.epoch("2026-10-09T18:00:01")
    assert not ge.covers(ge.Alert(late, "volume", "C", "C:\\", 10 * ge.GIB, 24, None, 0, late), ev)


def test_attribution_levels() -> None:
    f = "C:\\Users\\x\\Temp"
    assert ge.attribution("C:\\Users\\x\\Temp\\claude", f) == "correct"
    assert ge.attribution("C:\\Users\\x", f) == "coarse"
    assert ge.attribution("C:\\Other", f) == "wrong"
    assert ge.attribution(None, f) == "none"


def test_report_writer_targets_reports_dir() -> None:
    assert (Path(ge.__file__).resolve().parents[2] / "reports").is_dir()


def _methods(detected: dict[str, list[str]]) -> ge.Res:
    ids = ["old-event", "hf-copy-to-d-2026-10-09"]
    return {
        v: {"events": [{"id": i, "detected": i in detected.get(v, [])} for i in ids]}
        for v in ge.VARIANTS
    }


def test_a_detection_of_a_post_hoc_event_is_flagged_in_the_summary() -> None:
    lines = ge.post_hoc_caveat(_methods({"detector": ["hf-copy-to-d-2026-10-09"]}))
    text = "\n".join(lines)
    assert "Not an independent hit" in text and "`hf-copy-to-d-2026-10-09`" in text
    assert "the detector finds 0/1 as replayed" in text  # without the post-hoc event: 0 of 1
    assert ge.post_hoc_caveat(_methods({"detector": ["old-event"]})) == []
    assert ge.post_hoc_caveat(_methods({})) == []


def test_the_published_report_carries_the_caveat(result: ge.Res) -> None:
    sha = "0" * 40
    text = ge.render_markdown(result, sha)
    flagged = any(
        r["detected"] and r["id"] in ge.POST_HOC_EVENTS
        for v in ge.VARIANTS
        for r in result["methods"][v]["events"]
    )
    assert ("Not an independent hit" in text) == flagged
