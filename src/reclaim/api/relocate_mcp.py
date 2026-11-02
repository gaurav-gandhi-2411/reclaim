from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from reclaim import relocate
from reclaim.api import relocate_routes
from reclaim.api.state import AppState
from reclaim.app_paths import data_root

# The choke point `reclaim.mcp.server` reaches relocation through (docs/specs/assistant-mcp.md
# section 4a), the same role `service.mcp_execute_delete` plays for deletes: the MCP package never
# imports `reclaim.relocate` (a test enforces it). What this module offers an assistant is exactly
# plan + execute-an-approved-plan. It has no path in, no `delete_old_copy` and no `rollback`:
# undoing a move and deleting the kept old copy stay with the user (CLI or the dashboard page).
#
# The assistant names a folder and a drive only by opaque ids from `list_relocation_options`. An
# option id is a hash of one of `relocate.known_locations`' paths and resolves only if it matches
# one of those that exists now, so an id can never name an arbitrary folder (the same ids can be
# recomputed by anyone, so the lookup, not the secrecy, is the boundary).

_OPTION_ID_RE = re.compile(r"reloc-[0-9a-f]{16}")
_DRIVE_ID_RE = re.compile(r"drive-[0-9a-f]{12}")


class UnknownOptionError(ValueError):
    """The option or drive id is malformed (a path or drive letter was passed instead) or no longer
    names anything in the listing."""


class NotMovableError(RuntimeError):
    """The live plan refuses this move; `reasons` says why (an exclusion, a hardlink cache, a
    handle, no room, ...)."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True, slots=True)
class DriveOption:
    drive_id: str
    root: str
    free_bytes: int


@dataclass(frozen=True, slots=True)
class LiveRelocation:
    """One freshly re-planned move: what would run if it ran now, and the hash committing to it."""

    option_id: str
    drive_id: str
    plan: relocate.Plan
    plan_hash: str
    policy: relocate.Policy


def option_id_for(path: str) -> str:
    return "reloc-" + hashlib.sha256(relocate._key(path).encode()).hexdigest()[:16]


def drive_id_for(root: Path | str) -> str:
    return "drive-" + hashlib.sha256(relocate._key(root).encode()).hexdigest()[:12]


def list_drives(anchor: str) -> list[DriveOption]:
    """Fixed drives other than the one `anchor` is on, as the dashboard page offers them."""
    return [
        DriveOption(drive_id_for(d), str(d), relocate._free_bytes(str(d)))
        for d in relocate_routes._target_drives(anchor)
    ]


def rollback_hint(state: AppState) -> str:
    """The undo command as far as it is known before the move has an id."""
    base = _relocate_prefix(state)
    return f"{base} status (shows this move's id), then {base} rollback <id>"


def recover_hint(state: AppState) -> str:
    """What to run after a move that failed or was cut off part-way: the folder may sit between
    stages, and only `recover` (finish or roll back) puts it in a known state again."""
    base = _relocate_prefix(state)
    return (
        "The folder may be part-way through the move. Run "
        f"`{base} status` to see its stage and id, then `{base} recover <id>` to finish or "
        "roll it back; nothing else will run for this approval."
    )


def rollback_command(state: AppState, move_id: str) -> str:
    return f"{_relocate_prefix(state)} rollback {move_id}"


def _relocate_prefix(state: AppState) -> str:
    journals = relocate_routes.journal_dir(state)
    # The CLI's own default journal folder needs no flag; any other one must be named or the
    # command would look in the wrong place and report "no move with that id".
    flag = "" if journals == data_root() / "data" / "relocate" else f' --journal-dir "{journals}"'
    return f"reclaim relocate{flag}"


def resolve_relocation(
    state: AppState, option_id: str, drive_id: str, *, run_probe: bool
) -> LiveRelocation:
    """Re-plans a move from the two ids against the machine as it is NOW (never a cached plan).
    `run_probe=False` is the read-only dry run (the in-use probe renames the folder and back);
    execution passes True. Raises `UnknownOptionError` for an id that is malformed or no longer
    listed; a plan that refuses is returned with `plan.ok` False, not raised."""
    if not _OPTION_ID_RE.fullmatch(option_id) or not _DRIVE_ID_RE.fullmatch(drive_id):
        raise UnknownOptionError(
            "option_id and drive_id must be the ids list_relocation_options returned; a path or "
            "a drive letter is not accepted. Nothing was changed."
        )
    source = next(
        (
            p
            for p in relocate.known_locations(os.environ)
            if relocate._exists(relocate._norm(p)) and option_id_for(str(p)) == option_id
        ),
        None,
    )
    if source is None or not relocate_routes._in_home(source):
        raise UnknownOptionError(
            f"{option_id} is not in the current list_relocation_options (the folder may be gone). "
            "Call it again."
        )
    drive = next((d for d in list_drives(source.anchor) if d.drive_id == drive_id), None)
    if drive is None:
        raise UnknownOptionError(
            f"{drive_id} is not a drive list_relocation_options offers for this folder. "
            "Call it again."
        )
    policy = relocate_routes._policy(state)
    target = relocate.suggested_target(source, drive.root)
    plan = relocate.plan_move(
        source,
        target,
        policy,
        run_probe=run_probe,
        journal_dir=relocate_routes.journal_dir(state),
    )
    # Identity as well as size: a folder replaced by an equal-looking one is a different folder.
    figures = json.dumps(
        [
            option_id,
            drive_id,
            plan.source,
            plan.target,
            plan.moved,
            plan.files,
            plan.bytes,
            relocate._folder_id(plan.source),
            relocate._folder_dev(plan.source),
        ]
    )
    return LiveRelocation(
        option_id, drive_id, plan, hashlib.sha256(figures.encode()).hexdigest(), policy
    )


def execute_relocation(state: AppState, live: LiveRelocation) -> relocate.Outcome:
    """The existing move, unchanged (`relocate.execute_move` re-plans once more itself and keeps
    `<name>.reclaim-moved`). The caller already checked `live` against what the user approved."""
    return relocate.execute_move(
        live.plan.source, live.plan.target, live.policy, relocate_routes.journal_dir(state)
    )
