from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from reclaim import cli, logging_config
from reclaim import regenerable as rg
from reclaim.api import service
from reclaim.config import Config, load_config
from reclaim.regenerable import RegenerableEnv, RegenerableItemResult, run_regenerable_clean

# ADR-0034 addendum "pytest temp". Every path lives under tmp_path; the clock is fixed.

NOW = 2_000_000_000.0
DAY = 86400.0


def _write(path: Path, size: int = 100, *, age_days: float = 30.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _age_tree(root: Path, age_days: float) -> None:
    """Back-date every directory under (and including) `root` -- files are set by `_write`."""
    stamp = NOW - age_days * DAY
    for dirpath, dirnames, _files in os.walk(root, topdown=False):
        for d in dirnames:
            os.utime(Path(dirpath) / d, (stamp, stamp))
    os.utime(root, (stamp, stamp))


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.temp = tmp_path / "Temp"
        self.root = self.temp / "pytest-of-someone"
        self.root.mkdir(parents=True)
        self.locked: set[str] = set()

    def basetemp(self, n: int, *, age_days: float = 30.0, size: int = 100) -> Path:
        base = self.root / f"pytest-{n}"
        _write(base / "test_a0" / "data.bin", size, age_days=age_days)
        _age_tree(base, age_days)
        return base

    def env(self, **overrides: object) -> RegenerableEnv:
        base: dict[str, object] = {
            "home": self.temp.parent,
            "local_appdata": self.temp.parent,
            "temp_roots": (self.temp,),
            "crash_dump_roots": (),
            "now": lambda: NOW,
            "running_process_names": lambda: frozenset(),
            "which": lambda _name: None,
            "has_open_handle": lambda p: p in self.locked,
            "pytest_temp_roots": (self.root,),
            "pytest_temp_mode": "delete",
        }
        base.update(overrides)
        return RegenerableEnv(**base)  # type: ignore[arg-type]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _item(report: rg.RegenerableReport) -> RegenerableItemResult:
    return next(i for i in report.items if i.kind == "pytest_temp")


def _run(env: RegenerableEnv, *, apply: bool = True) -> rg.RegenerableReport:
    return run_regenerable_clean(env, apply=apply, audit_log_path=None)


def test_threshold_is_the_named_seven_day_constant() -> None:
    assert rg.PYTEST_TEMP_MIN_AGE_SECONDS == 7 * 24 * 3600.0
    assert RegenerableEnv.__dataclass_fields__["pytest_temp_mode"].default == "off"


def test_old_basetemp_is_cleaned_and_bytes_counted(world: World) -> None:
    old = world.basetemp(3, size=500)
    report = _run(world.env())
    item = _item(report)
    assert item.status == "cleaned"
    assert item.bytes_removed == 500 and item.files_removed == 1
    assert not old.exists()
    assert world.root.is_dir(), "the pytest-of-<user> parent is never a candidate"
    assert item.applied_paths == [str(old)]


def test_dry_run_lists_would_clean_and_deletes_nothing(world: World) -> None:
    old = world.basetemp(3, size=500)
    young = world.basetemp(4, age_days=1)
    item = _item(_run(world.env(), apply=False))
    assert item.status == "would_clean"
    assert item.bytes_removed == 500
    assert old.exists() and young.exists()
    assert "pytest-3" in item.detail and "pytest-4" in item.detail
    assert "ownership" in item.detail  # the caveat travels with the report


def test_old_own_mtime_but_fresh_file_inside_is_kept(world: World) -> None:
    base = world.basetemp(5, age_days=30)
    fresh = _write(base / "test_a0" / "live.log", 10, age_days=0.5)
    _age_tree(base, 30)  # the directory's own mtimes say "old"; the content says "in use"
    item = _item(_run(world.env()))
    assert fresh.exists() and base.exists()
    assert item.status == "nothing_to_clean"


def test_exactly_seven_days_qualifies_just_under_does_not(world: World) -> None:
    exact = world.basetemp(1, age_days=7.0)
    under = world.basetemp(2, age_days=7.0 - 1 / 86400)
    _run(world.env())
    assert not exact.exists(), "age == 7 d is 'at least 7 d old' (same >= as aged temp)"
    assert under.exists()


def test_non_matching_names_link_and_nested_names_are_ignored(world: World) -> None:
    keep = [
        _write(world.root / "pytest-current-old" / "f", age_days=30),
        _write(world.root / "pytest-" / "f", age_days=30),
        _write(world.root / "pytest-12.bak" / "f", age_days=30),
        _write(world.root / "xpytest-12" / "f", age_days=30),
        _write(world.root / "other" / "pytest-9" / "f", age_days=30),  # nested, not a child
        _write(world.root / "pytest-7", age_days=30),  # a FILE named like a basetemp
    ]
    for d in ("pytest-current-old", "pytest-", "pytest-12.bak", "xpytest-12", "other"):
        _age_tree(world.root / d, 30)
    report = _run(world.env())
    assert all(p.exists() for p in keep)
    assert _item(report).status == "nothing_to_clean"


def test_pytest_current_symlink_is_ignored_and_target_survives(world: World) -> None:
    target = world.basetemp(8, age_days=30)
    link = world.root / "pytest-current"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create a symlink here")
    _run(world.env(), apply=False)
    assert link.is_symlink()
    report = _run(world.env())
    assert not target.exists()  # the real basetemp was cleaned via its own name
    assert link.is_symlink() or not link.exists()
    assert report.items  # the link itself never produced an error


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_junction_named_like_a_basetemp_and_junction_inside_are_never_followed(
    world: World, tmp_path: Path
) -> None:
    outside = tmp_path / "precious"
    keep = _write(outside / "keep.txt", 50, age_days=400)
    top = world.root / "pytest-6"
    subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(top), str(outside)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    current_target = world.basetemp(9, age_days=1)  # young, so only the link could be at risk
    current = world.root / "pytest-current"
    subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(current), str(current_target)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    inside = world.basetemp(7, age_days=30)
    subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(inside / "link"), str(outside)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    _run(world.env())
    assert keep.exists(), "a junction's target must never be deleted"
    assert os.path.lexists(top), "a junction named pytest-<N> is ignored, not unlinked"
    assert os.path.lexists(current) and current_target.exists(), "pytest-current is ignored"
    assert not inside.exists(), "the real old basetemp (and the link inside it) is gone"


def test_open_handle_skips_the_whole_directory(world: World) -> None:
    base = world.basetemp(10)
    other = _write(base / "test_b0" / "also.bin", age_days=30)
    _age_tree(base, 30)
    world.locked.add(str(base / "test_a0" / "data.bin"))
    item = _item(_run(world.env()))
    assert item.status == "skipped_in_use"
    assert other.exists() and (base / "test_a0" / "data.bin").exists()
    assert "open in another process" in item.detail
    assert item.skipped_paths == [str(base)]


def test_open_handle_detection_error_fails_closed(world: World) -> None:
    base = world.basetemp(11)

    def boom(_path: str) -> bool:
        raise OSError("handle probe broke")

    item = _item(_run(world.env(has_open_handle=boom)))
    assert item.status == "skipped_in_use"
    assert base.exists()
    assert "open-handle check failed" in item.detail


def test_exclusion_pattern_skips_and_is_counted_not_applied(world: World) -> None:
    base = world.basetemp(12)
    _write(base / "test_a0" / "keepme-notes.txt", age_days=30)
    _age_tree(base, 30)
    free = world.basetemp(13)
    patterns = ("*keepme*",)
    report = _run(world.env(excluded_patterns=patterns))
    item = _item(report)
    assert base.exists() and not free.exists()
    assert len(item.excluded) == 1 and "keepme" in item.excluded[0]
    assert rg.count_excluded_applied(report.applied_paths, patterns) == 0
    assert report.excluded == item.excluded


def test_everything_excluded_reports_skipped_excluded(world: World) -> None:
    base = world.basetemp(14)
    _write(base / "test_a0" / "keepme-only.txt", age_days=30)
    _age_tree(base, 30)
    item = _item(_run(world.env(excluded_patterns=("*keepme*",))))
    assert item.status == "skipped_excluded"
    assert base.exists()


def test_excluded_parent_root_is_skipped_whole(world: World) -> None:
    base = world.basetemp(15)
    item = _item(_run(world.env(excluded_patterns=("*pytest-of-someone*",))))
    assert item.status == "skipped_excluded" and base.exists()


def test_flag_off_item_is_not_planned_and_nothing_is_deleted(world: World) -> None:
    old = world.basetemp(20)
    env = world.env(pytest_temp_mode="off")
    for apply in (True, False):
        report = _run(env, apply=apply)
        assert not [i for i in report.items if i.kind == "pytest_temp"]
    assert old.exists()


def test_report_mode_never_deletes_even_when_apply_is_requested(world: World) -> None:
    old = world.basetemp(21)
    item = _item(_run(world.env(pytest_temp_mode="report"), apply=True))
    assert old.exists()
    assert item.status == "would_clean" and item.files_removed == 1  # a dry-run style report
    assert item.applied_paths == []


def test_aged_temp_never_sweeps_pytest_of_user_even_when_all_old(world: World) -> None:
    old = world.basetemp(22, age_days=60)
    _age_tree(world.root, 60)
    for mode in ("off", "report", "delete"):
        report = _run(world.env(pytest_temp_mode=mode, pytest_temp_roots=()))
        temp_item = next(i for i in report.items if i.key == "temp0")
        assert old.exists(), mode
        assert "pytest-of-<user>" in temp_item.detail


def test_touched_after_planning_is_kept(world: World) -> None:
    base = world.basetemp(23)
    victim = base / "test_a0" / "data.bin"

    def touch_then_report_free(path: str) -> bool:
        # Runs during the apply-time handle probe, i.e. after the plan's scan and before the
        # delete: simulates a pytest run that starts writing into the directory right now.
        os.utime(victim, (NOW, NOW))
        return False

    item = _item(_run(world.env(has_open_handle=touch_then_report_free)))
    assert victim.exists() and base.exists()
    assert item.status == "nothing_to_clean"
    assert item.skipped_paths == [str(base)]


def test_directory_with_virtualenv_is_left_for_review(world: World) -> None:
    base = world.basetemp(24)
    _write(base / "venv_case" / "pyvenv.cfg", 5, age_days=30)
    _age_tree(base, 30)
    item = _item(_run(world.env()))
    assert base.exists() and item.status == "nothing_to_clean"
    assert "virtualenv" in item.detail


def test_missing_root_is_skipped_not_present(world: World) -> None:
    item = _item(_run(world.env(pytest_temp_roots=(world.temp / "pytest-of-nobody",))))
    assert item.status == "skipped_not_present"


def test_from_os_environment_points_at_pytest_of_user_under_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(rg, "_pytest_user_name", lambda: "zed")
    env = RegenerableEnv.from_os_environment()
    assert env.pytest_temp_roots == (tmp_path / "pytest-of-zed",)
    assert env.pytest_temp_mode == "off"


# --- config / service / CLI -----------------------------------------------------------------


def test_config_default_is_off_and_toml_opt_in_parses(tmp_path: Path) -> None:
    assert Config().regenerable.pytest_temp is False
    path = tmp_path / "config.toml"
    path.write_text("[regenerable]\npytest_temp = true\n", encoding="utf-8")
    assert load_config(path).regenerable.pytest_temp is True


@pytest.fixture
def cli_world(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    monkeypatch.setattr(service, "regenerable_clean_env", world.env)
    monkeypatch.setattr(rg, "DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(cli, "DEFAULT_LOG_PATH", tmp_path / "reclaim.log")
    monkeypatch.setattr(cli, "assert_not_elevated", lambda: None)
    # `cli.main` re-points the ROOT logger's stderr handler at this test's capsys stream; left
    # attached, a later test in the same process logs into a closed file (and the "Logging error"
    # traceback's own os.stat calls break tests/test_restat_decision_points.py's stat counter).
    handlers_before = list(logging.getLogger().handlers)
    configured_before = logging_config._configured_for_path
    # The default env fixture mode is "delete"; the service overrides it per run.
    yield world
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if handler not in handlers_before:
            root_logger.removeHandler(handler)
            handler.close()
    logging_config._configured_for_path = configured_before


def _cfg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "c.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_cli_default_never_lists_or_deletes_pytest_temp(
    cli_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = cli_world.basetemp(30)
    config = _cfg(tmp_path, "[autoclean]\nenabled = true\n")
    argv = ["auto-clean", "--apply", "--scheduled", "--json", "--config", str(config)]
    assert cli.main(argv) == 0
    body = json.loads(capsys.readouterr().out)
    assert old.exists()
    assert not [i for i in body["items"] if i["kind"] == "pytest_temp"]


def test_cli_include_flag_reports_in_dry_run_without_deleting(
    cli_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = cli_world.basetemp(31, size=321)
    config = _cfg(tmp_path, "")
    assert cli.main(["auto-clean", "--include-pytest-temp", "--json", "--config", str(config)]) == 0
    body = json.loads(capsys.readouterr().out)
    item = next(i for i in body["items"] if i["kind"] == "pytest_temp")
    assert item["status"] == "would_clean" and item["bytes_removed"] == 321
    assert old.exists()


def test_cli_include_flag_cannot_widen_an_apply_run(
    cli_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = cli_world.basetemp(32)
    config = _cfg(tmp_path, "")
    code = cli.main(["auto-clean", "--apply", "--include-pytest-temp", "--config", str(config)])
    assert code == 2 and old.exists()
    assert "dry-run only" in capsys.readouterr().err


def test_cli_config_opt_in_deletes_on_apply(cli_world: World, tmp_path: Path) -> None:
    old = cli_world.basetemp(33)
    config = _cfg(tmp_path, "[regenerable]\npytest_temp = true\n")
    assert cli.main(["auto-clean", "--apply", "--json", "--config", str(config)]) == 0
    assert not old.exists()


def test_cli_opt_in_still_honours_exclusions_and_reports_excluded_applied(
    cli_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    protected = cli_world.basetemp(34)
    _write(protected / "test_a0" / "my-protected-proj.txt", age_days=30)
    _age_tree(protected, 30)
    config = _cfg(
        tmp_path,
        '[regenerable]\npytest_temp = true\n[exclusions]\nproject_names = ["my-protected-proj"]\n',
    )
    assert cli.main(["auto-clean", "--apply", "--json", "--config", str(config)]) == 0
    body = json.loads(capsys.readouterr().out)
    assert protected.exists()
    assert body["excluded_applied"] == 0
    assert any("my-protected-proj" in e for e in body["excluded"])
