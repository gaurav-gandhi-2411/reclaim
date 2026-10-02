from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _warming_client import WarmingTestClient
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

import reclaim.executor as executor_module
import reclaim.scanner as scanner_module
from reclaim.api import security
from reclaim.api.app import create_app
from reclaim.cli import main
from reclaim.config import Config
from reclaim.dirlist import ListingUnsupported
from reclaim.executor import ItemApplyResult, apply_batch
from reclaim.index import ScanIndex
from reclaim.mcp.server import build_mcp_server, build_state
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, Mode, switch_to_power_mode
from reclaim.models import Candidate, Tier, Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

# ADR-0036: apply-time identity now also compares a FILE candidate's live (size, mtime) with the
# scan's record. Every test here has teeth: the "changed" cases fail if the comparison is removed,
# the "unchanged" cases fail if it is made too strict (a false skip).

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner/executor target Windows/NTFS")

_NOW = 1_700_000_000.0
_OLD = 1_600_000_000.0  # a fixed, long-past mtime so detectors treat the file as aged
_NEW_REASON = "size_or_mtime_changed_since_scan"


# --- helpers -------------------------------------------------------------------------------------


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


def _candidate_from_scan(
    path: Path, index: ScanIndex, *, retention_days: int | None = 30, is_dir: bool = False
) -> Candidate:
    record = index.get_record(path)
    assert record is not None, f"no scan record for {path}"
    return Candidate(
        path=path,
        is_dir=is_dir,
        category="test_category",
        category_group="test_group",
        size_bytes=record.size_bytes,
        tier=Tier.A,
        rationale="test rationale",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="TEST_REASON",
        retention_days=retention_days,
        dev=record.dev,
        ino=record.ino,
        mtime=record.mtime,
    )


def _apply(
    tmp_path: Path, candidate: Candidate, *, mode: Mode, method: str, **kwargs: Any
) -> ItemApplyResult:
    report = apply_batch(
        [candidate],
        safety=_safety(),
        apply=True,
        method=method,  # type: ignore[arg-type]
        mode=mode,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        now=_NOW,
        **kwargs,
    )
    return report.items[0]


@pytest.fixture(autouse=True)
def _fake_recycle_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real `send2trash` would fill the developer's actual Recycle Bin; this removes the path
    exactly as the real call does from the caller's point of view (K2a's post-condition check
    requires the original to be gone)."""

    def _fake(path: str) -> None:
        target = Path(path)
        shutil.rmtree(target) if target.is_dir() else target.unlink()

    monkeypatch.setattr(executor_module.send2trash, "send2trash", _fake)


_MODES = [
    pytest.param(Mode.POWER, "vault", None, id="power-direct_delete"),
    pytest.param(Mode.POWER, "vault", 30, id="power-vault"),
    pytest.param(Mode.SAFE, "recycle_bin", 30, id="safe-recycle_bin"),
]


def _scanned_file(
    tmp_path: Path, *, size: int = 1000, top_level: bool = False
) -> tuple[Path, ScanIndex]:
    """A file one directory BELOW the scanned root by default: `scan_tree` builds the root's own
    direct entries with `os.stat`, and only sub-directories go through the NTFS directory listing
    (`dirlist`) -- so the default exercises the listing-sourced record, `top_level=True` the
    `os.stat`-sourced one."""
    target = tmp_path / "tree" / ("flagged.bin" if top_level else "sub/flagged.bin")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * size)
    os.utime(target, (_OLD, _OLD))
    index = ScanIndex(tmp_path / "index.sqlite3")
    scan_tree(tmp_path / "tree", index)
    return target, index


# --- the behavior ----------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "method", "retention"), _MODES)
def test_in_place_append_is_skipped_and_file_intact(
    tmp_path: Path, mode: Mode, method: str, retention: int | None
) -> None:
    target, index = _scanned_file(tmp_path)
    with index:
        candidate = _candidate_from_scan(target, index, retention_days=retention)
    with target.open("ab") as fh:  # same inode: only (size, mtime) can reveal this
        fh.write(b"appended")

    result = _apply(tmp_path, candidate, mode=mode, method=method)

    assert result.succeeded is False
    assert result.skip_reason == _NEW_REASON
    assert result.error is None
    assert target.read_bytes() == b"x" * 1000 + b"appended"


@pytest.mark.parametrize(("mode", "method", "retention"), _MODES)
def test_mtime_only_touch_is_skipped(
    tmp_path: Path, mode: Mode, method: str, retention: int | None
) -> None:
    target, index = _scanned_file(tmp_path)
    with index:
        candidate = _candidate_from_scan(target, index, retention_days=retention)
    os.utime(target, (_OLD + 100, _OLD + 100))  # size and inode untouched

    result = _apply(tmp_path, candidate, mode=mode, method=method)

    assert result.skip_reason == _NEW_REASON
    assert target.exists()


def test_size_only_change_with_restored_mtime_is_skipped(tmp_path: Path) -> None:
    """Isolates the size half: the mtime is put back to the scan's exact nanosecond value."""
    target, index = _scanned_file(tmp_path)
    with index:
        candidate = _candidate_from_scan(target, index)
    original_ns = target.stat().st_mtime_ns
    with target.open("ab") as fh:
        fh.write(b"more")
    os.utime(target, ns=(original_ns, original_ns))
    assert target.stat().st_mtime == candidate.mtime  # the mtime half would pass

    result = _apply(tmp_path, candidate, mode=Mode.POWER, method="vault")

    assert result.skip_reason == _NEW_REASON
    assert target.exists()


@pytest.mark.parametrize(("mode", "method", "retention"), _MODES)
def test_unchanged_file_is_applied_normally(
    tmp_path: Path, mode: Mode, method: str, retention: int | None
) -> None:
    target, index = _scanned_file(tmp_path)
    with index:
        candidate = _candidate_from_scan(target, index, retention_days=retention)

    result = _apply(tmp_path, candidate, mode=mode, method=method)

    assert result.skip_reason is None
    assert result.succeeded is True
    assert not target.exists()


def test_swapped_file_new_inode_stays_identity_changed(tmp_path: Path) -> None:
    target, index = _scanned_file(tmp_path)
    with index:
        candidate = _candidate_from_scan(target, index)
    target.unlink()
    target.write_bytes(b"SWAPPED")  # new inode; size and mtime differ too

    result = _apply(tmp_path, candidate, mode=Mode.POWER, method="vault")

    assert result.skip_reason == "identity_changed_since_scan"
    assert target.read_bytes() == b"SWAPPED"


def test_candidate_without_an_mtime_baseline_is_not_size_mtime_checked(tmp_path: Path) -> None:
    """`mtime == 0.0` is the documented 'no baseline' marker (hand-built fixtures); the identity
    half still applies, the size/mtime half must not fire on an absent baseline."""
    target, index = _scanned_file(tmp_path)
    with index:
        record = index.get_record(target)
    assert record is not None
    candidate = Candidate(
        path=target,
        is_dir=False,
        category="c",
        category_group="g",
        size_bytes=1,  # deliberately wrong
        tier=Tier.A,
        rationale="r",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="T",
        retention_days=30,
        dev=record.dev,
        ino=record.ino,
        mtime=0.0,
    )

    result = _apply(tmp_path, candidate, mode=Mode.POWER, method="vault")

    assert result.skip_reason is None
    assert not target.exists()


def test_directory_candidates_are_exempt_from_the_size_mtime_check(tmp_path: Path) -> None:
    """A directory's mtime moves on any child change, so it must never trigger the new reason."""
    cache = tmp_path / "tree" / "cache_dir"
    cache.mkdir(parents=True)
    (cache / "a.bin").write_bytes(b"x" * 10)
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(tmp_path / "tree", index)
        candidate = _candidate_from_scan(cache, index, is_dir=True)
    (cache / "added_after_scan.bin").write_bytes(b"y")  # bumps the directory's own mtime
    assert cache.stat().st_mtime != candidate.mtime

    result = _apply(tmp_path, candidate, mode=Mode.POWER, method="vault")

    assert result.skip_reason is None  # vaulted tiering is top-level identity only (M1)
    assert not cache.exists()


# --- dirlist-sourced scan records compare equal to a live os.stat ----------------------------


def test_dirlist_scan_record_equals_live_stat_and_unchanged_file_is_not_false_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = {"entries": 0}
    real = scanner_module.list_directory

    def counting(path: str, volume_serial: int | None = None) -> Any:
        serial, entries = real(path, volume_serial)
        served["entries"] += len(entries)
        return serial, entries

    monkeypatch.setattr(scanner_module, "list_directory", counting)
    target, index = _scanned_file(tmp_path, size=4321)
    with index:
        record = index.get_record(target)
        candidate = _candidate_from_scan(target, index)
    assert served["entries"] > 0, "the scan did not go through the NTFS directory listing"
    assert record is not None
    live = target.stat()
    assert (record.size_bytes, record.mtime) == (live.st_size, live.st_mtime)  # exact float ==

    result = _apply(tmp_path, candidate, mode=Mode.POWER, method="vault")

    assert result.skip_reason is None
    assert result.succeeded is True


def test_stat_sourced_top_level_scan_record_is_not_false_skipped(tmp_path: Path) -> None:
    target, index = _scanned_file(tmp_path, top_level=True)
    with index:
        candidate = _candidate_from_scan(target, index)

    result = _apply(tmp_path, candidate, mode=Mode.POWER, method="vault")

    assert result.skip_reason is None
    assert not target.exists()


def test_legacy_scandir_scan_record_is_not_false_skipped_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(path: str, volume_serial: int | None = None) -> Any:
        raise ListingUnsupported(None, "forced fallback", path, 1)

    monkeypatch.setattr(scanner_module, "list_directory", unsupported)
    target, index = _scanned_file(tmp_path)
    with index:
        candidate = _candidate_from_scan(target, index)

    result = _apply(tmp_path, candidate, mode=Mode.POWER, method="vault")

    assert result.skip_reason is None
    assert not target.exists()


# --- ONE choke point: every entry point reaches apply_batch's preflight ----------------------


def _write_dmp(tree: Path) -> Path:
    dump = tree / "Crash" / "app.dmp"
    dump.parent.mkdir(parents=True)
    dump.write_bytes(b"d" * 3000)
    os.utime(dump, (_OLD, _OLD))
    return dump


def _make_client(tmp_path: Path, *, power: bool) -> TestClient:
    mode_log = tmp_path / "mode_log.jsonl"
    if power:
        switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=Config(),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host="127.0.0.1",
        port=8420,
    )
    return WarmingTestClient(
        app,
        base_url="http://127.0.0.1:8420",
        headers={security.CSRF_HEADER_NAME: app.state.reclaim.csrf_token},
    )


def _http_scan_and_apply(client: TestClient, root: Path, *, one_click: bool) -> dict[str, Any]:
    assert client.post("/api/scan", json={"path": str(root)}).status_code == 202
    assert client.get("/api/scan/status").json()["status"] == "completed"
    if one_click:  # the dashboard's flow: summary groups -> flattened explicit paths
        summary = client.get("/api/clean/one-click-summary").json()
        paths = [p for group in summary["groups"] for p in group["paths"]]
    else:
        paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]
    assert paths, "no candidates to apply"
    response = client.post(
        "/api/apply", json={"tier": "both", "paths": paths, "method": "vault", "dry_run": False}
    )
    assert response.status_code == 202, response.text
    status = client.get("/api/apply/status").json()
    assert status["status"] == "completed", status
    return status["result"]  # type: ignore[no-any-return]


def _entry_cli(tmp_path: Path, tree: Path, dump: Path, capsys: pytest.CaptureFixture[str]) -> str:
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    db = tmp_path / "index.sqlite3"
    assert main(["scan", str(tree), "--db", str(db)]) == 0
    capsys.readouterr()
    exit_code = main(
        [
            "apply",
            str(tree),
            "--db",
            str(db),
            "--config",
            str(tmp_path / "no_config.toml"),
            "--apply",
            "--mode-log",
            str(mode_log),
            "--vault-dir",
            str(tmp_path / "vault"),
            "--manifest",
            str(tmp_path / "manifest.jsonl"),
        ]
    )
    err = capsys.readouterr().err
    assert exit_code == 1  # a skipped item counts as not-succeeded
    assert f"SKIPPED: {dump} — {_NEW_REASON}" in err  # the CLI names the reason
    return err


def _entry_http(tmp_path: Path, tree: Path, dump: Path, capsys: pytest.CaptureFixture[str]) -> str:
    client = _make_client(tmp_path, power=True)
    result = _http_scan_and_apply(client, tree, one_click=False)
    (item,) = (i for i in result["items"] if i["path"] == dump.as_posix())
    assert item["skip_reason"] == _NEW_REASON
    return ""


def _entry_one_click(
    tmp_path: Path, tree: Path, dump: Path, capsys: pytest.CaptureFixture[str]
) -> str:
    client = _make_client(tmp_path, power=False)  # safe mode, exactly the dashboard's default
    result = _http_scan_and_apply(client, tree, one_click=True)
    (item,) = (i for i in result["items"] if i["path"] == dump.as_posix())
    assert item["skip_reason"] == _NEW_REASON
    return ""


def _entry_mcp(tmp_path: Path, tree: Path, dump: Path, capsys: pytest.CaptureFixture[str]) -> str:
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    state = build_state(
        db_path=tmp_path / "index.sqlite3",
        config=Config(),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
    )

    async def run() -> dict[str, Any]:
        server = build_mcp_server(state)
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            await session.call_tool("scan", {"path": str(tree)})
            scan_id = ""
            for _ in range(500):
                status = (await session.call_tool("scan_status", {})).structuredContent
                if status["status"] == "completed":
                    scan_id = status["scan_id"]
                    break
                await asyncio.sleep(0.02)
            assert scan_id, "scan did not complete"
            selector = {"scan_id": scan_id, "rule_id_or_category": "crash_dump_file", "tier": "A"}
            preview = (await session.call_tool("preview_apply", selector)).structuredContent
            outcome = await session.call_tool(
                "delete", {**selector, "selection_hash": preview["selection_hash"]}
            )
            assert outcome.isError is False, outcome.content
            return outcome.structuredContent  # type: ignore[no-any-return]

    result = asyncio.run(run())
    # MCP never enumerates paths (see DeleteResult): the reason arrives as a per-reason count.
    assert result["skipped_by_reason"] == {_NEW_REASON: 1}
    assert result["files_failed"] == 1
    return ""


_ENTRY_POINTS: dict[str, Callable[..., str]] = {
    "cli_apply": _entry_cli,
    "http_post_apply": _entry_http,
    "dashboard_one_click": _entry_one_click,
    "mcp_delete": _entry_mcp,
}


@pytest.mark.parametrize("entry", list(_ENTRY_POINTS))
def test_every_entry_point_runs_the_same_preflight_and_reports_the_new_reason(
    entry: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A write that lands AFTER candidate generation and BEFORE the preflight (simulated inside
    the preflight spy itself, so it is identical for all four entry points) must be caught by the
    single `executor._preflight_skip_reason` call every entry point funnels through. The spy
    delegating to the real function proves the shared implementation, not a per-entry copy."""
    tree = tmp_path / "tree"
    dump = _write_dmp(tree)
    seen: list[tuple[Path, str | None]] = []
    real = executor_module._preflight_skip_reason
    mutated = {"done": False}

    def spy(candidate: Candidate, **kwargs: Any) -> Any:
        if candidate.path == dump and not mutated["done"]:
            mutated["done"] = True
            with dump.open("ab") as fh:
                fh.write(b"late write")
        reason = real(candidate, **kwargs)
        seen.append((candidate.path, reason))
        return reason

    monkeypatch.setattr(executor_module, "_preflight_skip_reason", spy)

    _ENTRY_POINTS[entry](tmp_path, tree, dump, capsys)

    assert (dump, _NEW_REASON) in seen, f"{entry} never reached apply_batch's preflight"
    assert dump.read_bytes() == b"d" * 3000 + b"late write"  # skipped, not touched
