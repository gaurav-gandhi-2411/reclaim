"""ADR-0027: schema versioning for `config.Config` / `config.toml`.

No `tests/test_config.py` existed before this file (confirmed by grep: no other test file
exercises `load_config`/`load_effective_config` against a real `config.toml`). Covers both
directions:
- Backward compat: a `config.toml` written before this ADR (no `schema_version` key at all)
  still parses and behaves exactly as before.
- Forward compat: a `config.toml` with an unrecognized top-level or category-level key, and/or a
  `schema_version` higher than this code knows about, never crashes `load_config`/
  `load_effective_config` -- and is logged (not silently swallowed with zero signal).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
from pathlib import Path
from typing import Any

import pytest

from reclaim.config import CONFIG_SCHEMA_VERSION, Config, load_config, load_effective_config
from reclaim.models import Mode

_NOW = 1_700_000_000.0


class _RecordingLogger:
    """Minimal stand-in for `config.py`'s module-level `structlog` logger -- see the identical
    helper in `tests/test_manifest_schema_versioning.py` for why a hand-rolled recorder is used
    instead of `caplog`."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:  # pragma: no cover - unused here
        pass


# --- Baseline: a bare Config() carries the current schema_version by construction -------------


def test_bare_config_defaults_schema_version_to_current() -> None:
    assert Config().schema_version == CONFIG_SCHEMA_VERSION


# --- Update check: opt-in, off by default (see PRIVACY.md's "Updates" section) -----------------


def test_bare_config_defaults_update_check_to_disabled() -> None:
    assert Config().update_check.enabled is False


def test_config_toml_can_opt_into_update_check(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[update_check]
enabled = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.update_check.enabled is True


def test_load_config_returns_defaults_when_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.toml"
    config = load_config(missing)
    assert config.schema_version == CONFIG_SCHEMA_VERSION
    assert config == Config()


# --- Notifications (R5, 80%-threshold disk-space alert): opt-in, off by default ----------------


def test_bare_config_defaults_notifications_to_disabled() -> None:
    assert Config().notifications.enabled is False


def test_bare_config_notifications_defaults() -> None:
    notifications = Config().notifications
    assert notifications.disk_threshold_percent == 80.0
    assert notifications.renotify_after_hours == 24.0
    assert notifications.snooze_days == 7


def test_config_toml_can_opt_into_notifications(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[notifications]
enabled = true
disk_threshold_percent = 90.0
renotify_after_hours = 12.0
snooze_days = 3
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.notifications.enabled is True
    assert config.notifications.disk_threshold_percent == 90.0
    assert config.notifications.renotify_after_hours == 12.0
    assert config.notifications.snooze_days == 3


def test_load_config_returns_defaults_when_path_is_none() -> None:
    assert load_config(None) == Config()


# --- P0 fix (2026-08 audit, temp-sweep age-guard finding): min_temp_root_age_hours floor --------


def test_bare_config_defaults_temp_root_age_guard_to_seven_days() -> None:
    """Default matches this project's own Track A manual cleanup threshold (7 days) -- the
    shipped default had drifted more aggressive than what was manually judged safe."""
    assert Config().categories.temp_and_browser_caches.min_temp_root_age_hours == 24.0 * 7


def test_config_toml_can_override_temp_root_age_guard_above_the_floor(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[categories.temp_and_browser_caches]
min_temp_root_age_hours = 48.0
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.categories.temp_and_browser_caches.min_temp_root_age_hours == 48.0


def test_temp_root_age_guard_below_hard_floor_raises(tmp_path: Path) -> None:
    """A misconfigured value below the 24h hard floor is rejected outright at config-load time --
    never silently clamped -- so a value below the floor can never quietly defeat the guard
    against sweeping just-written temp files (installer mid-extraction, active download, running
    application scratch state)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[categories.temp_and_browser_caches]
min_temp_root_age_hours = 1.0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="min_temp_root_age_hours"):
        load_config(config_path)


def test_temp_root_age_guard_exactly_at_hard_floor_is_accepted(tmp_path: Path) -> None:
    """The floor itself (24.0) is inclusive, not exclusive."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[categories.temp_and_browser_caches]
min_temp_root_age_hours = 24.0
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.categories.temp_and_browser_caches.min_temp_root_age_hours == 24.0


# --- Backward compat: pre-this-ADR config.toml, read by this code -----------------------------


def test_backward_compat_pre_adr0027_toml_with_no_schema_version_key_parses(
    tmp_path: Path,
) -> None:
    """A config.toml written before this ADR (no schema_version key anywhere) parses fine, with
    schema_version defaulting to CONFIG_SCHEMA_VERSION -- `Config` deliberately couples its field
    default to the current constant rather than a frozen historical literal (unlike
    `QuarantineManifestEntry`, which decouples -- see ADR-0027 for why the two models differ:
    `Config` is never re-serialized back to config.toml, so there is no stale-version-mislabeling
    risk from treating an unversioned file as "current")."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[safety]
deny = ["C:/protected/*"]

[categories.dev_artifacts]
enabled = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.schema_version == CONFIG_SCHEMA_VERSION
    assert config.safety.deny == ["C:/protected/*"]
    assert config.categories.dev_artifacts.enabled is True


def test_backward_compat_load_effective_config_still_applies_safe_mode_overrides(
    tmp_path: Path,
) -> None:
    """A pre-schema_version config.toml, read through the real end-user entry point
    (`load_effective_config`), still gets the safe-mode category overrides layered on top exactly
    as before -- this ADR must not change that policy."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[categories.dev_artifacts]
enabled = true
""",
        encoding="utf-8",
    )

    config = load_effective_config(config_path, mode=Mode.SAFE)

    assert config.mode == Mode.SAFE
    # dev_artifacts is one of SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS -- forced off regardless of
    # what config.toml requested.
    assert config.categories.dev_artifacts.enabled is False


# --- Forward compat: newer-than-this-code config.toml, read by this code ----------------------


def test_forward_compat_unknown_top_level_key_does_not_raise(tmp_path: Path) -> None:
    """Tolerance for an unrecognized key is gated on the file HONESTLY claiming to be from a
    newer release (`schema_version` genuinely > `CONFIG_SCHEMA_VERSION`) -- `schema_version ==
    CONFIG_SCHEMA_VERSION` (the CURRENT version) alongside an unrecognized key is exactly the
    ambiguous case `test_unknown_top_level_key_with_no_newer_schema_version_claim_raises` below
    proves gets rejected instead, so this test uses a real future version
    (`CONFIG_SCHEMA_VERSION + 1`) to demonstrate genuine forward compat, not the security-boundary
    case."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
schema_version = {CONFIG_SCHEMA_VERSION + 1}
a_future_top_level_key = "something new"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.schema_version == CONFIG_SCHEMA_VERSION + 1


def test_forward_compat_unknown_category_level_key_does_not_raise(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
schema_version = {CONFIG_SCHEMA_VERSION + 1}

[categories]
a_future_category = {{ enabled = true }}

[categories.dev_artifacts]
enabled = true
a_future_field = "unseen by this code"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.categories.dev_artifacts.enabled is True


# --- Security boundary: an unrecognized key with NO newer-schema-version claim is rejected -----
#
# This is the case a first version of ADR-0027 got wrong: tolerating EVERY unrecognized key
# unconditionally (regardless of whether the file claimed to be from a newer release) broke
# evals/test_ai_safety_gate.py's adversarial requirement that a hand-edited config.toml must
# never be able to smuggle an `ai_`-named category or field into the deterministic pipeline just
# by being unrecognized. Forward compat is scoped to ONLY the case it's actually meant for.


def test_unknown_top_level_key_with_no_newer_schema_version_claim_raises(tmp_path: Path) -> None:
    from reclaim.config import UnknownConfigKeyError

    config_path = tmp_path / "config.toml"
    config_path.write_text('a_typo_or_attack_key = "x"\n', encoding="utf-8")

    with pytest.raises(UnknownConfigKeyError, match="a_typo_or_attack_key"):
        load_config(config_path)


def test_unknown_category_field_with_schema_version_equal_to_current_raises(
    tmp_path: Path,
) -> None:
    """The exact ambiguous case: `schema_version` present but equal to (not greater than)
    `CONFIG_SCHEMA_VERSION` -- not a real forward-compat claim, so an unrecognized field still
    raises, same as if `schema_version` were absent entirely."""
    from reclaim.config import UnknownConfigKeyError

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
schema_version = {CONFIG_SCHEMA_VERSION}

[categories.dev_artifacts]
enabled = true
ai_source = "near_identical_image"
""",
        encoding="utf-8",
    )

    with pytest.raises(UnknownConfigKeyError, match="ai_source"):
        load_config(config_path)


def test_forward_compat_newer_schema_version_does_not_raise(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("schema_version = 99\n", encoding="utf-8")

    config = load_config(config_path)

    assert config.schema_version == 99


def test_forward_compat_load_effective_config_does_not_raise_on_newer_config(
    tmp_path: Path,
) -> None:
    """The actual bug this ADR fixes for config.py, exercised through the real end-user entry
    point: a config.toml from a future release must never crash the CLI/dashboard."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
schema_version = 42
a_future_top_level_key = "unseen by this code"

[categories.dev_artifacts]
enabled = true
a_future_field = "also unseen"
""",
        encoding="utf-8",
    )

    config = load_effective_config(config_path, mode=Mode.POWER)

    assert config.schema_version == 42
    assert config.categories.dev_artifacts.enabled is True


# --- Warning-log behavior (never raises, but not silently absorbed either) --------------------


def test_load_config_logs_warning_on_newer_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.config as config_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(config_module, "logger", fake_logger)

    config_path = tmp_path / "config.toml"
    config_path.write_text("schema_version = 7\n", encoding="utf-8")

    load_config(config_path)

    version_warnings = [
        w for w in fake_logger.warnings if w[0] == "config.newer_schema_version_detected"
    ]
    assert len(version_warnings) == 1
    _, kwargs = version_warnings[0]
    assert kwargs["encountered_schema_version"] == 7
    assert kwargs["known_schema_version"] == CONFIG_SCHEMA_VERSION


# --- P0-2 fix (2026-08 audit): persisting a category toggle from the in-app Settings tab -------
#
# `set_category_enabled`/`_set_category_enabled_in_toml_text` back `POST
# /api/settings/categories/{group}` -- a narrow, targeted text patch (no TOML-writer dependency,
# see config.py's module comment) that must round-trip through `load_config` afterward, not just
# produce syntactically-plausible-looking text.


def test_set_category_enabled_flips_existing_enabled_line(tmp_path: Path) -> None:
    from reclaim.config import set_category_enabled

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[safety]
deny = ["C:/protected/*"]

[categories.dev_artifacts]
enabled = true
# a user comment that must survive
retention_days = 30

[categories.old_installers]
enabled = false
""",
        encoding="utf-8",
    )

    set_category_enabled(config_path, "dev_artifacts", enabled=False)

    text = config_path.read_text(encoding="utf-8")
    assert "# a user comment that must survive" in text
    assert 'deny = ["C:/protected/*"]' in text

    config = load_config(config_path)
    assert config.categories.dev_artifacts.enabled is False
    # Untouched category/section stay exactly as they were.
    assert config.categories.old_installers.enabled is False
    assert config.safety.deny == ["C:/protected/*"]


def test_set_category_enabled_inserts_missing_enabled_line(tmp_path: Path) -> None:
    from reclaim.config import set_category_enabled

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[categories.dev_artifacts]
retention_days = 30
""",
        encoding="utf-8",
    )

    set_category_enabled(config_path, "dev_artifacts", enabled=True)

    config = load_config(config_path)
    assert config.categories.dev_artifacts.enabled is True
    assert config.categories.dev_artifacts.retention_days == 30


def test_set_category_enabled_appends_new_section_when_absent(tmp_path: Path) -> None:
    from reclaim.config import set_category_enabled

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[safety]
deny = ["C:/protected/*"]
""",
        encoding="utf-8",
    )

    set_category_enabled(config_path, "old_installers", enabled=True)

    config = load_config(config_path)
    assert config.categories.old_installers.enabled is True
    assert config.safety.deny == ["C:/protected/*"]


def test_set_category_enabled_creates_file_when_missing(tmp_path: Path) -> None:
    from reclaim.config import set_category_enabled

    config_path = tmp_path / "config.toml"
    assert not config_path.exists()

    set_category_enabled(config_path, "crash_dumps", enabled=False)

    assert config_path.exists()
    config = load_config(config_path)
    assert config.categories.crash_dumps.enabled is False


def test_set_category_enabled_rejects_unknown_category(tmp_path: Path) -> None:
    from reclaim.config import set_category_enabled

    config_path = tmp_path / "config.toml"
    with pytest.raises(ValueError, match="not_a_real_category"):
        set_category_enabled(config_path, "not_a_real_category", enabled=True)
    assert not config_path.exists()


def test_load_config_logs_warning_on_unknown_top_level_and_category_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only reached in the tolerate-branch (a genuine newer-schema-version claim) -- see
    `test_unknown_top_level_key_with_no_newer_schema_version_claim_raises` for the other branch,
    where an unrecognized key raises instead of logging."""
    import reclaim.config as config_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(config_module, "logger", fake_logger)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
schema_version = {CONFIG_SCHEMA_VERSION + 1}
a_future_top_level_key = "x"

[categories.dev_artifacts]
enabled = true
a_future_field = "y"
""",
        encoding="utf-8",
    )

    load_config(config_path)

    # schema_version=CONFIG_SCHEMA_VERSION + 1 (> CONFIG_SCHEMA_VERSION) also logs its own
    # "config.newer_schema_version_detected" warning (no "scope" key) -- filter to just the
    # unknown-keys ones this test cares about.
    key_warnings = [
        (event, kwargs)
        for event, kwargs in fake_logger.warnings
        if event == "config.unknown_keys_ignored"
    ]
    scopes = {kwargs["scope"]: kwargs["keys"] for _, kwargs in key_warnings}
    assert scopes["top_level"] == ["a_future_top_level_key"]
    assert scopes["categories.dev_artifacts"] == ["a_future_field"]


def test_load_config_does_not_warn_for_a_perfectly_ordinary_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.config as config_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(config_module, "logger", fake_logger)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[categories.dev_artifacts]
enabled = true
""",
        encoding="utf-8",
    )

    load_config(config_path)

    assert fake_logger.warnings == []


# --- P0 fix (2026-08 session, live-reproduced): `_win_path` resolves 8.3 short-name env vars ---
#
# Bug: on any account whose profile path is long enough to trigger Windows' 8.3 short-name (DOS
# alias) generation, `os.environ.get("TEMP")` (and, in principle, any other env var `_win_path`
# resolves) can come back already in short form -- confirmed live this session on a real machine
# with a 16-character username, where `%TEMP%` resolved to `C:\Users\RECLAI~1\AppData\Local\Temp`
# while `%USERPROFILE%`/`%LOCALAPPDATA%` correctly resolved long-form on the SAME account. The
# scanner always indexes real long-form paths (`FindFirstFileW` never returns 8.3 names), so a
# `temp_and_browser_caches` detector pattern built from an unresolved short-form value could
# structurally never match anything in the index -- `detect_temp_and_browser_caches` silently
# proposed zero `windows_temp` candidates, permanently, for any affected user. See
# `config._resolve_long_path`'s own docstring for the fix, and
# `tests/test_detectors.py::test_windows_temp_candidates_proposed_when_temp_env_var_was_short_form`
# for the full end-to-end (env var -> detector candidates) regression proof.


def _win32_get_short_path_name(long_path: str) -> str:
    """Test-only helper: a REAL `GetShortPathNameW` round trip, producing a genuine 8.3 short-name
    alias for `long_path` -- the same Win32 mechanism that produced the real-world short `%TEMP%`
    value this fix addresses, rather than a hand-authored fake short-name string. Callers must
    handle the case where 8.3 short-name generation is disabled for the target volume (the NTFS
    `NtfsDisable8dot3NameCreation` setting) -- in that case this returns `long_path` unchanged,
    with no `~` in it, and the caller should `pytest.skip`."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.restype = ctypes.wintypes.DWORD
    kernel32.GetShortPathNameW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPWSTR,
        ctypes.wintypes.DWORD,
    ]
    buf = ctypes.create_unicode_buffer(260)
    length = kernel32.GetShortPathNameW(long_path, buf, 260)
    assert length != 0, f"GetShortPathNameW failed for {long_path!r}"
    return buf.value


@pytest.mark.skipif(os.name != "nt", reason="Win32-only: GetLongPathNameW/GetShortPathNameW")
def test_resolve_long_path_round_trips_a_real_short_name_alias(tmp_path: Path) -> None:
    """End-to-end proof (preferred per this fix's own spec, over a mocked `GetLongPathNameW`
    return value): create a real directory with a long name, obtain its REAL short-name alias via
    a genuine Win32 round trip, then confirm `_resolve_long_path` converts that real short alias
    back to the real long form -- the exact mechanism the confirmed-live bug depended on."""
    from reclaim.config import _resolve_long_path

    long_dir = tmp_path / "a_genuinely_long_directory_name_for_8dot3_shortname_generation"
    long_dir.mkdir()

    short_path = _win32_get_short_path_name(str(long_dir))
    if "~" not in Path(short_path).name:
        pytest.skip(
            "8.3 short-name generation is disabled for this volume "
            "(NtfsDisable8dot3NameCreation) -- GetShortPathNameW returned the long form "
            "unchanged, so this environment cannot exercise the real short-form round trip."
        )

    resolved = _resolve_long_path(short_path)

    assert Path(resolved).resolve() == long_dir.resolve()
    assert "~" not in Path(resolved).name


@pytest.mark.skipif(os.name != "nt", reason="Win32-only: GetLongPathNameW")
def test_resolve_long_path_falls_back_to_original_on_nonexistent_path() -> None:
    """Fail-safe contract: a path that doesn't exist yet (a plausible state for an env-derived
    root before the first real scan/apply touches it) returns unchanged, never raises."""
    from reclaim.config import _resolve_long_path

    missing = "C:/this/path/genuinely/does/not/exist/on/this/machine/9f3a7c"
    assert _resolve_long_path(missing) == missing


def test_resolve_long_path_falls_back_when_win32_call_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-safe contract for the non-Windows/no-`ctypes.WinDLL` case -- same posture
    `elevation.is_elevated` documents for its own equivalent probe. Runs on every platform
    (doesn't need `os.name == "nt"`) since it only exercises the fallback branch."""
    import reclaim.config as config_module

    def _raise_oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated: not on Windows")

    monkeypatch.setattr(config_module.ctypes, "WinDLL", _raise_oserror, raising=False)

    assert config_module._resolve_long_path("C:/anything") == "C:/anything"


@pytest.mark.skipif(os.name != "nt", reason="Win32-only: GetLongPathNameW/GetShortPathNameW")
def test_win_path_resolves_a_real_short_form_env_var_to_long_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through `_win_path` itself -- what `_default_temp_roots`/
    `_default_package_cache_paths`/etc. actually call -- proving the fix at the real call site,
    not just the helper in isolation."""
    from reclaim.config import _win_path

    long_dir = tmp_path / "another_genuinely_long_directory_name_for_this_env_var_test"
    long_dir.mkdir()
    short_path = _win32_get_short_path_name(str(long_dir))
    if "~" not in Path(short_path).name:
        pytest.skip(
            "8.3 short-name generation is disabled for this volume -- see the sibling test above."
        )

    monkeypatch.setenv("RECLAIM_TEST_LONG_PATH_VAR", short_path)

    resolved = _win_path("RECLAIM_TEST_LONG_PATH_VAR", "C:/fallback")

    assert Path(resolved).resolve() == long_dir.resolve()
    assert "~" not in resolved
