from __future__ import annotations

import time
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict

from reclaim.models import Mode

logger = structlog.get_logger(__name__)

# Append-only event log, same "fold to latest entry" pattern as executor.py's manifest.jsonl —
# the live mode is whatever `to_mode` the LAST entry recorded, never a value read from
# config.toml (a hand-edited config file must never be the thing that silently disables the
# safety boundary; only an explicit, logged, gated switch can).
DEFAULT_MODE_LOG_PATH = Path("data/mode_log.jsonl")

# Deliberately exact and specific — not "yes"/"I agree"/a checkbox. A user who cannot be
# bothered to type this sentence has not demonstrated understanding of what power mode does,
# and a typo means "not confirmed," never "close enough." Case-sensitive, no normalization.
REQUIRED_POWER_MODE_CONFIRMATION = "I understand this can permanently delete files"


class ModeSwitchDeniedError(RuntimeError):
    """Raised by `switch_to_power_mode` when the supplied confirmation text does not match
    `REQUIRED_POWER_MODE_CONFIRMATION` exactly. Nothing is appended to the log when this is
    raised — a rejected attempt leaves the live mode unchanged, and there is nothing to audit
    about a switch that never happened."""


class ModeChangeEntry(BaseModel):
    """One line in the append-only `data/mode_log.jsonl`. Mirrors
    `executor.QuarantineManifestEntry`'s event-log shape/rigor."""

    model_config = ConfigDict(extra="forbid")

    from_mode: Mode
    to_mode: Mode
    changed_at: float
    # Whether a valid typed confirmation gated this entry. Always True in practice — the only
    # two ways to append an entry are `switch_to_power_mode` (raises before appending anything
    # if the confirmation text doesn't match) and `switch_to_safe_mode` (no gate needed, reverting
    # to the conservative mode is never the dangerous direction) — recorded explicitly anyway so
    # the audit trail states the fact rather than requiring a reader to infer it from which
    # function must have been called.
    confirmed: bool


def current_mode(log_path: Path | None = None) -> Mode:
    """The live application mode: the most recent entry's `to_mode`, or `Mode.SAFE` if the log
    doesn't exist or is empty — SAFE is the honest default for an install that has never
    switched, not merely a fallback value."""
    resolved = log_path if log_path is not None else DEFAULT_MODE_LOG_PATH
    if not resolved.exists():
        return Mode.SAFE
    latest: ModeChangeEntry | None = None
    with resolved.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            latest = ModeChangeEntry.model_validate_json(stripped)
    return latest.to_mode if latest is not None else Mode.SAFE


def _append_mode_change(log_path: Path, entry: ModeChangeEntry) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(entry.model_dump_json())
        fh.write("\n")


def switch_to_power_mode(
    confirmation_text: str, *, log_path: Path | None = None, now: float | None = None
) -> ModeChangeEntry:
    """The only way this codebase ever transitions SAFE -> POWER. Requires `confirmation_text`
    to equal `REQUIRED_POWER_MODE_CONFIRMATION` exactly — raises `ModeSwitchDeniedError`
    (mode unchanged, nothing logged) otherwise. On success, appends a logged, timestamped
    `ModeChangeEntry` — the switch is deliberate, auditable, and reversible via
    `switch_to_safe_mode` at any time."""
    resolved = log_path if log_path is not None else DEFAULT_MODE_LOG_PATH
    if confirmation_text != REQUIRED_POWER_MODE_CONFIRMATION:
        raise ModeSwitchDeniedError(
            "power mode requires typing the exact confirmation phrase "
            f"{REQUIRED_POWER_MODE_CONFIRMATION!r} — refusing the switch, mode remains safe"
        )
    entry = ModeChangeEntry(
        from_mode=current_mode(resolved),
        to_mode=Mode.POWER,
        changed_at=now if now is not None else time.time(),
        confirmed=True,
    )
    _append_mode_change(resolved, entry)
    logger.info("mode.switched_to_power", changed_at=entry.changed_at)
    return entry


def switch_to_safe_mode(
    *, log_path: Path | None = None, now: float | None = None
) -> ModeChangeEntry:
    """Reverts to safe mode. No confirmation gate — becoming more conservative is never the
    dangerous direction, only becoming less conservative is."""
    resolved = log_path if log_path is not None else DEFAULT_MODE_LOG_PATH
    entry = ModeChangeEntry(
        from_mode=current_mode(resolved),
        to_mode=Mode.SAFE,
        changed_at=now if now is not None else time.time(),
        confirmed=True,
    )
    _append_mode_change(resolved, entry)
    logger.info("mode.switched_to_safe", changed_at=entry.changed_at)
    return entry
