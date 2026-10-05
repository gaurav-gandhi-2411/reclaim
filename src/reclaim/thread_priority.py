"""Windows "background mode" for one thread (ADR-0040): lowers that thread's CPU, I/O and memory
priority so a long dedup warm-up does not compete with the user's foreground work.

`SetThreadPriority(GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)` only ever affects the
CALLING thread, and `THREAD_MODE_BACKGROUND_END` must be called from that same thread. The
setter is an injectable seam (`BackgroundModeSetter`) so tests record enter/exit without touching
the OS; off Windows (or when the call fails) it is a logged no-op -- a priority hint must never
break the work it is attached to.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import structlog

logger = structlog.get_logger(__name__)

THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
THREAD_MODE_BACKGROUND_END = 0x00020000

# `setter(True)` enters background mode, `setter(False)` leaves it; returns whether the OS
# accepted the change. Never raises.
BackgroundModeSetter = Callable[[bool], bool]


def windows_background_mode_setter(enable: bool) -> bool:
    """The real kernel32 call for the CURRENT thread. False (and a log line) on any failure."""
    if os.name != "nt":
        return False
    try:
        import ctypes  # Windows-only; kept out of module import time

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        flag = THREAD_MODE_BACKGROUND_BEGIN if enable else THREAD_MODE_BACKGROUND_END
        ok = bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), flag))
        if not ok:
            logger.warning(
                "thread_priority.set_failed",
                enable=enable,
                error=ctypes.get_last_error(),
            )
    except Exception as exc:  # a priority hint must never raise into the work
        logger.warning("thread_priority.set_error", enable=enable, error=str(exc))
        return False
    return ok


def set_background_mode(enable: bool, setter: BackgroundModeSetter | None = None) -> bool:
    """Calls `setter` (default: the real Windows one) and swallows any exception from it."""
    try:
        return (setter or windows_background_mode_setter)(enable)
    except Exception as exc:  # see module docstring
        logger.warning("thread_priority.setter_raised", enable=enable, error=str(exc))
        return False


class BackgroundScope:
    """Idempotent enter/leave of background mode for the thread that calls them (both MUST be
    called from that one thread). `leave()` after a successful `enter()` happens at most once, so
    a mid-run promotion followed by the final `finally` never double-ends the mode."""

    def __init__(self, setter: BackgroundModeSetter | None = None) -> None:
        self._setter = setter
        self.active = False

    def enter(self) -> None:
        if not self.active:
            self.active = set_background_mode(True, self._setter)

    def leave(self) -> None:
        if self.active:
            self.active = False
            set_background_mode(False, self._setter)


@contextmanager
def background_mode(setter: BackgroundModeSetter | None = None) -> Iterator[None]:
    """Enter background mode for the current thread; ALWAYS leave it in `finally`."""
    scope = BackgroundScope(setter)
    scope.enter()
    try:
        yield
    finally:
        scope.leave()
