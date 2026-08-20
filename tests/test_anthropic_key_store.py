from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.anthropic_key_store import (
    DpapiError,
    delete_key,
    has_key,
    load_key,
    protect,
    store_key,
    unprotect,
)

# Real DPAPI calls -- Windows crypto APIs only, no network, no real Anthropic API call. This
# project is Windows/NTFS-only by design (see scanner.py's own pytestmark), so exercising the
# real Win32 call here (rather than mocking `ctypes.windll`) is the right level of test.


def test_protect_unprotect_round_trips(tmp_path: Path) -> None:
    original = b"sk-ant-fake-key-for-testing-only"
    encrypted = protect(original)
    assert encrypted != original  # actually encrypted, not a pass-through
    assert unprotect(encrypted) == original


def test_protect_unprotect_round_trips_empty_bytes() -> None:
    assert unprotect(protect(b"")) == b""


def test_unprotect_rejects_garbage_bytes() -> None:
    with pytest.raises(DpapiError):
        unprotect(b"this is not a real DPAPI blob")


def test_store_load_delete_key_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "anthropic_key.bin"
    assert has_key(path) is False
    assert load_key(path) is None

    store_key("sk-ant-real-shaped-key-0123456789", path)
    assert has_key(path) is True
    assert path.exists()
    # The file on disk must never contain the plaintext key.
    assert b"sk-ant-real-shaped-key" not in path.read_bytes()
    assert load_key(path) == "sk-ant-real-shaped-key-0123456789"


def test_store_key_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "anthropic_key.bin"
    store_key("sk-ant-nested", path)
    assert load_key(path) == "sk-ant-nested"


def test_store_key_overwrites_an_existing_key(tmp_path: Path) -> None:
    path = tmp_path / "anthropic_key.bin"
    store_key("sk-ant-first", path)
    store_key("sk-ant-second", path)
    assert load_key(path) == "sk-ant-second"


def test_delete_key_is_a_no_op_when_nothing_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "anthropic_key.bin"
    delete_key(path)  # must not raise
    assert has_key(path) is False


def test_delete_key_removes_a_stored_key(tmp_path: Path) -> None:
    path = tmp_path / "anthropic_key.bin"
    store_key("sk-ant-to-delete", path)
    delete_key(path)
    assert has_key(path) is False
    assert load_key(path) is None


def test_has_key_is_false_for_an_empty_file(tmp_path: Path) -> None:
    """A zero-byte file (e.g. an interrupted write) must read as "not configured," never as a
    configured-but-empty key -- `load_key` treats it the same way (returns None, not "")."""
    path = tmp_path / "anthropic_key.bin"
    path.write_bytes(b"")
    assert has_key(path) is False
    assert load_key(path) is None
