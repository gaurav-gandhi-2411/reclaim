from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reclaim.mode import (
    MODE_LOG_SCHEMA_VERSION,
    REQUIRED_POWER_MODE_CONFIRMATION,
    ModeChangeEntry,
    ModeSwitchDeniedError,
    current_mode,
    switch_to_power_mode,
    switch_to_safe_mode,
)
from reclaim.models import Mode

_NOW = 1_700_000_000.0


class _RecordingLogger:
    """Minimal stand-in for `mode.py`'s module-level `structlog` logger -- see the identical
    helper in `tests/test_manifest_schema_versioning.py` for why a hand-rolled recorder is used
    instead of `caplog` (this project's structlog isn't wired to stdlib logging)."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:  # pragma: no cover - unused here
        pass


def _mode_entry_dict(**overrides: Any) -> dict[str, Any]:
    """A complete, current-shape mode-log line as a plain dict (not via the pydantic model), so
    backward/forward-compat tests can freely add/remove keys to simulate other versions/releases."""
    data: dict[str, Any] = {
        "from_mode": "safe",
        "to_mode": "power",
        "changed_at": _NOW,
        "confirmed": True,
        "schema_version": MODE_LOG_SCHEMA_VERSION,
    }
    data.update(overrides)
    return data


def _read_lines(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- current_mode: SAFE default -----------------------------------------------------------------


def test_current_mode_defaults_to_safe_when_log_missing(tmp_path: Path) -> None:
    log_path = tmp_path / "mode_log.jsonl"
    assert current_mode(log_path) == Mode.SAFE


# --- switch_to_power_mode: the only SAFE -> POWER gate in the codebase -----------------------


def test_switch_to_power_mode_denies_wrong_confirmation_and_leaves_mode_unchanged(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "mode_log.jsonl"

    with pytest.raises(ModeSwitchDeniedError):
        switch_to_power_mode("nope, not typing that", log_path=log_path, now=_NOW)

    assert current_mode(log_path) == Mode.SAFE
    assert not log_path.exists()  # rejected attempt appends nothing at all


def test_switch_to_power_mode_denies_typo_confirmation(tmp_path: Path) -> None:
    """A single-character typo is never "close enough" -- exact match only, no normalization."""
    log_path = tmp_path / "mode_log.jsonl"
    typo = REQUIRED_POWER_MODE_CONFIRMATION[:-1] + "X"  # last char wrong

    with pytest.raises(ModeSwitchDeniedError):
        switch_to_power_mode(typo, log_path=log_path, now=_NOW)

    assert current_mode(log_path) == Mode.SAFE
    assert not log_path.exists()


def test_switch_to_power_mode_denies_partial_confirmation(tmp_path: Path) -> None:
    """A truncated/partial phrase (a user who stopped typing early) is refused, not accepted as
    a prefix match."""
    log_path = tmp_path / "mode_log.jsonl"
    partial = REQUIRED_POWER_MODE_CONFIRMATION[:10]

    with pytest.raises(ModeSwitchDeniedError):
        switch_to_power_mode(partial, log_path=log_path, now=_NOW)

    assert current_mode(log_path) == Mode.SAFE
    assert not log_path.exists()


def test_switch_to_power_mode_denies_case_mismatched_confirmation(tmp_path: Path) -> None:
    """Case-sensitive: an otherwise-correct phrase typed in the wrong case is still refused."""
    log_path = tmp_path / "mode_log.jsonl"

    with pytest.raises(ModeSwitchDeniedError):
        switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION.upper(), log_path=log_path, now=_NOW)

    assert current_mode(log_path) == Mode.SAFE
    assert not log_path.exists()


def test_switch_to_power_mode_rejection_does_not_append_to_an_existing_log(tmp_path: Path) -> None:
    """A rejected attempt must leave a PRE-EXISTING log completely untouched -- not just "no new
    file created" (the case above), but "no new line added" when there is already history."""
    log_path = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path, now=_NOW)
    switch_to_safe_mode(log_path=log_path, now=_NOW + 1)
    lines_before = _read_lines(log_path)
    assert len(lines_before) == 2

    with pytest.raises(ModeSwitchDeniedError):
        switch_to_power_mode("wrong text entirely", log_path=log_path, now=_NOW + 2)

    lines_after = _read_lines(log_path)
    assert lines_after == lines_before  # byte-for-byte unchanged, nothing appended
    assert current_mode(log_path) == Mode.SAFE  # live mode also unchanged


def test_switch_to_power_mode_succeeds_with_exact_confirmation(tmp_path: Path) -> None:
    log_path = tmp_path / "mode_log.jsonl"

    entry = switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path, now=_NOW)

    assert entry.from_mode == Mode.SAFE
    assert entry.to_mode == Mode.POWER
    assert entry.changed_at == _NOW
    assert entry.confirmed is True
    assert current_mode(log_path) == Mode.POWER
    assert len(_read_lines(log_path)) == 1

    round_tripped = ModeChangeEntry.model_validate_json(_read_lines(log_path)[0])
    assert round_tripped == entry


# --- switch_to_safe_mode: never called by any existing test before this file -----------------


def test_switch_to_safe_mode_from_fresh_install_reverts_safe_to_safe_and_logs(
    tmp_path: Path,
) -> None:
    """Even with no prior history (already-SAFE fresh install), calling switch_to_safe_mode
    still logs a real entry -- it is not a no-op guarded behind "only if currently in power"."""
    log_path = tmp_path / "mode_log.jsonl"

    entry = switch_to_safe_mode(log_path=log_path, now=_NOW)

    assert entry.from_mode == Mode.SAFE
    assert entry.to_mode == Mode.SAFE
    assert entry.changed_at == _NOW
    assert entry.confirmed is True
    assert current_mode(log_path) == Mode.SAFE
    assert len(_read_lines(log_path)) == 1


def test_switch_to_safe_mode_reverts_from_power_and_logs_from_mode_correctly(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path, now=_NOW)
    assert current_mode(log_path) == Mode.POWER

    entry = switch_to_safe_mode(log_path=log_path, now=_NOW + 1)

    assert entry.from_mode == Mode.POWER
    assert entry.to_mode == Mode.SAFE
    assert entry.changed_at == _NOW + 1
    assert current_mode(log_path) == Mode.SAFE
    assert len(_read_lines(log_path)) == 2


def test_switch_to_safe_mode_requires_no_confirmation_argument() -> None:
    """switch_to_safe_mode's signature has no confirmation_text parameter at all -- becoming
    more conservative is never gated, by construction, not merely by an unused default."""
    import inspect

    params = inspect.signature(switch_to_safe_mode).parameters
    assert "confirmation_text" not in params


# --- Full round-trip: safe -> power -> safe, replaying the mode log --------------------------


def test_round_trip_safe_to_power_to_safe_replays_correctly_from_log(tmp_path: Path) -> None:
    log_path = tmp_path / "mode_log.jsonl"
    assert current_mode(log_path) == Mode.SAFE

    power_entry = switch_to_power_mode(
        REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path, now=_NOW
    )
    assert current_mode(log_path) == Mode.POWER

    safe_entry = switch_to_safe_mode(log_path=log_path, now=_NOW + 100)
    assert current_mode(log_path) == Mode.SAFE

    lines = _read_lines(log_path)
    assert len(lines) == 2
    replayed = [ModeChangeEntry.model_validate_json(line) for line in lines]
    assert replayed[0] == power_entry
    assert replayed[1] == safe_entry
    assert replayed[0].from_mode == Mode.SAFE
    assert replayed[0].to_mode == Mode.POWER
    assert replayed[1].from_mode == Mode.POWER
    assert replayed[1].to_mode == Mode.SAFE


# --- ADR-0027: schema versioning, backward compat (pre-this-ADR data) ------------------------


def test_backward_compat_pre_adr0027_line_with_no_schema_version_key_defaults_to_one(
    tmp_path: Path,
) -> None:
    """A mode-log line written before this ADR (no schema_version key at all) validates with
    schema_version defaulting to 1 -- the literal truth for that line."""
    log_path = tmp_path / "mode_log.jsonl"
    data = _mode_entry_dict()
    del data["schema_version"]
    log_path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    entry = ModeChangeEntry.model_validate_json(log_path.read_text(encoding="utf-8").strip())

    assert entry.schema_version == 1
    assert entry.to_mode == Mode.POWER


def test_backward_compat_current_mode_resolves_pre_adr0027_log(tmp_path: Path) -> None:
    """`current_mode`, the function on nearly every CLI command's load path, must resolve a
    pre-schema_version log exactly as before."""
    log_path = tmp_path / "mode_log.jsonl"
    data = _mode_entry_dict()
    del data["schema_version"]
    log_path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    assert current_mode(log_path) == Mode.POWER


# --- ADR-0027: schema versioning, forward compat (future-release data) ------------------------


def test_forward_compat_unknown_field_and_newer_schema_version_does_not_raise() -> None:
    """A line from a future release: an unrecognized field plus a schema_version higher than
    this code knows about. Must parse without raising and record the higher version."""
    data = _mode_entry_dict(schema_version=42, a_future_field="unseen by this code")

    entry = ModeChangeEntry.model_validate_json(json.dumps(data))

    assert entry.schema_version == 42
    assert entry.to_mode == Mode.POWER
    # extra="ignore": the unrecognized field is dropped, not preserved -- deliberate for this
    # class (see ADR-0027), since a ModeChangeEntry is never re-serialized after being read.
    assert entry.model_extra in (None, {})


def test_forward_compat_current_mode_does_not_raise_on_newer_schema_version(
    tmp_path: Path,
) -> None:
    """The actual bug this ADR fixes for mode.py, exercised through the real entry point:
    `current_mode` must never crash the entire CLI over one incompatible mode-log line."""
    log_path = tmp_path / "mode_log.jsonl"
    newer = _mode_entry_dict(schema_version=9, brand_new_field="from the future")
    log_path.write_text(json.dumps(newer) + "\n", encoding="utf-8")

    assert current_mode(log_path) == Mode.POWER


def test_current_mode_logs_warning_on_newer_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`current_mode` logs (never raises) when it sees a newer schema_version than this code
    knows about, once per call, naming every distinct newer version encountered."""
    import reclaim.mode as mode_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(mode_module, "logger", fake_logger)

    log_path = tmp_path / "mode_log.jsonl"
    lines = [
        _mode_entry_dict(schema_version=MODE_LOG_SCHEMA_VERSION, to_mode="safe"),
        _mode_entry_dict(schema_version=3, to_mode="power"),
    ]
    log_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    assert current_mode(log_path) == Mode.POWER
    assert len(fake_logger.warnings) == 1
    event, kwargs = fake_logger.warnings[0]
    assert event == "mode.log_newer_schema_version_detected"
    assert kwargs["encountered_schema_versions"] == [3]
    assert kwargs["known_schema_version"] == MODE_LOG_SCHEMA_VERSION


def test_current_mode_does_not_warn_when_no_newer_version_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.mode as mode_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(mode_module, "logger", fake_logger)

    log_path = tmp_path / "mode_log.jsonl"
    log_path.write_text(json.dumps(_mode_entry_dict()) + "\n", encoding="utf-8")

    current_mode(log_path)

    assert fake_logger.warnings == []


def test_current_shape_entry_round_trips_and_defaults_schema_version(tmp_path: Path) -> None:
    """A freshly-created entry (via `switch_to_power_mode`) carries the current schema_version by
    construction, and round-trips through the real log file unchanged."""
    log_path = tmp_path / "mode_log.jsonl"

    entry = switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=log_path, now=_NOW)

    assert entry.schema_version == MODE_LOG_SCHEMA_VERSION
    round_tripped = ModeChangeEntry.model_validate_json(_read_lines(log_path)[0])
    assert round_tripped == entry


def test_default_log_path_used_when_none_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both switch functions and current_mode fall back to DEFAULT_MODE_LOG_PATH when
    log_path=None -- proven here by chdir'ing into an isolated tmp_path and never passing
    log_path explicitly."""
    import reclaim.mode as mode_module

    monkeypatch.chdir(tmp_path)

    assert current_mode() == Mode.SAFE
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, now=_NOW)
    assert current_mode() == Mode.POWER
    assert (tmp_path / mode_module.DEFAULT_MODE_LOG_PATH).exists()
