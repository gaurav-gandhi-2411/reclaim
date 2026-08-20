from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from reclaim.api import service
from reclaim.api.state import AppState
from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig
from reclaim.mcp.selection import (
    SelectionMismatchError,
    StaleScanError,
    compute_selection_hash,
)
from reclaim.mcp.server import build_mcp_server, build_state
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_NOW = 1_700_000_000.0


def _write(path: Path, content: bytes, *, mtime: float = _NOW) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def _config() -> Config:
    """dev_artifacts is the only category this file needs enabled -- a `node_modules` dir is a
    real, deterministic Tier A candidate (rebuildable via `npm install`), the same fixture shape
    `tests/test_api.py::_build_tree` already uses for its own dev_artifacts assertions.

    `retention_days=30` explicitly, same reason `tests/test_api.py::_config` sets it: ADR-0001
    changed dev_artifacts' real default to `None` (direct, permanent delete, no vault) -- this
    file's `delete` tool needs a genuine vault+manifest round trip to assert against, which a
    `None`-retention candidate structurally cannot produce (`apply_batch` skips the vault move
    entirely for those)."""
    return Config(
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30)
        )
    )


def _build_tree(root: Path) -> dict[str, Path]:
    # `detect_dev_artifacts` only ever proposes `node_modules` when a `package.json` manifest
    # sits in its parent directory (rebuildability proof, spec invariant, absolute) -- same
    # `package.json` sibling `tests/test_api.py::_build_tree` already includes for the same
    # reason.
    _write(root / "Project" / "package.json", b'{"name": "demo"}')
    node_modules_file = root / "Project" / "node_modules" / "pkg" / "index.js"
    _write(node_modules_file, b"x" * 5_000)
    kept_file = root / "Documents" / "keep_me.txt"
    _write(kept_file, b"do-not-touch")
    return {"node_modules_dir": node_modules_file.parent.parent, "kept_file": kept_file}


def _build_power_mode_state(tmp_path: Path, *, config: Config) -> AppState:
    """Power mode, not safe mode -- `AppState.effective_config`'s safe-mode override forces
    every candidate to Tier B and (irrelevant here, but real) `apply_batch` to Recycle-Bin-only,
    which would defeat this file's Tier A / vault-quarantine assertions. Mirrors `tests/
    test_api.py::_make_app`'s exact same pre-seeded-POWER-mode-log setup for the same reason."""
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    return build_state(
        db_path=tmp_path / "index.sqlite3",
        config=config,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
    )


async def _poll_scan_status_until_completed(
    session: object, *, timeout_seconds: float = 10.0
) -> str:
    """`scan`'s tool runs `service.run_scan` on a background daemon thread (see
    `reclaim.mcp.server.scan`'s docstring) -- a real MCP client polls `scan_status()` the same
    way `app.js` polls `GET /api/scan/status`; this fixture tree is tiny, so the loop below
    terminates in well under a second in practice, with a generous ceiling only to fail loudly
    (not hang forever) if something regresses."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = await session.call_tool("scan_status", {})  # type: ignore[attr-defined]
        assert result.isError is False, result.content
        status = result.structuredContent
        if status["status"] == "completed":
            scan_id: str = status["scan_id"]
            return scan_id
        if status["status"] in ("failed", "cancelled"):
            raise AssertionError(f"scan ended unexpectedly: {status}")
        await asyncio.sleep(0.02)
    raise AssertionError("scan did not complete within the test timeout")


# --- Pure hash / typed-error unit tests --------------------------------------------------------


def test_compute_selection_hash_is_order_independent_over_paths() -> None:
    """The commitment is over the SET of paths, not the order they happened to be listed in --
    `preview_apply` and `delete` independently rebuild this list from `_all_candidates`, whose
    own iteration order is not guaranteed to be stable across two separate calls."""
    forward = compute_selection_hash(
        scan_id="scan-1", tier="A", rule_id_or_category="dev_artifacts", paths=["b", "a", "c"]
    )
    reordered = compute_selection_hash(
        scan_id="scan-1", tier="A", rule_id_or_category="dev_artifacts", paths=["c", "a", "b"]
    )
    assert forward == reordered


@pytest.mark.parametrize(
    ("scan_id", "tier", "rule_id_or_category", "paths"),
    [
        ("scan-2", "A", "dev_artifacts", ["a", "b"]),  # different scan_id
        ("scan-1", "B", "dev_artifacts", ["a", "b"]),  # different tier
        ("scan-1", "A", "package_caches", ["a", "b"]),  # different selector
        ("scan-1", "A", "dev_artifacts", ["a", "b", "c"]),  # different path set
    ],
)
def test_compute_selection_hash_changes_when_any_committed_field_changes(
    scan_id: str, tier: str, rule_id_or_category: str, paths: list[str]
) -> None:
    baseline = compute_selection_hash(
        scan_id="scan-1", tier="A", rule_id_or_category="dev_artifacts", paths=["a", "b"]
    )
    varied = compute_selection_hash(
        scan_id=scan_id, tier=tier, rule_id_or_category=rule_id_or_category, paths=paths
    )
    assert baseline != varied


def test_stale_scan_error_and_selection_mismatch_error_are_distinct_typed_errors() -> None:
    """`reclaim.mcp.server.delete` must be able to tell these two refusal reasons apart (a
    caller should re-run `scan_status()` for one, `preview_apply()` for the other) -- proven
    here as a plain type-system fact, independent of any server/protocol machinery."""
    assert issubclass(StaleScanError, RuntimeError)
    assert issubclass(SelectionMismatchError, RuntimeError)
    assert StaleScanError is not SelectionMismatchError
    assert not issubclass(StaleScanError, SelectionMismatchError)
    assert not issubclass(SelectionMismatchError, StaleScanError)


# --- service.py's new MCP-facing functions (no MCP protocol involved) --------------------------


def test_scan_id_for_state_only_changes_after_a_completed_scan(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())

    before = service.scan_id_for_state(state)
    service.run_scan(state, [root], time.time())
    after = service.scan_id_for_state(state)

    assert before != after
    assert service.is_current_scan_id(state, after) is True
    assert service.is_current_scan_id(state, before) is False


def test_select_candidates_for_selector_matches_fine_grained_category_or_group(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    service.run_scan(state, [root], time.time())

    by_category = service.select_candidates_for_selector(
        state, tier="A", rule_id_or_category="dev_artifact_node_modules"
    )
    by_group = service.select_candidates_for_selector(
        state, tier="A", rule_id_or_category="dev_artifacts"
    )
    assert [c.path for c in by_category] == [paths["node_modules_dir"]]
    assert [c.path for c in by_group] == [paths["node_modules_dir"]]

    # Negative control: an unrelated selector matches nothing, and the kept file never appears
    # under any selector -- there is no code path here that could smuggle it in.
    assert (
        service.select_candidates_for_selector(
            state, tier="A", rule_id_or_category="package_caches"
        )
        == []
    )


def test_select_candidates_for_selector_rejects_an_unknown_tier(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    service.run_scan(state, [root], time.time())

    with pytest.raises(ValueError, match="tier"):
        service.select_candidates_for_selector(
            state, tier="not-a-tier", rule_id_or_category="dev_artifacts"
        )


# --- Full MCP-protocol round trip (mcp.shared.memory's in-memory client/server harness) --------


async def test_full_workflow_scan_list_preview_delete_actually_quarantines(
    tmp_path: Path,
) -> None:
    """The end-to-end integration proof the task brief asks for: `delete` really does reach
    `apply_batch` (not just a hash check) when `selection_hash` matches -- asserted by real
    filesystem state after the call, not just a 200-shaped response."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        scan_result = await session.call_tool("scan", {"path": str(root)})
        assert scan_result.isError is False, scan_result.content
        scan_id = await _poll_scan_status_until_completed(session)

        list_result = await session.call_tool(
            "list_candidates", {"scan_id": scan_id, "tier": "A", "category": None}
        )
        assert list_result.isError is False, list_result.content
        listed_paths = {c["path"] for c in list_result.structuredContent["candidates"]}
        node_modules_posix = paths["node_modules_dir"].as_posix()
        assert node_modules_posix in listed_paths
        assert paths["kept_file"].as_posix() not in listed_paths  # negative control

        preview_result = await session.call_tool(
            "preview_apply",
            {
                "scan_id": scan_id,
                "rule_id_or_category": "dev_artifact_node_modules",
                "tier": "A",
            },
        )
        assert preview_result.isError is False, preview_result.content
        preview = preview_result.structuredContent
        assert preview["item_count"] == 1
        assert preview["sample_paths"] == [node_modules_posix]

        delete_result = await session.call_tool(
            "delete",
            {
                "scan_id": scan_id,
                "rule_id_or_category": "dev_artifact_node_modules",
                "tier": "A",
                "selection_hash": preview["selection_hash"],
            },
        )
        assert delete_result.isError is False, delete_result.content
        outcome = delete_result.structuredContent
        assert outcome["files_succeeded"] == 1
        assert outcome["files_failed"] == 0

    # Real, disk-mutating proof -- not just a well-shaped response.
    assert not paths["node_modules_dir"].exists()
    assert paths["kept_file"].exists()  # negative control: untouched
    assert any((tmp_path / "vault").rglob("index.js"))  # landed in the real vault


async def test_delete_refuses_a_stale_scan_id(tmp_path: Path) -> None:
    """A newer scan completing between `preview_apply` and `delete` must refuse, not execute
    against the (possibly now-wrong) old selection."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("scan", {"path": str(root)})
        first_scan_id = await _poll_scan_status_until_completed(session)

        preview = (
            await session.call_tool(
                "preview_apply",
                {
                    "scan_id": first_scan_id,
                    "rule_id_or_category": "dev_artifact_node_modules",
                    "tier": "A",
                },
            )
        ).structuredContent

        # A second (incremental, no new files) scan bumps scan_generation -- first_scan_id is
        # now stale even though nothing about the candidate set actually changed.
        await session.call_tool("scan", {"path": str(root)})
        await _poll_scan_status_until_completed(session)

        delete_result = await session.call_tool(
            "delete",
            {
                "scan_id": first_scan_id,
                "rule_id_or_category": "dev_artifact_node_modules",
                "tier": "A",
                "selection_hash": preview["selection_hash"],
            },
        )
        assert delete_result.isError is True
        [content] = delete_result.content
        assert "stale" in content.text.lower() or "does not match" in content.text.lower()

    # Refused, not partially executed.
    assert paths["node_modules_dir"].exists()


async def test_delete_refuses_a_tampered_selection_hash(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("scan", {"path": str(root)})
        scan_id = await _poll_scan_status_until_completed(session)

        preview = (
            await session.call_tool(
                "preview_apply",
                {
                    "scan_id": scan_id,
                    "rule_id_or_category": "dev_artifact_node_modules",
                    "tier": "A",
                },
            )
        ).structuredContent

        tampered_hash = preview["selection_hash"][:-4] + "dead"
        delete_result = await session.call_tool(
            "delete",
            {
                "scan_id": scan_id,
                "rule_id_or_category": "dev_artifact_node_modules",
                "tier": "A",
                "selection_hash": tampered_hash,
            },
        )
        assert delete_result.isError is True
        [content] = delete_result.content
        assert "selection_hash" in content.text or "match" in content.text.lower()

    assert paths["node_modules_dir"].exists()


async def test_delete_refuses_when_the_candidate_set_changed_since_preview(
    tmp_path: Path,
) -> None:
    """A race with a manual apply/config change between preview and delete: `select_candidates_
    for_selector` is re-derived fresh inside `delete`, so a selection that shrank (here:
    dev_artifacts got disabled between the two calls, simulating a config/mode change a
    concurrent actor made) produces a different hash and must refuse -- same mechanism as a
    tampered hash, different real-world cause."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("scan", {"path": str(root)})
        scan_id = await _poll_scan_status_until_completed(session)

        preview = (
            await session.call_tool(
                "preview_apply",
                {
                    "scan_id": scan_id,
                    "rule_id_or_category": "dev_artifact_node_modules",
                    "tier": "A",
                },
            )
        ).structuredContent

        # Simulate the underlying candidate set changing between preview and delete -- e.g. a
        # concurrent config reload -- without going through a rescan (which would instead hit
        # the already-covered stale-scan_id path).
        state.config = state.config.model_copy(
            update={
                "categories": state.config.categories.model_copy(
                    update={"dev_artifacts": DevArtifactsConfig(enabled=False)}
                )
            }
        )

        delete_result = await session.call_tool(
            "delete",
            {
                "scan_id": scan_id,
                "rule_id_or_category": "dev_artifact_node_modules",
                "tier": "A",
                "selection_hash": preview["selection_hash"],
            },
        )
        assert delete_result.isError is True

    assert paths["node_modules_dir"].exists()


async def test_list_candidates_and_preview_apply_refuse_a_stale_scan_id(tmp_path: Path) -> None:
    """Staleness is checked on every read tool that's scoped to a scan_id, not just `delete` --
    an agent should never see candidate data attributed to a scan_id that no longer reflects the
    live index."""
    root = tmp_path / "tree"
    _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("scan", {"path": str(root)})
        await _poll_scan_status_until_completed(session)
        # A real scan just completed (scan_generation is now 1) -- "scan-0" is the id that
        # NAMED the pre-scan generation, so it's guaranteed stale now, not just an unissued
        # sentinel that happens to collide with the live generation by coincidence.
        stale_scan_id = "scan-0"

        list_result = await session.call_tool(
            "list_candidates", {"scan_id": stale_scan_id, "tier": "A", "category": None}
        )
        assert list_result.isError is True

        preview_result = await session.call_tool(
            "preview_apply",
            {"scan_id": stale_scan_id, "rule_id_or_category": "dev_artifacts", "tier": "A"},
        )
        assert preview_result.isError is True


# The schema-level "no registered tool accepts a raw path" proof (including a negative test that
# the check itself has teeth) lives in evals/test_mcp_safety_gate.py, alongside this package's
# other structural safety-gate assertions -- not duplicated here. `tests/` has no import path to
# `evals/` (no shared `pythonpath`/package root between the two directories, by this project's
# existing convention -- neither does tests/test_ai_safety_gate-adjacent code), so the schema
# check is self-contained there instead of split across two directories.
