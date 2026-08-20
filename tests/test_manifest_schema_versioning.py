"""ADR-0027: schema versioning for `QuarantineManifestEntry`.

Covers both directions explicitly:
- Backward compat: a manifest line written before this ADR (no `schema_version` key at all, and
  in the oldest case, none of ADR-0026's `phase`/`intent_id`/`operation` either) still parses and
  behaves exactly as before.
- Forward compat: a manifest line written by a *future* release (an unrecognized field, and/or a
  `schema_version` higher than this code knows about) never crashes `read_manifest_entries`, and
  survives a read-modify-write round trip (`model_copy` + `model_dump_json`, the exact pattern
  `restore_batch`/`purge_expired`/`reclaim.recovery` use) without losing the unrecognized field.
- Schema v2 (audit P0-4, 2026-08-20): the backward-compat gap ADR-0027 left open — a manifest
  line even OLDER than "version 1" (missing `is_dir`/`rebuild_instruction`/`retention_days`,
  required-with-no-default fields that predate ADR-0026/ADR-0027) — including an end-to-end
  proof against `data/quarantine/manifest.jsonl`'s real captured pre-upgrade content
  (`tests/fixtures/quarantine_manifest_pre_p0_4_2026_07_13.jsonl`), not just a synthetic fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from reclaim.api import security
from reclaim.api.app import create_app
from reclaim.config import Config
from reclaim.executor import (
    QUARANTINE_MANIFEST_SCHEMA_VERSION,
    QuarantineManifestEntry,
    append_manifest_entries,
    read_manifest_entries,
)
from reclaim.models import Tier
from reclaim.recovery import compute_reconciliation

_NOW = 1_700_000_000.0
_REAL_PRE_UPGRADE_MANIFEST_FIXTURE = (
    Path(__file__).parent / "fixtures" / "quarantine_manifest_pre_p0_4_2026_07_13.jsonl"
)
_TEST_HOST = "127.0.0.1"
_TEST_PORT = 8421


class _RecordingLogger:
    """Minimal stand-in for the module's `structlog` logger, recording both `.warning(...)` and
    `.info(...)` calls (schema v2, audit P0-4, added `.info` recording for the new per-entry
    migration events) into the same list so tests can assert on either without depending on
    structlog's stdlib-logging integration (which this project doesn't configure). Kept as one
    list rather than splitting by level -- no test in this file needs to distinguish level, only
    event name and kwargs."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))


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
    """Predates ADR-0026 too: no phase/intent_id/operation/schema_version at all, though this
    line still has ADR-0001's is_dir/rebuild_instruction/retention_days (this was, in practice,
    required for the line to parse before schema v2's P0-4 fix -- see the "schema v2" tests below
    for the genuinely oldest shape, which drops those three as well). Every one of the fields
    tested here must default sensibly and the line must fold as a completed ("done") entry,
    exactly as ADR-0026 already promised."""
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


# --- Schema v2 (audit P0-4): even older data, missing is_dir/rebuild_instruction/retention_days -


def test_schema_v2_line_missing_is_dir_rebuild_instruction_retention_days_gets_safe_defaults() -> (
    None
):
    """The genuinely oldest possible shape: predates ADR-0001's is_dir/rebuild_instruction/
    retention_days fields (which had no default until this fix), on top of also predating
    ADR-0026/ADR-0027. Must parse without raising and get the documented safe defaults -- see
    each field's own comment on `QuarantineManifestEntry` for why `False`/`None`/`None` are the
    correct, non-destructive choices."""
    data = _base_entry_dict()
    for key in ("is_dir", "rebuild_instruction", "retention_days", "schema_version"):
        del data[key]

    entry = QuarantineManifestEntry.model_validate_json(json.dumps(data))

    assert entry.is_dir is False
    assert entry.rebuild_instruction is None
    assert entry.retention_days is None
    assert entry.schema_version == 1
    assert entry.batch_id == "batch_test"


def test_schema_v2_read_manifest_entries_logs_migration_event_for_older_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defaulting above must be explicit and loggable, not silent (per the audit P0-4 brief):
    `read_manifest_entries` logs one `.info` event per migrated entry naming exactly which fields
    were missing and what they were defaulted to, plus one summary event for the whole read."""
    import reclaim.executor as executor_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(executor_module, "logger", fake_logger)

    manifest_path = tmp_path / "manifest.jsonl"
    data = _base_entry_dict(batch_id="batch_legacy")
    for key in ("is_dir", "rebuild_instruction", "retention_days", "schema_version"):
        del data[key]
    manifest_path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    entries = read_manifest_entries(manifest_path)

    assert len(entries) == 1
    migration_events = [
        (event, kwargs)
        for event, kwargs in fake_logger.warnings
        if event == "executor.manifest_entry_migrated_from_older_schema"
    ]
    assert len(migration_events) == 1
    _, kwargs = migration_events[0]
    assert kwargs["batch_id"] == "batch_legacy"
    assert set(kwargs["missing_fields"]) == {"is_dir", "rebuild_instruction", "retention_days"}
    assert kwargs["defaults_applied"] == {
        "is_dir": False,
        "rebuild_instruction": None,
        "retention_days": None,
    }
    assert kwargs["recorded_schema_version"] == 1
    assert kwargs["current_schema_version"] == QUARANTINE_MANIFEST_SCHEMA_VERSION

    summary_events = [
        (event, kwargs)
        for event, kwargs in fake_logger.warnings
        if event == "executor.manifest_migration_summary"
    ]
    assert len(summary_events) == 1
    assert summary_events[0][1]["migrated_entry_count"] == 1
    assert summary_events[0][1]["total_entry_count"] == 1


def test_schema_v2_no_migration_event_for_current_shape_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No spurious migration logging for an ordinary, fully-populated current-shape entry."""
    import reclaim.executor as executor_module

    fake_logger = _RecordingLogger()
    monkeypatch.setattr(executor_module, "logger", fake_logger)

    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(json.dumps(_base_entry_dict()) + "\n", encoding="utf-8")

    read_manifest_entries(manifest_path)

    assert fake_logger.warnings == []


def test_schema_v2_real_captured_pre_upgrade_manifest_parses_without_raising(
    tmp_path: Path,
) -> None:
    """The specific proof the audit brief requires: `read_manifest_entries` against the actual
    `data/quarantine/manifest.jsonl` content captured on this machine before this fix (a real
    July-13 batch missing is_dir/rebuild_instruction/retention_days/schema_version), copied
    byte-for-byte into `tests/fixtures/`, not a synthetic minimal stand-in."""
    assert _REAL_PRE_UPGRADE_MANIFEST_FIXTURE.exists(), (
        "real captured pre-upgrade manifest fixture is missing -- see "
        "tests/fixtures/quarantine_manifest_pre_p0_4_2026_07_13.jsonl"
    )
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_bytes(_REAL_PRE_UPGRADE_MANIFEST_FIXTURE.read_bytes())

    entries = read_manifest_entries(manifest_path)

    assert len(entries) == 2
    for entry in entries:
        assert entry.batch_id == "batch_1783893791_836d0aad"
        assert entry.is_dir is False
        assert entry.rebuild_instruction is None
        assert entry.retention_days is None
        assert entry.schema_version == 1
    assert entries[0].restored is False
    assert entries[1].restored is True


def test_schema_v2_compute_reconciliation_succeeds_on_real_captured_pre_upgrade_manifest(
    tmp_path: Path,
) -> None:
    """`GET /api/recovery/status`'s underlying function (`compute_reconciliation`) must not raise
    on the real captured pre-upgrade manifest -- this is the exact 500 the audit reproduced with
    a full traceback (P0-4)."""
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_bytes(_REAL_PRE_UPGRADE_MANIFEST_FIXTURE.read_bytes())

    report = compute_reconciliation(manifest_path=manifest_path, vault_dir=tmp_path / "vault")

    # Every real entry has phase="done" (defaulted, pre-ADR-0026 shape) -- nothing here is an
    # orphaned "intent", so there is genuinely nothing to reconcile; the point of this test is
    # that the call completes at all rather than raising a ValidationError.
    assert report.scanned_intents == 0
    assert report.reconciled == ()


def _api_config() -> Config:
    return Config()


def _make_test_api_client(tmp_path: Path, *, manifest_path: Path) -> TestClient:
    """Minimal local `create_app` wiring, deliberately not shared with `test_api.py`'s own
    `_make_app` helper -- this file only needs a read-only client against one pre-seeded
    manifest, not the full apply/restore/vault machinery those helpers set up."""
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_api_config(),
        vault_dir=tmp_path / "vault",
        manifest_path=manifest_path,
        mode_log_path=tmp_path / "mode_log.jsonl",
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    csrf_token: str = app.state.reclaim.csrf_token
    return TestClient(
        app,
        base_url=f"http://{_TEST_HOST}:{_TEST_PORT}",
        headers={security.CSRF_HEADER_NAME: csrf_token},
    )


def test_schema_v2_get_api_recovery_status_succeeds_on_real_captured_pre_upgrade_manifest(
    tmp_path: Path,
) -> None:
    """End-to-end proof through the real route, not just the underlying function: `GET
    /api/recovery/status` against a manifest containing the real captured pre-upgrade content
    must return 200, not the raw 500 the audit reproduced."""
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_bytes(_REAL_PRE_UPGRADE_MANIFEST_FIXTURE.read_bytes())
    client = _make_test_api_client(tmp_path, manifest_path=manifest_path)

    response = client.get("/api/recovery/status")

    assert response.status_code == 200
    body = response.json()
    assert body["scanned_intents"] == 0
    assert body["pending"] == []


def test_schema_v2_get_api_quarantine_succeeds_on_real_captured_pre_upgrade_manifest(
    tmp_path: Path,
) -> None:
    """End-to-end proof for the other 500 site the audit found: `GET /api/quarantine` (quarantine
    batch listing) against the real captured pre-upgrade manifest must return 200 and the one
    real batch, folded to its latest ("restored") state."""
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_bytes(_REAL_PRE_UPGRADE_MANIFEST_FIXTURE.read_bytes())
    client = _make_test_api_client(tmp_path, manifest_path=manifest_path)

    response = client.get("/api/quarantine")

    assert response.status_code == 200
    body = response.json()
    assert len(body["batches"]) == 1
    batch = body["batches"][0]
    assert batch["batch_id"] == "batch_1783893791_836d0aad"
    assert batch["items"][0]["restored"] is True


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
    partway through.

    `QUARANTINE_MANIFEST_SCHEMA_VERSION + 1` (not a hardcoded literal): guarantees this line is
    genuinely "from tomorrow" relative to this code regardless of the constant's current value --
    a literal `2` stopped being future-dated the moment schema v2 (audit P0-4) made `2` the
    current version."""
    future_version = QUARANTINE_MANIFEST_SCHEMA_VERSION + 1
    data = _base_entry_dict(
        schema_version=future_version, future_flag=True, future_note="from tomorrow"
    )
    entry = QuarantineManifestEntry.model_validate_json(json.dumps(data))

    # Simulate exactly what restore_batch/purge_expired/reclaim.recovery do: model_copy an
    # already-parsed entry, then re-serialize it.
    updated = entry.model_copy(update={"phase": "aborted"})
    round_tripped_json = updated.model_dump_json()
    round_tripped = QuarantineManifestEntry.model_validate_json(round_tripped_json)

    assert round_tripped.phase == "aborted"
    assert round_tripped.schema_version == future_version
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
    """Explicit `schema_version=QUARANTINE_MANIFEST_SCHEMA_VERSION` (schema v2, audit P0-4):
    the field's own default is now the literal historical `1` (see `QuarantineManifestEntry.
    schema_version`'s comment), not the current version, so a freshly-constructed entry standing
    in for real code (mirrors `apply_batch`'s own intent-entry construction in `executor.py`,
    which does the same explicitly) must pass it explicitly too -- exactly what real code does."""
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
        schema_version=QUARANTINE_MANIFEST_SCHEMA_VERSION,
    )
    assert entry.schema_version == QUARANTINE_MANIFEST_SCHEMA_VERSION

    append_manifest_entries(manifest_path, [entry])
    entries = read_manifest_entries(manifest_path)

    assert len(entries) == 1
    assert entries[0] == entry


def test_fresh_construction_without_explicit_schema_version_gets_literal_historical_default(
    tmp_path: Path,
) -> None:
    """The flip side of the test above, made explicit rather than left implicit: constructing an
    entry WITHOUT passing `schema_version` gets the literal `1` -- the honest "no version claimed"
    value -- never silently the current known version. This is exactly why every real
    fresh-construction call site (`apply_batch`'s intent entry) must pass it explicitly."""
    entry = QuarantineManifestEntry(
        batch_id="batch_no_version",
        original_path=Path("C:/Users/gg/Downloads/thing.bin"),
        size_bytes=42,
        category="test_category",
        category_group="test_group",
        rationale="test",
        tier=Tier.A,
        method="vault",
        vault_path=Path("data/quarantine/batch_no_version/thing.bin"),
        quarantined_at=_NOW,
        retention_until=_NOW + 30 * 86400.0,
    )
    assert entry.schema_version == 1
    assert entry.is_dir is False
    assert entry.rebuild_instruction is None
    assert entry.retention_days is None
