from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import reclaim.relocate as relocate
from reclaim.cli import main


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = Path(os.environ["USERPROFILE"])
    local = Path(os.environ["LOCALAPPDATA"])
    hub = home / ".cache" / "huggingface" / "hub" / f"t-{tmp_path.name}"
    d = tmp_path / "D"
    d.mkdir()
    d_key = relocate._key(d)
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p: 2 if relocate._key(p).startswith(d_key) else 1
    )
    return {
        "home": home,
        "local": local,
        "source": tmp_path / "C" / "models",
        "hub": hub,
        "d": d,
        "journals": tmp_path / "journals",
        "config": tmp_path / "config.toml",
    }


def _cli(layout: dict[str, Path], *args: str) -> int:
    return main(
        [
            "relocate",
            "--config",
            str(layout["config"]),
            "--journal-dir",
            str(layout["journals"]),
            *args,
        ]
    )


def test_list_classifies_the_known_locations_read_only(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    uv = layout["local"] / "uv" / "cache"
    uv.mkdir(parents=True)
    (uv / "blob").write_bytes(b"x")
    extra = layout["source"]
    extra.mkdir(parents=True)
    (extra / "w.bin").write_bytes(b"w" * 100)

    assert (
        _cli(layout, "list", "--json", "--target-drive", str(layout["d"]), "--path", str(extra))
        == 0
    )

    rows = {Path(r["path"]): r for r in json.loads(capsys.readouterr().out)["candidates"]}
    assert rows[layout["local"] / "uv"]["verdict"] == "must_stay"
    assert rows[extra]["verdict"] == "movable"
    assert rows[extra]["bytes"] == 100
    assert rows[extra]["suggested_target"].startswith(str(layout["d"] / "reclaim-relocated"))
    assert (extra / "w.bin").exists()
    assert not layout["journals"].exists()


def test_move_without_yes_is_only_the_plan(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    src = layout["source"]
    src.mkdir(parents=True)
    (src / "a.bin").write_bytes(b"a" * 10)
    target = layout["d"] / "models"

    assert _cli(layout, "move", str(src), "--target", str(target)) == 0

    out = capsys.readouterr().out
    assert "dry run OK" in out and "--yes" in out
    assert not target.exists()
    assert not relocate._is_reparse(str(src))


def test_move_status_rollback_round_trip(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    src = layout["source"]
    src.mkdir(parents=True)
    (src / "a.bin").write_bytes(b"a" * 10)
    target = layout["d"] / "models"

    assert _cli(layout, "move", str(src), "--target", str(target), "--yes") == 0
    assert relocate._is_reparse(str(src))
    (journal,) = relocate.journals(layout["journals"])
    capsys.readouterr()
    assert _cli(layout, "status") == 0
    assert journal.id in capsys.readouterr().out
    assert _cli(layout, "delete-old", journal.id) == 1  # inside the 7 days, no --confirm
    assert "kept for" in capsys.readouterr().err

    assert _cli(layout, "rollback", journal.id) == 0

    assert not relocate._is_reparse(str(src))
    assert (src / "a.bin").read_bytes() == b"a" * 10
    assert not target.exists()


def test_a_refused_plan_exits_1_and_says_why(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    src = layout["source"]
    (src / ".venv").mkdir(parents=True)
    (src / ".venv" / "pyvenv.cfg").write_text("home =", encoding="utf-8")
    assert _cli(layout, "plan", str(src), "--target", str(layout["d"] / "x")) == 1
    assert "Python environment" in capsys.readouterr().out


def test_an_unknown_move_id_is_an_error(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(layout, "rollback", "nope") == 1
    assert "no move with id nope" in capsys.readouterr().err
