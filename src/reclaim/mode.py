from __future__ import annotations

import time
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from reclaim.models import Mode

logger = structlog.get_logger(__name__)

# ADR-0027 (schema versioning): the mode-log shape as of introducing `schema_version` — every
# field `ModeChangeEntry` has today. Bump whenever a field is added/removed/changed.
MODE_LOG_SCHEMA_VERSION = 1

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
    `executor.QuarantineManifestEntry`'s event-log shape/rigor.

    ADR-0027 (schema versioning): `extra="ignore"` (not `"allow"`) is deliberate here — unlike
    `QuarantineManifestEntry`, a `ModeChangeEntry` is never re-serialized after being read (no
    `model_copy` call on this class anywhere in the codebase; every entry is freshly constructed
    by `switch_to_power_mode`/`switch_to_safe_mode` and appended once, immutably), so there is no
    read-modify-write cycle for an unrecognized field to be silently dropped from — `"ignore"` is
    exactly as safe as `"allow"` for this class and simpler. `current_mode` never raises on an
    entry written by a newer release; it logs a warning if that entry's `schema_version` is newer
    than `MODE_LOG_SCHEMA_VERSION`, and otherwise resolves `to_mode` from every field it does
    know — critical because `current_mode` is on the load path of nearly every CLI command.
    """

    model_config = ConfigDict(extra="ignore")

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
    # ADR-0027: absent (pre-versioning) entries validate with this defaulting to `1` — the literal
    # truth, since `1` is the version every existing field on this class belongs to.
    schema_version: int = Field(default=MODE_LOG_SCHEMA_VERSION)


def current_mode(log_path: Path | None = None) -> Mode:
    """The live application mode: the most recent entry's `to_mode`, or `Mode.SAFE` if the log
    doesn't exist or is empty — SAFE is the honest default for an install that has never
    switched, not merely a fallback value.

    ADR-0027: never raises on a log line written by a newer release — `ModeChangeEntry`'s
    `extra="ignore"` already guarantees an unrecognized field parses fine; this additionally logs
    a warning (once per call, listing every newer version actually seen) rather than silently
    absorbing it. This function sits on the load path of nearly every CLI command, so it must
    never hard-crash the whole CLI over one incompatible mode-log line.
    """
    resolved = log_path if log_path is not None else DEFAULT_MODE_LOG_PATH
    if not resolved.exists():
        return Mode.SAFE
    latest: ModeChangeEntry | None = None
    newer_versions: set[int] = set()
    with resolved.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            latest = ModeChangeEntry.model_validate_json(stripped)
            if latest.schema_version > MODE_LOG_SCHEMA_VERSION:
                newer_versions.add(latest.schema_version)
    if newer_versions:
        logger.warning(
            "mode.log_newer_schema_version_detected",
            log_path=str(resolved),
            known_schema_version=MODE_LOG_SCHEMA_VERSION,
            encountered_schema_versions=sorted(newer_versions),
        )
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
