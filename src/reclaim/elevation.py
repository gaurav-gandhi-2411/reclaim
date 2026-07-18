from __future__ import annotations

import ctypes


class ElevatedProcessError(RuntimeError):
    """Raised by `assert_not_elevated` when this process holds an elevated (Administrator)
    Windows token."""


def _raw_is_admin() -> bool:  # pragma: no cover -- real Win32 call; is_elevated's branches are
    # covered by monkeypatching this function directly (see test_elevation.py), not by
    # controlling the actual elevation state of the test process.
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def is_elevated() -> bool:
    """True if this process holds an elevated (Administrator) Windows token.

    Uses `shell32.IsUserAnAdmin` — the standard, documented Win32 check for "is THIS process's
    token elevated right now under UAC," distinct from "is the user a member of the
    Administrators group" (which a non-elevated process run by an admin user would still answer
    yes to, and isn't what matters here).

    `SafetyValidator`'s protected-root denial is a pattern match, not an OS permission check —
    but it isn't the only thing standing between this tool and `C:\\Windows`/`C:\\Program
    Files`: an ordinary (non-elevated) process's own filesystem permissions are a real,
    OS-enforced backstop too (a standard user's process simply cannot write to
    `C:\\Windows\\System32` even in the hypothetical case a `protected_roots` pattern somehow
    failed to match). Running elevated silently removes that backstop — this function is how
    `assert_not_elevated` refuses to run at all rather than relying on it having gone unnoticed.
    """
    try:
        return _raw_is_admin()
    except (AttributeError, OSError):
        # Not on Windows (no ctypes.windll), or the call itself failed. Fail *open* here is
        # correct, not a hole: this tool is Windows/NTFS-only by design (see scanner.py's own
        # pytestmark), so a failure to even ask the question means there's no real elevation
        # concept to refuse against — most commonly a non-Windows dev/test environment.
        return False


def assert_not_elevated() -> None:
    """Refuses to proceed if this process is elevated. Called at the entry point of every
    mutating CLI command (`apply`, `undo`, `purge`, `serve`/`dashboard`) — never for read-only
    `scan`, which touches nothing."""
    if is_elevated():
        raise ElevatedProcessError(
            "reclaim refuses to run elevated (as Administrator) — this tool moves and "
            "permanently deletes files, and an ordinary user's filesystem permissions are part "
            "of what keeps it from touching protected system paths even if SafetyValidator's "
            "own pattern rules somehow missed one. Restart this command from a normal "
            "(non-elevated) terminal."
        )
