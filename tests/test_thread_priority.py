"""ADR-0040: the OS-priority seam. A recording fake stands in for kernel32."""

from __future__ import annotations

import pytest

from reclaim import thread_priority
from reclaim.thread_priority import BackgroundScope, background_mode


class _Recorder:
    def __init__(self, *, accept: bool = True, raises: bool = False) -> None:
        self.calls: list[bool] = []
        self.accept = accept
        self.raises = raises

    def __call__(self, enable: bool) -> bool:
        self.calls.append(enable)
        if self.raises:
            raise OSError("boom")
        return self.accept


def test_context_manager_enters_and_leaves() -> None:
    rec = _Recorder()
    with background_mode(rec):
        assert rec.calls == [True]
    assert rec.calls == [True, False]


def test_leaves_even_when_the_body_raises() -> None:
    rec = _Recorder()
    with pytest.raises(RuntimeError), background_mode(rec):
        raise RuntimeError("warm-up failed")
    assert rec.calls == [True, False]


def test_scope_leave_is_idempotent_so_promotion_then_finally_ends_once() -> None:
    rec = _Recorder()
    scope = BackgroundScope(rec)
    scope.enter()
    scope.leave()  # promotion
    scope.leave()  # the worker's finally
    assert rec.calls == [True, False]


def test_failed_enter_never_calls_end() -> None:
    rec = _Recorder(accept=False)
    with background_mode(rec):
        pass
    assert rec.calls == [True]


def test_a_raising_setter_is_swallowed() -> None:
    rec = _Recorder(raises=True)
    with background_mode(rec):
        pass  # must not raise
    assert rec.calls == [True]


def test_real_setter_never_raises_and_returns_a_bool() -> None:
    entered = thread_priority.windows_background_mode_setter(True)
    assert isinstance(entered, bool)
    if entered:
        assert thread_priority.windows_background_mode_setter(False) is True
