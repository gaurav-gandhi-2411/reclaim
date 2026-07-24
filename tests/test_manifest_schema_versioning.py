"""ADR-0027: schema versioning for `QuarantineManifestEntry`.

Covers both directions explicitly:
- Backward compat: a manifest line written before this ADR (no `schema_version` key at all, and
  in the oldest case, none of ADR-0026's `phase`/`intent_id`/`operation` either) still parses and
  behaves exactly as before.
- Forward compat: a manifest line written by a *future* release (an unrecognized field, and/or a
  `schema_version` higher than this code knows about) never crashes `read_manifest_entries`, and
  survives a read-modify-write round trip (`model_copy` + `model_dump_json`, the exact pattern
  `restore_batch`/`purge_expired`/`reclaim.recovery` use) without losing the unrecognized field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reclaim.executor import (
    QUARANTINE_MANIFEST_SCHEMA_VERSION,
    QuarantineManifestEntry,
    append_manifest_entries,
    read_manifest_entries,
)
from reclaim.models import Tier

_NOW = 1_700_000_000.0


class _RecordingLogger:
    """Minimal stand-in for the module's `structlog` logger, recording `.warning(...)` calls so
    tests can assert on them without depending on structlog's stdlib-logging integration (which
    this project doesn't configure)."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:  # pragma: no cover - unused here
        pass


def _base_entry_dict(**overrides: Any) -> dict[str, Any]:
    """A complete, current-shape manifest line as a plain dict (not via the pydantic model),
    so backward/forward-compat tests can freely add/remove keys to simulate other versions."""
    data: dict[str, Any] = {
        "batch_id": "batch_test",
        "original_path": "C:/Users/gg/Downloads/old_installer.exe",
        "size_bytes": 1234,
        "is_dir": False,
        "category": "old_installer",
        "category_group": "old_installers",
        "rationale": "test rationale",
        "rebuild_instruction": "Re-download from the original source if needed again.",
        "tier": "A",
        "method": "vault",
        "vault_path": "data/quarantine/batch_test/abc_old_installer.exe",
        "retention_days": 30,
        "quarantined_at": _NOW,
        "retention_until": _NOW + 30 * 86400.0,
        "restored": False,
        "restored_at": None,
        "purged": False,
        "purged_at": None,
        "phase": "done",
        "intent_id": None,
        "operation": "apply",
        "schema_version": QUARANTINE_MANIFEST_SCHEMA_VERSION,
    }
    data.update(overrides)
    return data


# --- Backward compat: pre-this-ADR data, read by this code -----------------------------------


def test_backward_compat_pre_adr0027_line_with_no_schema_version_key_defaults_to_one() -> None:
    """A line written after ADR-0026 but before ADR-0027 (has phase/intent_id/operation, but no
    schema_version key at all) validates with schema_version defaulting to 1 -- the literal
    truth for that line, not an approximation."""
    data = _base_entry_dict()
    del data["schema_version"]

    entry = QuarantineManifestEntry.model_validate_json(json.dumps(data))

    assert entry.schema_version == 1
    assert entry.phase == "done"
    assert entry.intent_id is None
    assert entry.operation == "apply"


def test_backward_compat_pre_adr0026_line_with_no_phase_fields_still_parses() -> None:
    """The oldest possible shape: no phase/intent_id/operation/schema_version at all (predates
    ADR-0026 too). Every one of those fields must default sensibly and the line must fold as a
    completed ("done") entry, exactly as ADR-0026 already promised."""
    data = _base_entry_dict()
    for key in ("phase", "intent_id", "operation", "schema_version"):
        del data[key]

    entry = QuarantineManifestEntry.model_validate_json(json.dumps(data))

    assert entry.phase == "done"
    assert entry.intent_id is None
    assert entry.operation == "apply"
    assert entry.schema_version == 1
    assert entry.batch_id == "batch_test"


def test_backward_compat_read_manifest_entries_parses_pre_adr0027_file(tmp_path: Path) -> None:
    """A whole manifest.jsonl file written entirely in the pre-schema_version shape reads back
    with every entry defaulting schema_version to 1, via the real file-reading entry point."""
    manifest_path = tmp_path / "manifest.jsonl"
    data = _base_entry_dict()
    del data["schema_version"]
    manifest_path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    entries = read_manifest_entries(manifest_path)

    assert len(entries) == 1
    assert entries[0].schema_version == 1


# --- Forward compat: newer-than-this-code data, read by this code ----------------------------


def test_forward_compat_unknown_field_and_newer_schema_version_does_not_raise() -> None:
    """A line from a future release: an unrecognized field plus a schema_version higher than
    this code knows about. Must parse without raising, preserve the unknown field, and record
    the higher version -- never a hard crash."""
    data = _base_entry_dict(schema_version=99, a_future_field="something new")

    entry = QuarantineManifestEntry.model_validate_json(json.dumps(data))

    assert entry.schema_version == 99
    assert entry.model_extra == {"a_future_field": "something new"}


def test_forward_compat_unknown_field_survives_read_modify_write_round_trip() -> None:
    """The crux of ADR-0027: `extra='allow'` (not 'ignore') is what makes this pass. Every real
    reserialize in this codebase is exactly this shape -- read an entry, `model_copy(update=...)`
    it (e.g. closing out an intent to phase='done'/'aborted'), then `model_dump_json()` the
    result back to the manifest -- and the unrecognized field must not be silently dropped
    partway through."""
    data = _base_entry_dict(schema_version=2, future_flag=True, future_note="from tomorrow")
    entry = QuarantineManifestEntry.model_validate_json(json.dumps(data))

    # Simulate exactly what restore_batch/purge_expired/reclaim.recovery do: model_copy an
    # already-parsed entry, then re-serialize it.
    updated = entry.model_copy(update={"phase": "aborted"})
    round_tripped_json = updated.model_dump_json()
    round_tripped = QuarantineManifestEntry.model_validate_json(round_tripped_json)

    assert round_tripped.phase == "aborted"
    assert round_tripped.schema_version == 2
    assert round_tripped.model_extra == {"future_flag": True, "future_note": "from tomorrow"}
    assert '"future_flag":true' in round_tripped_json.replace(" ", "")
    assert '"future_note":"from tomorrow"' in round_tripped_json


def test_forward_compat_read_manifest_entries_does_not_raise_on_newer_schema_version(
    tmp_path: Path,
) -> None:
    """The actual bug this ADR fixes, exercised through the real file-reading entry point: a
    manifest containing one newer-schema-version line and one current-shape line must return
    both entries, never raise."""
    manifest_path = tmp_path / "manifest.jsonl"
    current = _base_entry_dict(batch_id="batch_current")
    newer = _base_entry_dict(
        batch_id="batch_newer", schema_version=5, brand_new_field="unseen by this code"
    )
    manifest_path.write_text(
        json.dumps(current) + "\n" + json.dumps(newer) + "\n", encoding="utf-8"
    )

    entries = read_manifest_entries(manifest_path)

    assert len(entries) == 2
    by_batch = {entry.batch_id: entry for entry in entries}
    assert by_batch["batch_current"].schema_version == QUARANTINE_MANIFEST_SCHEMA_VERSION
    assert by_batch["batch_newer"].schema_version == 5
    assert by_batch["batch_newer"].model_extra == {"brand_new_field": "unseen by this code"}


def test_read_manifest_entries_logs_warning_on_newer_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_manifest_entries` must log (not raise) when it sees a newer schema_version, once per
    call, naming every distinct newer version actually encountered."""
    import reclaim.executor as executor_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(executor_module, "logger", fake_logger)

    manifest_path = tmp_path / "manifest.jsonl"
    lines = [
        _base_entry_dict(batch_id="a", schema_version=QUARANTINE_MANIFEST_SCHEMA_VERSION),
        _base_entry_dict(batch_id="b", schema_version=7),
        _base_entry_dict(batch_id="c", schema_version=8),
    ]
    manifest_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    read_manifest_entries(manifest_path)

    assert len(fake_logger.warnings) == 1
    event, kwargs = fake_logger.warnings[0]
    assert event == "executor.manifest_newer_schema_version_detected"
    assert kwargs["encountered_schema_versions"] == [7, 8]
    assert kwargs["known_schema_version"] == QUARANTINE_MANIFEST_SCHEMA_VERSION


def test_read_manifest_entries_does_not_warn_when_no_newer_version_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No spurious warnings for a perfectly ordinary, current-schema manifest."""
    import reclaim.executor as executor_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(executor_module, "logger", fake_logger)

    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(json.dumps(_base_entry_dict()) + "\n", encoding="utf-8")

    read_manifest_entries(manifest_path)

    assert fake_logger.warnings == []


# --- Round trip via append_manifest_entries (the non-fsync public writer) --------------------


def test_current_shape_entry_round_trips_through_append_and_read(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    entry = QuarantineManifestEntry(
        batch_id="batch_rt",
        original_path=Path("C:/Users/gg/Downloads/thing.bin"),
        size_bytes=42,
        is_dir=False,
        category="test_category",
        category_group="test_group",
        rationale="test",
        rebuild_instruction=None,
        tier=Tier.A,
        method="vault",
        vault_path=Path("data/quarantine/batch_rt/thing.bin"),
        retention_days=30,
        quarantined_at=_NOW,
        retention_until=_NOW + 30 * 86400.0,
    )
    assert entry.schema_version == QUARANTINE_MANIFEST_SCHEMA_VERSION

    append_manifest_entries(manifest_path, [entry])
    entries = read_manifest_entries(manifest_path)

    assert len(entries) == 1
    assert entries[0] == entry
