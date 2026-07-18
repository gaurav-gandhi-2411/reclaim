from __future__ import annotations

import pytest

from reclaim.elevation import ElevatedProcessError, assert_not_elevated, is_elevated


def test_is_elevated_reflects_the_raw_admin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reclaim.elevation._raw_is_admin", lambda: True)
    assert is_elevated() is True

    monkeypatch.setattr("reclaim.elevation._raw_is_admin", lambda: False)
    assert is_elevated() is False


def test_is_elevated_fails_open_when_the_win32_call_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not-Windows / a broken ctypes call must not be mistaken for "elevated" — this tool is
    Windows/NTFS-only by design, so a failure to even ask the question means there's no real
    elevation concept to refuse against."""

    def _boom() -> bool:
        raise OSError("simulated: ctypes.windll unavailable")

    monkeypatch.setattr("reclaim.elevation._raw_is_admin", _boom)
    assert is_elevated() is False


def test_assert_not_elevated_raises_when_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reclaim.elevation.is_elevated", lambda: True)
    with pytest.raises(ElevatedProcessError, match="Administrator"):
        assert_not_elevated()


def test_assert_not_elevated_is_silent_when_not_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reclaim.elevation.is_elevated", lambda: False)
    assert_not_elevated()  # must not raise
