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


def test_load_config_returns_defaults_when_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.toml"
    config = load_config(missing)
    assert config.schema_version == CONFIG_SCHEMA_VERSION
    assert config == Config()


def test_load_config_returns_defaults_when_path_is_none() -> None:
    assert load_config(None) == Config()


# --- Backward compat: pre-this-ADR config.toml, read by this code -----------------------------


def test_backward_compat_pre_adr0027_toml_with_no_schema_version_key_parses(
    tmp_path: Path,
) -> None:
    """A config.toml written before this ADR (no schema_version key anywhere) parses fine, with
    schema_version defaulting to 1 -- the literal truth for that file."""
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

    assert config.schema_version == 1
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
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
schema_version = 1
a_future_top_level_key = "something new"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.schema_version == 1


def test_forward_compat_unknown_category_level_key_does_not_raise(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[categories]
a_future_category = { enabled = true }

[categories.dev_artifacts]
enabled = true
a_future_field = "unseen by this code"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.categories.dev_artifacts.enabled is True


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


def test_load_config_logs_warning_on_unknown_top_level_and_category_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.config as config_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(config_module, "logger", fake_logger)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
a_future_top_level_key = "x"

[categories.dev_artifacts]
enabled = true
a_future_field = "y"
""",
        encoding="utf-8",
    )

    load_config(config_path)

    scopes = {kwargs["scope"]: kwargs["keys"] for _, kwargs in fake_logger.warnings}
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
