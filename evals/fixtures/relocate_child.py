"""Child process for `evals/test_relocate_gate.py`'s real-kill case: starts a move, signals
once it is mid-copy, then waits to be killed. Never run by hand; it only touches the paths it
is given, which the test creates in its tmp dir.

usage: relocate_child.py SOURCE TARGET JOURNAL_DIR D_ROOT READY_FILE
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import reclaim.relocate as relocate
from reclaim.relocate import Policy


def main() -> None:
    source, target, journals, d_root, ready = sys.argv[1:6]
    d_key = relocate._key(d_root)
    # Same two-volume stand-in as the test: paths under D_ROOT read as another volume.
    relocate._volume_serial = lambda path: 2 if relocate._key(path).startswith(d_key) else 1  # type: ignore[assignment]
    copied = {"n": 0}

    def slow(name: str) -> None:
        if name == "copy_file":
            copied["n"] += 1
            if copied["n"] == 3:
                Path(ready).write_text("mid-copy", encoding="utf-8")
                time.sleep(120)  # killed here

    policy = Policy(exclusion_match=lambda _p: None, protected=lambda _p: False)
    relocate.execute_move(
        source, target, policy, Path(journals), free_bytes=lambda _p: 10**15, checkpoint=slow
    )


if __name__ == "__main__":
    main()
