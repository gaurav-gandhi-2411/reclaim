from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from reclaim import growth
from reclaim.config import Config
from reclaim.growth import Measurement, Root, Snapshot
from reclaim.growth_alerts import (
    growth_anomaly_text,
    maybe_send_growth_toast,
    recent_growth_line,
)
from reclaim.notifications import DiskSpaceCheckResult, apply_snooze, disk_space_toast_body

# Hermetic: stores and state files live under tmp_path, the toast sender is always an injected
# fake (or a monkeypatched module function), and no drive is walked.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="volume paths are Windows paths")

NOW = 2_000_000_000.0
VOL = "C:\\"
SCRATCH = "C:\\Users\\g\\AppData\\Local\\Temp\\claude\\proj\\sess"
SECRET = "C:\\Users\\g\\work\\secretproj"  # noqa: S105 -- an excluded project path, not a password
GIB = growth.GIB
HOUR = 3600.0
EXPECTED_ANOMALY = "C: grew 20.0 GiB in 3 days — 18.0 GiB from proj\\sess"


def write_store(path: Path, now: float, *, step_age_h: float = 40.0) -> None:
    """Five days of 4-hourly snapshots ending at `now`. From `step_age_h` ago C: used is 20 GiB
    higher, 18 GiB of it the scratch session; an excluded project also grows 30 GiB (the biggest
    grower of all: it must never be named)."""
    volume = Root(VOL, "volume", VOL)
    total = Root(VOL + growth.TOTAL_SUFFIX, "volume_total", VOL)
    scratch = Root(SCRATCH, "scratch", "proj\\sess", "proj", "sess")
    secret = Root(SECRET, "toplevel", "secretproj")
    with growth.GrowthStore(path) as store:
        for k in range(30, -1, -1):
            age_h = 4.0 * k
            up = 1 if age_h <= step_age_h else 0
            store.write(
                Snapshot(
                    now - age_h * HOUR,
                    120.0,
                    True,
                    [
                        Measurement(volume, 100 * GIB + up * 20 * GIB),
                        Measurement(total, 500 * GIB),
                        Measurement(scratch, 1 * GIB + up * 18 * GIB),
                        Measurement(secret, 1 * GIB + up * 30 * GIB),
                    ],
                )
            )


def cfg(**growth_kw: object) -> Config:
    return Config.model_validate(
        {
            "notifications": {"enabled": True, "renotify_after_hours": 24.0},
            "exclusions": {"project_names": ["secretproj"]},
            "growth": growth_kw,
        }
    )


def test_recent_growth_line_names_top_contributors_but_never_an_excluded_project(
    tmp_path: Path,
) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)
    line = recent_growth_line(cfg(), store, NOW, VOL)
    assert line == "Last 3 days: +18.0 GiB from proj\\sess"


def test_recent_growth_line_is_omitted_without_fresh_enough_data(tmp_path: Path) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)
    assert recent_growth_line(cfg(), store, NOW + 2 * HOUR, VOL) is None  # newest sample is stale
    assert recent_growth_line(cfg(), tmp_path / "absent.sqlite3", NOW, VOL) is None
    assert not (tmp_path / "absent.sqlite3").exists()  # reading never creates a store


def test_growth_disabled_opens_no_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("growth IO with [growth] enabled = false")

    monkeypatch.setattr(growth.GrowthStore, "__init__", boom)
    off = cfg(enabled=False)
    sent: list[str] = []
    assert recent_growth_line(off, store, NOW, VOL) is None
    assert growth_anomaly_text(off, store, NOW, VOL) is None
    result = maybe_send_growth_toast(off, store, tmp_path / "s.json", now=NOW, send=lambda t: True)
    assert result == "disabled"
    assert sent == []


def test_anomaly_text_matches_the_spec_shape(tmp_path: Path) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)
    assert growth_anomaly_text(cfg(), store, NOW, VOL) == EXPECTED_ANOMALY


def test_no_anomaly_text_when_nothing_grew(tmp_path: Path) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW, step_age_h=-1.0)  # no step inside the stored span
    assert growth_anomaly_text(cfg(), store, NOW, VOL) is None


def test_growth_toast_has_its_own_debounce_and_leaves_the_80_percent_state_alone(
    tmp_path: Path,
) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)
    state = tmp_path / "notification_state.json"
    sent: list[str] = []

    def send(text: str) -> bool:
        sent.append(text)
        return True

    first = maybe_send_growth_toast(cfg(), store, state, now=NOW, volume=VOL, send=send)
    assert first == "sent"
    assert sent == [EXPECTED_ANOMALY]
    assert (tmp_path / "growth_alert_state.json").is_file()
    assert not state.exists()  # the 80% toast's state file was not touched
    again = maybe_send_growth_toast(cfg(), store, state, now=NOW + HOUR, volume=VOL, send=send)
    assert again == "debounced"
    assert len(sent) == 1


def test_growth_toast_not_delivered_is_not_debounced(tmp_path: Path) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)
    refused = maybe_send_growth_toast(
        cfg(), store, tmp_path / "s.json", now=NOW, volume=VOL, send=lambda _t: False
    )
    assert refused == "not_delivered"
    assert not (tmp_path / "growth_alert_state.json").exists()


def test_growth_toast_respects_snooze_and_notifications_off(tmp_path: Path) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)
    state = tmp_path / "notification_state.json"
    apply_snooze(state, snooze_days=7, now=NOW)
    snoozed = maybe_send_growth_toast(cfg(), store, state, now=NOW, send=lambda _t: True)
    assert snoozed == "snoozed"
    off = Config.model_validate({"notifications": {"enabled": False}})
    assert maybe_send_growth_toast(off, store, state, now=NOW, send=lambda _t: True) == "disabled"


def test_growth_toast_contains_a_failing_sender(tmp_path: Path) -> None:
    store = tmp_path / "g.sqlite3"
    write_store(store, NOW)

    def boom(_t: str) -> bool:
        raise RuntimeError("notifier exploded")

    result = maybe_send_growth_toast(
        cfg(), store, tmp_path / "s.json", now=NOW, volume=VOL, send=boom
    )
    assert result == "failed"


def _result(growth_line: str | None) -> DiskSpaceCheckResult:
    return DiskSpaceCheckResult("ok", 85.0, 15.0, 80.0, True, True, "would_notify", growth_line)


def test_80_percent_toast_body_gains_one_line_only_when_given() -> None:
    plain = disk_space_toast_body(_result(None))
    assert "\n" not in plain
    line = "Last 3 days: +18.0 GiB from x"
    assert disk_space_toast_body(_result(line)) == plain + "\n" + line


def test_check_disk_space_cli_adds_the_line_and_sends_the_separate_growth_toast(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from reclaim.cli import main

    store = tmp_path / "g.sqlite3"
    write_store(store, time.time())
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[notifications]\nenabled = true\ndisk_threshold_percent = 1.0\n"
        '[exclusions]\nproject_names = ["secretproj"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SYSTEMDRIVE", "C:")
    monkeypatch.setattr("reclaim.growth_alerts.scheduled_snapshot", lambda *a, **k: "not_due")
    eighty: list[DiskSpaceCheckResult] = []
    growth_texts: list[str] = []
    monkeypatch.setattr(
        "reclaim.notifications.send_disk_space_toast", lambda r: eighty.append(r) or True
    )
    monkeypatch.setattr(
        "reclaim.notifications.send_growth_toast", lambda t: growth_texts.append(t) or True
    )
    argv = [
        "check-disk-space",
        "--config",
        str(config_path),
        "--state",
        str(tmp_path / "notification_state.json"),
        "--growth-db",
        str(store),
    ]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert eighty[0].growth_line == "Last 3 days: +18.0 GiB from proj\\sess"
    assert growth_texts == [EXPECTED_ANOMALY]
    assert "growth_toast=sent" in out
    assert "secretproj" not in out.lower() + (eighty[0].growth_line or "").lower()
    # Second run: the 80% toast is debounced by its own state, the growth toast by its own.
    assert main(argv) == 0
    assert "growth_toast=debounced" in capsys.readouterr().out
    assert len(growth_texts) == 1
