from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reclaim import autoclean_state, cli, notifications
from reclaim.api import service
from reclaim.api.schemas import RegenerableCleanResponse, RegenerableItemOut
from reclaim.autoclean_state import AutoCleanState, write_state
from reclaim.elevation import ElevatedProcessError

# Contract: `reclaim auto-clean --json` writes exactly ONE JSON document to stdout on every exit
# path, human text goes to stderr, and exit codes are unchanged. Every service call is an injected
# fake -- nothing is cleaned, scheduled or toasted.

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _response(*, apply: bool, excluded_applied: int = 0) -> RegenerableCleanResponse:
    return RegenerableCleanResponse(
        run_id="run-1",
        apply=apply,
        items=[
            RegenerableItemOut(
                kind="temp",
                key="temp",
                label="Temp files",
                status="cleaned",
                bytes_removed=10,
                bytes_removed_human="10 B",
                files_removed=1,
                files_skipped_in_use=0,
                detail="",
                skipped_paths=[],
            )
        ],
        bytes_removed=10,
        bytes_removed_human="10 B",
        files_skipped_in_use=0,
        disk_free_before_bytes=None,
        disk_free_after_bytes=None,
        disk_free_delta_bytes=None,
        percent_used_after=None,
        duration_seconds=0.5,
        excluded_applied=excluded_applied,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "assert_not_elevated", lambda: None)
    monkeypatch.setattr(autoclean_state, "utc_now", lambda: NOW)
    monkeypatch.setattr(notifications, "send_autoclean_toast", lambda *_a: True)


def _config(tmp_path: Path, *, enabled: bool) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f"[autoclean]\nenabled = {str(enabled).lower()}\n", encoding="utf-8")
    return path


def _run(capsys: pytest.CaptureFixture[str], argv: Sequence[str]) -> tuple[int, str, str, object]:
    code = cli.main(["auto-clean", *argv, "--json"])
    captured = capsys.readouterr()
    # json.loads fails on any extra text, so success == "exactly one document, nothing else".
    return code, captured.out, captured.err, json.loads(captured.out)


def _fake_service(monkeypatch: pytest.MonkeyPatch, **kwargs: int) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake(**call_kwargs: object) -> RegenerableCleanResponse:
        calls.append(call_kwargs)
        return _response(apply=bool(call_kwargs["apply"]), **kwargs)

    monkeypatch.setattr(service, "regenerable_clean_response", fake)
    return calls


def test_normal_path_shape_is_the_response_model_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_service(monkeypatch)
    code, out, err, doc = _run(capsys, ["--config", str(_config(tmp_path, enabled=True))])
    assert code == 0 and err == ""
    assert out == _response(apply=False).model_dump_json(indent=2) + "\n"
    assert isinstance(doc, dict) and "status" not in doc and doc["run_id"] == "run-1"


def test_apply_normal_path_is_one_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_service(monkeypatch)
    cfg = _config(tmp_path, enabled=True)
    code, _out, _err, doc = _run(capsys, ["--apply", "--config", str(cfg)])
    assert code == 0 and isinstance(doc, dict) and doc["apply"] is True


def test_disabled_scheduled_emits_skipped_json_and_human_text_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_service(monkeypatch)
    cfg = _config(tmp_path, enabled=False)
    code, _out, err, doc = _run(capsys, ["--apply", "--scheduled", "--config", str(cfg)])
    assert code == 0 and calls == []
    assert doc == {"status": "skipped", "reason": "autoclean_disabled", "applied": False}
    assert "turned off" in err


def test_scheduled_noop_emits_skipped_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_service(monkeypatch)
    write_state(AutoCleanState(NOW - timedelta(days=1)), autoclean_state.default_state_path())
    cfg = _config(tmp_path, enabled=True)
    code, _out, err, doc = _run(capsys, ["--apply", "--scheduled", "--config", str(cfg)])
    assert code == 0 and calls == []
    assert doc == {"status": "skipped", "reason": "nothing_to_do", "applied": False}
    assert "nothing to do" in err


@pytest.mark.parametrize("pending", [frozenset[str](), frozenset({"uv"})])
def test_scheduled_full_and_retry_emit_the_normal_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pending: frozenset[str],
) -> None:
    calls = _fake_service(monkeypatch)
    if pending:  # retry: a recent full run with uv left in use
        state = AutoCleanState(NOW - timedelta(days=1), pending)
        write_state(state, autoclean_state.default_state_path())
    cfg = _config(tmp_path, enabled=True)
    code, _out, err, doc = _run(capsys, ["--apply", "--scheduled", "--config", str(cfg)])
    assert code == 0 and isinstance(doc, dict) and doc["run_id"] == "run-1"
    assert "scheduled run mode=" in err
    assert (calls[0]["only_keys"] is not None) == bool(pending)


def test_busy_emits_error_json_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def busy(**_kwargs: object) -> RegenerableCleanResponse:
        raise service.RegenerableCleanBusyError("a clean is already running")

    monkeypatch.setattr(service, "regenerable_clean_response", busy)
    code, _out, err, doc = _run(capsys, ["--config", str(_config(tmp_path, enabled=True))])
    assert code == 1
    assert doc == {
        "status": "error",
        "reason": "busy",
        "applied": False,
        "message": "a clean is already running",
    }
    assert "already running" in err


def test_uncaught_run_error_emits_error_json_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**_kwargs: object) -> RegenerableCleanResponse:
        raise OSError("disk on fire")

    monkeypatch.setattr(service, "regenerable_clean_response", boom)
    cfg = _config(tmp_path, enabled=True)
    code, _out, err, doc = _run(capsys, ["--apply", "--config", str(cfg)])
    assert code == 1
    assert doc == {
        "status": "error",
        "reason": "run_failed",
        "applied": False,
        "message": "OSError: disk on fire",
    }
    assert "run failed: OSError: disk on fire" in err


def test_config_error_emits_error_json_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[autoclean\n", encoding="utf-8")
    code, _out, err, doc = _run(capsys, ["--config", str(bad)])
    assert code == 1
    assert isinstance(doc, dict)
    assert doc["status"] == "error" and doc["reason"] == "config_invalid"
    assert doc["applied"] is False and "config.toml is invalid" in err


def test_elevated_emits_error_json_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def elevated() -> None:
        raise ElevatedProcessError("running as Administrator")

    monkeypatch.setattr(cli, "assert_not_elevated", elevated)
    code, _out, err, doc = _run(capsys, ["--config", str(_config(tmp_path, enabled=True))])
    assert code == 1
    assert doc == {
        "status": "error",
        "reason": "elevated",
        "applied": False,
        "message": "running as Administrator",
    }
    assert "running as Administrator" in err


def test_exclusion_invariant_violation_is_one_document_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_service(monkeypatch, excluded_applied=2)
    cfg = _config(tmp_path, enabled=True)
    code, _out, err, doc = _run(capsys, ["--apply", "--config", str(cfg)])
    assert code == 1
    assert isinstance(doc, dict) and doc["excluded_applied"] == 2
    assert "INVARIANT VIOLATION" in err


def test_disabled_without_json_keeps_the_human_line_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path, enabled=False)
    assert cli.main(["auto-clean", "--apply", "--scheduled", "--config", str(cfg)]) == 0
    assert "turned off" in capsys.readouterr().out
