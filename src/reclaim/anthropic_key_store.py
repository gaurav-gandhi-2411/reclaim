from __future__ import annotations

import ctypes
from pathlib import Path

from reclaim.app_paths import data_root

# R2 (per-category LLM explainer): the ONLY place in this codebase that ever persists the
# user's Anthropic API key. Uses Windows DPAPI (`CryptProtectData`/`CryptUnprotectData`,
# current-user scope) via `ctypes` -- the exact same "touch a Windows-native API directly, no
# heavy dependency" precedent `elevation.py` already established for `IsUserAnAdmin` (see that
# module's docstring). DPAPI ties the encrypted blob to the current Windows user account: only a
# process running as the same OS user that encrypted it can ever decrypt it (no password, no
# extra secret to manage -- the user's own Windows login IS the key-encryption key), which is
# the right threat model for a single-user, localhost-only desktop tool (see `AppState`'s own
# docstring for that same framing applied to the rest of this app). This module never talks to
# the network and never imports `reclaim.executor`/`send2trash`.
#
# NEVER logged, NEVER exposed on any diagnostics/API response: `service.build_diagnostics`
# (`DiagnosticsResponse`) must never call anything in this module, and nothing here uses
# `structlog` at all -- there is no safe way to log a Crypt{Protect,Unprotect}Data failure
# without risking including a fragment of the plaintext key in a future edit, so the discipline
# is "never log from this module, ever," the same zero-logging posture `document_text.py`/
# `screenshot_ocr.py` already use for OCR'd/document text (see those modules' docstrings).

# CWD-independent (see reclaim.app_paths.data_root's docstring, and PR #51 for the original
# confirmed-live crash this class of bug caused elsewhere) -- not yet reachable from any
# working-directory-less invocation today, but "not reachable today" is a property of today's
# call sites, not of the code.
DEFAULT_KEY_PATH = data_root() / "data" / "anthropic_key.bin"

# CRYPTPROTECT_UI_FORBIDDEN: never show a Windows UI prompt, even on failure -- this call must
# always be non-interactive (it can run from a background thread / a headless CI-adjacent dev
# machine), and a silent failure that raises `DpapiError` is the correct behavior there, not a
# blocking dialog no one is watching for.
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiError(RuntimeError):
    """Raised when a Windows DPAPI Crypt{Protect,Unprotect}Data call itself fails (e.g. the
    encrypted blob was produced by a different Windows user account, or is corrupted)."""


class _DataBlob(ctypes.Structure):
    """`DATA_BLOB` (wincrypt.h) -- `{ DWORD cbData; BYTE *pbData; }`. `ctypes.wintypes` isn't
    imported for just `DWORD` here; `c_uint32` is the identical width and avoids importing a
    Windows-only submodule at module load time (this module already assumes Windows via
    `ctypes.windll`, so that's not a portability concern -- it's just one fewer import)."""

    _fields_ = (("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char)))


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    """Builds a `_DataBlob` pointing at a live `create_string_buffer` -- the buffer is returned
    alongside the blob so the caller keeps a reference to it for the duration of the Win32 call
    (ctypes does not keep the buffer alive on its own; letting it get garbage-collected before
    the call completes would be a use-after-free)."""
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _bytes_from_blob(blob: _DataBlob) -> bytes:
    if not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free_blob(blob: _DataBlob) -> None:
    """DPAPI allocates `pbData` with `LocalAlloc` internally; the caller owns freeing it via
    `LocalFree` once it's been copied out (`_bytes_from_blob`) -- see the `CryptProtectData`/
    `CryptUnprotectData` MSDN remarks on `pDataOut`."""
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(data: bytes) -> bytes:
    """Encrypts `data` with Windows DPAPI, current-user scope. Raises `DpapiError` on failure
    (never returns a falsy/empty result silently on failure -- see `_check_the_win32_return`
    equivalent inline below)."""
    in_blob, _keepalive = _blob_from_bytes(data)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,  # szDataDescr -- no human-readable description needed
        None,  # pOptionalEntropy -- current-user DPAPI scope alone is the right threat model here
        None,  # pvReserved -- must be NULL
        None,  # pPromptStruct -- forbidden by the UI_FORBIDDEN flag below regardless
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DpapiError(f"CryptProtectData failed: {ctypes.WinError()}")
    try:
        return _bytes_from_blob(out_blob)
    finally:
        _free_blob(out_blob)


def unprotect(data: bytes) -> bytes:
    """Decrypts a blob previously produced by `protect`. Raises `DpapiError` on failure (wrong
    Windows user account, corrupted/truncated blob, etc.) -- never returns partially-decrypted
    or garbage bytes silently."""
    in_blob, _keepalive = _blob_from_bytes(data)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DpapiError(f"CryptUnprotectData failed: {ctypes.WinError()}")
    try:
        return _bytes_from_blob(out_blob)
    finally:
        _free_blob(out_blob)


def store_key(api_key: str, path: Path = DEFAULT_KEY_PATH) -> None:
    """Encrypts `api_key` with DPAPI and writes the ciphertext blob to `path` (parent
    directories created if needed, matching `data/`'s existing convention -- see
    `logging_config.DEFAULT_LOG_PATH`/`mode.DEFAULT_MODE_LOG_PATH`). Overwrites any existing
    file at `path` -- this is the one write path (re-entering a key replaces the old one)."""
    encrypted = protect(api_key.encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypted)


def load_key(path: Path = DEFAULT_KEY_PATH) -> str | None:
    """Returns the decrypted key, or `None` if no key has been stored yet at `path` (the
    everyday "user hasn't configured AI features" case -- callers must treat this as a normal,
    expected state, never an error). Raises `DpapiError` if a file exists but can't be decrypted
    (e.g. it was produced under a different Windows user account) -- that IS an error, distinct
    from "nothing stored," and callers should surface it rather than silently treating it as
    "no key.\""""
    if not path.exists():
        return None
    encrypted = path.read_bytes()
    if not encrypted:
        return None
    return unprotect(encrypted).decode("utf-8")


def delete_key(path: Path = DEFAULT_KEY_PATH) -> None:
    """Removes the stored key, if any. A no-op (never raises) when nothing is stored -- mirrors
    this API's other idempotent "nothing to do" actions (see `routes.cancel_scan`'s docstring
    for the same convention elsewhere in this codebase)."""
    path.unlink(missing_ok=True)


def has_key(path: Path = DEFAULT_KEY_PATH) -> bool:
    """Cheap presence check with no decryption -- used by the Settings UI to show "configured"/
    "not configured" without ever touching the plaintext key."""
    return path.exists() and path.stat().st_size > 0
