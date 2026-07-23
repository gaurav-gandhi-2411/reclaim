from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.mode import (
    REQUIRED_POWER_MODE_CONFIRMATION,
    ModeChangeEntry,
    ModeSwitchDeniedError,
    current_mode,
    switch_to_power_mode,
    switch_to_safe_mode,
)
from reclaim.models import Mode

_NOW = 1_700_000_000.0


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
