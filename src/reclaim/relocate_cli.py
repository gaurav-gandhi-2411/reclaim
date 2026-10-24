"""`reclaim relocate ...`: the command line for `reclaim.relocate` (spec `docs/specs/relocate.md`).

Kept out of `cli.py`, which only wires the subparser in and dispatches here.

    reclaim relocate list [--target-drive D:] [--json]   candidates, read-only
    reclaim relocate plan SOURCE [--target T]            the dry run (renames and renames back)
    reclaim relocate move SOURCE [--target T] --yes      the move; without --yes it is the plan
    reclaim relocate status                              every move's journal
    reclaim relocate rollback ID                         puts the original back, at any stage
    reclaim relocate recover ID                          after a crash: finish or roll back
    reclaim relocate delete-old ID [--confirm] [--accept-missing]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from reclaim import relocate
from reclaim.app_paths import data_root
from reclaim.config import Config, load_config
from reclaim.drives import NoFixedDrivesFoundError, list_fixed_drives
from reclaim.elevation import ElevatedProcessError, assert_not_elevated
from reclaim.index_location import default_config_path, resolve_index_db

DEFAULT_JOURNAL_DIR = data_root() / "data" / "relocate"


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "relocate",
        help="Move a large folder to another drive and leave a junction at its old path, so "
        "programs still find it. Dry run first; the old copy is kept until you delete it.",
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    sub = parser.add_subparsers(dest="relocate_command", required=True)

    listing = sub.add_parser("list", help="Classify the usual candidates (read-only).")
    listing.add_argument("--target-drive", default=None, help="e.g. D: (default: most free)")
    listing.add_argument("--path", action="append", type=Path, default=[], help="Also check")
    listing.add_argument("--json", action="store_true", help="Machine-readable output")

    for name, text in (("plan", "Dry run one move."), ("move", "Move one folder.")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("source", type=Path)
        cmd.add_argument("--target", type=Path, default=None)
        cmd.add_argument("--target-drive", default=None)
        if name == "move":
            cmd.add_argument("--yes", action="store_true", help="Confirm after reading the plan")

    sub.add_parser("status", help="Every move and its stage.")
    for name, text in (
        ("rollback", "Put the original folder back."),
        ("recover", "After an interruption: finish the move or roll it back."),
        ("delete-old", "Delete the kept old copy after re-hashing every file."),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("move_id")
        if name == "rollback":
            cmd.add_argument(
                "--use-old-copy",
                action="store_true",
                help="Put back the copy from before the move instead of the moved folder as it is",
            )
        if name == "delete-old":
            cmd.add_argument("--confirm", action="store_true", help="Before the 7 days are up")
            cmd.add_argument("--accept-missing", action="store_true")


def _err(text: str) -> int:
    print(f"reclaim relocate: {text}", file=sys.stderr)  # noqa: T201
    return 1


def _out(text: str) -> None:
    print(text)  # noqa: T201


def _gib(n: int) -> str:
    return f"{n / 2**30:.2f} GiB"


def _policy(config: Config, config_path: Path, journal_dir: Path) -> relocate.Policy:
    app_dirs = [data_root() / "data", journal_dir]
    try:
        app_dirs.append(resolve_index_db(None, config_path).parent)
    except (OSError, ValueError) as exc:
        # Fail closed: the index folder must be on the must-stay list, so not knowing where it
        # is stops the command rather than leaving it movable.
        raise relocate.RelocateError(f"cannot find the index folder: {exc}") from exc
    return relocate.default_policy(config, environ=os.environ, app_dirs=app_dirs)


def _target_drive(requested: str | None, source: Path | None = None) -> Path | None:
    if requested:
        return Path(requested.rstrip("\\/") + "\\")
    try:
        drives = list_fixed_drives()
    except NoFixedDrivesFoundError:
        return None
    system = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    home = (source or system).anchor.casefold()
    others = [d for d in drives if str(d).casefold() != home]
    if not others:
        return None
    return max(others, key=lambda d: relocate._free_bytes(str(d)))


def _journal(journal_dir: Path, move_id: str) -> Path:
    path = journal_dir / f"relocate-{move_id}.json"
    if not path.is_file():
        raise relocate.RelocateError(f"no move with id {move_id} in {journal_dir}")
    return path


def _print_plan(plan: relocate.Plan) -> None:
    _out(f"source: {plan.source} ({plan.files} files, {_gib(plan.bytes)})")
    _out(f"target: {plan.target} (volume free: {_gib(plan.target_free)})")
    _out(f"the old copy would be kept at {plan.moved} until you delete it")
    if plan.ok:
        _out("dry run OK")
    for reason in plan.reasons:
        _out(f"  NOT MOVABLE: {reason}")


def run(args: argparse.Namespace) -> int:
    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        return _err(str(exc))
    config_path: Path = args.config
    try:
        config = load_config(config_path if config_path.exists() else None)
    except (ValueError, OSError) as exc:
        return _err(f"config.toml is invalid ({config_path}): {exc}")
    try:
        policy = _policy(config, config_path, args.journal_dir)
    except relocate.RelocateError as exc:
        return _err(str(exc))
    journal_dir: Path = args.journal_dir
    command: str = args.relocate_command
    try:
        handler: Callable[[], int] = {
            "list": lambda: _list(args, policy),
            "plan": lambda: _plan_or_move(args, policy, journal_dir, execute=False),
            "move": lambda: _plan_or_move(args, policy, journal_dir, execute=args.yes),
            "status": lambda: _status(journal_dir),
            "rollback": lambda: _report(
                relocate.rollback(
                    _journal(journal_dir, args.move_id), use_old_copy=args.use_old_copy
                )
            ),
            "recover": lambda: _report(relocate.recover(_journal(journal_dir, args.move_id))),
            "delete-old": lambda: _report(
                relocate.delete_old_copy(
                    _journal(journal_dir, args.move_id),
                    confirmed=args.confirm,
                    accept_missing=args.accept_missing,
                )
            ),
        }[command]
        return handler()
    except relocate.RelocateError as exc:
        return _err(str(exc))


def _list(args: argparse.Namespace, policy: relocate.Policy) -> int:
    drive = _target_drive(args.target_drive)
    paths = [*relocate.known_locations(os.environ), *args.path]
    found = relocate.propose_candidates(paths, policy, drive)
    if args.json:
        rows = [
            {
                "path": c.path,
                "verdict": c.classification.verdict.value,
                "bytes": c.classification.bytes,
                "files": c.classification.files,
                "reasons": list(c.classification.reasons),
                "link_target": c.classification.link_target,
                "suggested_target": c.suggested_target,
            }
            for c in found
        ]
        _out(json.dumps({"target_drive": str(drive) if drive else None, "candidates": rows}))
        return 0
    if not found:
        _out("no candidate folders exist on this machine")
        return 0
    _out(f"target drive: {drive or 'none found (no second fixed drive)'}")
    for c in found:
        cls = c.classification
        _out(f"{cls.verdict.value:13} {_gib(cls.bytes):>12}  {c.path}")
        for reason in cls.reasons:
            _out(f"{'':27}{reason}")
        if cls.verdict is relocate.Verdict.MOVABLE and c.suggested_target:
            _out(f"{'':27}-> {c.suggested_target}")
    return 0


def _plan_or_move(
    args: argparse.Namespace, policy: relocate.Policy, journal_dir: Path, *, execute: bool
) -> int:
    source: Path = args.source
    target: Path | None = args.target
    if target is None:
        drive = _target_drive(args.target_drive, source.absolute())
        if drive is None:
            return _err("no other fixed drive to move to; pass --target")
        target = Path(relocate.suggested_target(source, drive))
    plan = relocate.plan_move(source, target, policy, journal_dir=journal_dir)
    _print_plan(plan)
    if not plan.ok:
        return 1
    if not execute:
        if args.relocate_command == "move":
            _out("re-run with --yes to move it")
        return 0
    outcome = relocate.execute_move(source, target, policy, journal_dir)
    return _report(outcome)


def _status(journal_dir: Path) -> int:
    rows = relocate.journals(journal_dir)
    if not rows:
        _out("no moves recorded")
    for j in rows:
        _out(f"{j.id}  {j.stage:13} {_gib(j.bytes):>12}  {j.source} -> {j.target}")
    return 0


def _report(outcome: relocate.Outcome) -> int:
    j = outcome.journal
    _out(f"{j.id}: {outcome.message}")
    for step in j.history[-3:]:
        _out(f"  {step[1]}: {step[2]}")
    if outcome.kept:
        _out(f"  kept {outcome.kept[0]}: {outcome.kept[1]}")
    _out(f"journal: {j.path}")
    return 0
