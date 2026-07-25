from __future__ import annotations

# Standalone child-process harness for `tests/test_recovery.py`'s crash-safety proof.
#
# Not a pytest test file itself (no `test_*` functions) — invoked via `subprocess.run([sys.
# executable, __file__, config_path])` so it runs as a genuinely separate OS process. This is
# what makes `os._exit()` below a real simulated hard-crash rather than a caught Python
# exception: `os._exit()` bypasses every `finally:` block (including `apply_batch`/`restore_
# batch`/`purge_expired`'s own `manifest_fh.close()`), atexit handlers, and any buffered-but-
# not-yet-fsynced writes — the closest thing to `SIGKILL`/power-loss available cross-platform
# (Windows has no real `SIGKILL`). `sys.exit()`/`raise SystemExit` are deliberately never used
# for the crash itself: both are catchable and both still run `finally:` blocks, which would
# prove nothing about ADR-0026's actual crash-safety claim.
#
# Reads one JSON config file (path given as `sys.argv[1]`) describing which real production
# operation to run (`apply_batch`/`restore_batch`/`purge_expired`, called for real, against real
# on-disk fixture files) and exactly which item, at which checkpoint, should trigger the crash:
#
#     {
#         "operation": "apply" | "restore" | "purge",
#         "checkpoint": "after_intent_fsync" | "after_action_before_done_fsync",
#         "crash_index": <int, 0-based — the Nth intent-write / Nth real action for this run>,
#         "manifest_path": <str>,
#         "vault_dir": <str>,
#         "now": <float>,
#         "items": [{"path": <str>, "size_bytes": <int>}, ...]   # only for operation="apply"
#         "batch_id": <str>                                       # only for operation="restore"
#     }
#
# Exit codes (checked by the parent test, never left ambiguous with a genuine harness bug):
#   - `CRASH_EXIT_CODE` (9): the installed crash hook fired as intended — the real proof.
#   - `NO_CRASH_FIRED_EXIT_CODE` (2): the operation ran to completion without the crash hook
#     ever firing — always a harness/test-config bug (e.g. `crash_index` >= the number of real
#     actions taken), not a valid outcome to silently accept.
#   - anything else (typically `1`): an unhandled Python exception in the harness itself or the
#     production code under test — surfaced to the parent test via captured stderr for debugging.
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import reclaim.executor as executor_module
import reclaim.purge as purge_module
from reclaim.config import Config
from reclaim.models import Candidate, Tier, Verdict
from reclaim.safety import SafetyValidator

CRASH_EXIT_CODE = 9
NO_CRASH_FIRED_EXIT_CODE = 2


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


def _candidate(path: Path, *, size_bytes: int) -> Candidate:
    return Candidate(
        path=path,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        size_bytes=size_bytes,
        tier=Tier.A,
        rationale="crash-harness fixture",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="TEST_REASON",
        retention_days=30,
        size_guard_exempt=False,
        rebuildable=False,
    )


def _install_lock_sync_hooks(module: Any, lock_acquired_marker: str, proceed_marker: str) -> None:
    """Audit finding C10 (second pass), point 4 — test-only synchronization hook, opt-in via two
    optional config keys, used only by `tests/test_recovery.py`'s "second batch blocked on the
    lock during a hard crash" test.

    Wraps `_open_manifest_for_sync` (called exactly once, up front, by every one of `apply_batch`
    /`restore_batch`/`purge_expired` — see `executor.py`) so that the instant this process
    actually holds the manifest's OS-level lock, it (1) writes `lock_acquired_marker` to disk —
    real, observable proof from the PARENT test's perspective that the lock is genuinely held,
    not merely "probably held by now" — then (2) busy-waits, STILL HOLDING THE LOCK, until
    `proceed_marker` appears before letting `apply_batch` proceed into its per-item loop (which
    is where this scenario's crash then fires, per the existing `crash_index`/`checkpoint`
    machinery below). This is what lets the parent test start a REAL second lock-acquisition
    attempt and confirm it is genuinely blocked before killing this process — without it, timing
    between "process A holds the lock" and "process A crashes" would be a race the parent test
    couldn't observe deterministically.
    """
    original = module._open_manifest_for_sync

    def _wrapped(manifest_path: Path) -> Any:
        fh = original(manifest_path)
        Path(lock_acquired_marker).write_text("1", encoding="utf-8")
        while not Path(proceed_marker).exists():
            time.sleep(0.02)
        return fh

    module._open_manifest_for_sync = _wrapped


def _install_intent_crash(module: Any, crash_index: int) -> None:
    """Crashes immediately after the `crash_index`-th `phase="intent"` entry is appended and
    fsynced — i.e. durably before any filesystem action for that item has run. `done`-phase
    calls (already-completed prior items) pass through untouched and don't advance the
    counter."""
    original = module._append_and_sync
    counter = {"n": 0}

    def _wrapped(fh: Any, entry: Any) -> None:
        original(fh, entry)
        if entry.phase == "intent":
            idx = counter["n"]
            counter["n"] += 1
            if idx == crash_index:
                os._exit(CRASH_EXIT_CODE)

    module._append_and_sync = _wrapped


def _install_action_crash(module: Any, attr_name: str, crash_index: int) -> None:
    """Crashes immediately after the `crash_index`-th call to the real filesystem action
    (`_atomic_move` for apply/restore, `unlink_clear_readonly` for purge) returns successfully
    — the action has genuinely happened on disk, but control never returns to the caller, so
    the subsequent `phase="done"` `_append_and_sync` call never runs."""
    original = getattr(module, attr_name)
    counter = {"n": 0}

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        idx = counter["n"]
        counter["n"] += 1
        if idx == crash_index:
            os._exit(CRASH_EXIT_CODE)
        return result

    setattr(module, attr_name, _wrapped)


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    operation = config["operation"]
    checkpoint = config["checkpoint"]
    crash_index = int(config["crash_index"])
    manifest_path = Path(config["manifest_path"])
    vault_dir = Path(config["vault_dir"])
    now = float(config.get("now", 1_700_000_000.0))

    # `apply_batch`/`restore_batch` are defined in `executor.py` and resolve `_append_and_sync`/
    # `_atomic_move` from THEIR OWN module globals at call time — patching `executor_module`'s
    # attribute intercepts both. `purge_expired` (defined in `purge.py`) imports those same
    # names into ITS OWN module globals via `from reclaim.executor import ...` — a separate
    # binding pointing at the same original object — so intercepting purge's calls requires
    # patching `purge_module`'s own attribute instead; patching `executor_module` would have no
    # effect on `purge.py`'s calls.
    target_module = executor_module if operation in ("apply", "restore") else purge_module
    action_attr = "_atomic_move" if operation in ("apply", "restore") else "unlink_clear_readonly"

    # Audit finding C10 (second pass), point 4: optional, opt-in-only lock-acquired/proceed
    # synchronization — see `_install_lock_sync_hooks`'s docstring. Absent from every existing
    # crash-harness config (this repo's other tests never pass these keys), so this is a no-op
    # for all of them.
    lock_acquired_marker = config.get("lock_acquired_marker")
    proceed_marker = config.get("proceed_marker")
    if lock_acquired_marker and proceed_marker:
        _install_lock_sync_hooks(target_module, str(lock_acquired_marker), str(proceed_marker))

    if checkpoint == "after_intent_fsync":
        _install_intent_crash(target_module, crash_index)
    elif checkpoint == "after_action_before_done_fsync":
        _install_action_crash(target_module, action_attr, crash_index)
    else:
        raise ValueError(f"unknown checkpoint {checkpoint!r}")

    safety = _safety()

    if operation == "apply":
        candidates = [
            _candidate(Path(item["path"]), size_bytes=int(item["size_bytes"]))
            for item in config["items"]
        ]
        executor_module.apply_batch(
            candidates,
            safety=safety,
            apply=True,
            method="vault",
            vault_dir=vault_dir,
            manifest_path=manifest_path,
            now=now,
        )
    elif operation == "restore":
        executor_module.restore_batch(
            config["batch_id"],
            manifest_path=manifest_path,
            vault_dir=vault_dir,
            safety=safety,
            now=now,
        )
    elif operation == "purge":
        purge_module.purge_expired(
            apply=True,
            manifest_path=manifest_path,
            vault_dir=vault_dir,
            safety=safety,
            now=now,
        )
    else:
        raise ValueError(f"unknown operation {operation!r}")

    # Reached only if the crash hook never fired — a test-config bug, not a valid harness
    # outcome; the parent test must never silently treat this as "the crash happened".
    sys.exit(NO_CRASH_FIRED_EXIT_CODE)


if __name__ == "__main__":
    main()
