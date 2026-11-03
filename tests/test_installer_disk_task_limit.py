from __future__ import annotations

# The installer's disk-space task ("Reclaim Disk Space Check (<user>)") runs `check-disk-space`,
# which takes a growth snapshot bounded by `[growth] budget_seconds` plus unbounded index queries.
# A 2-minute ExecutionTimeLimit once equalled that budget: the measured run took 157 s, Task
# Scheduler killed it every time (0x41306), and neither the snapshot nor the low-disk alert ever
# completed. This gate keeps the limit above the budget with real margin.
import re
from pathlib import Path

from reclaim.config import GrowthConfig

_ISS = Path(__file__).parent.parent / "packaging" / "reclaim.iss"
_MARGIN_SECONDS = 5 * 60


def _iso_duration_seconds(value: str) -> int:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    assert match and any(match.groups()), f"unparseable ISO 8601 duration: {value!r}"
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def test_disk_space_task_limit_exceeds_the_snapshot_budget_by_five_minutes() -> None:
    text = _ISS.read_text(encoding="utf-8")
    limits = re.findall(r"<ExecutionTimeLimit>([^<]+)</ExecutionTimeLimit>", text)
    assert len(limits) == 1, f"expected exactly one task limit in reclaim.iss, found {limits}"
    limit = _iso_duration_seconds(limits[0])
    budget = GrowthConfig().budget_seconds
    assert limit >= budget + _MARGIN_SECONDS, (
        f"ExecutionTimeLimit {limits[0]} ({limit}s) must exceed the default growth snapshot "
        f"budget ({budget}s) by at least {_MARGIN_SECONDS}s"
    )


def test_iso_duration_parser() -> None:
    assert _iso_duration_seconds("PT10M") == 600
    assert _iso_duration_seconds("PT90S") == 90
    assert _iso_duration_seconds("PT1H30M") == 5400
