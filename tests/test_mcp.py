from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from reclaim.api import service
from reclaim.api.state import AppState
from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig
from reclaim.mcp.selection import (
    ConcurrentDeleteError,
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


def test_concurrent_delete_calls_for_the_identical_selection_do_not_both_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the concurrency gap found by adversarial re-verification of PR #39:
    two `delete()` calls firing concurrently with the IDENTICAL valid `(scan_id,
    rule_id_or_category, tier, selection_hash)` must not both pass through to `apply_batch`.
    Before the `AppState.mcp_delete_in_progress` fix, the scan index doesn't reflect the first
    call's real file move, so the second call's fresh re-derivation still matched the same
    `selection_hash` and both proceeded -- the second one failed at the filesystem level (the
    path was already gone) but the tool call itself still returned `isError=False,
    files_succeeded=0`, a misleading "the call succeeded" shape on a call that deleted nothing.

    Deliberately NOT driven through `create_connected_server_and_client_session` /
    `asyncio.gather()`: verified directly against this project's pinned `mcp==1.29.0` that a
    sync tool function is called with a plain, unthreaded `fn(**arguments)`
    (`mcp.server.fastmcp.utilities.func_metadata.FuncMetadata.call_fn_with_arg_validation`) --
    with no `await` boundary inside it, one sync tool call fully monopolizes the single asyncio
    event loop until it returns, so two tool calls dispatched via `asyncio.gather` (over one
    session or two) can never genuinely interleave their execution in this SDK's current
    architecture, no matter how they're scheduled. `AppState.lock` is a real `threading.Lock`
    (see its own docstring) built for genuine OS-thread races, so this test drives the actual
    concern directly: two real `threading.Thread`s calling the registered `delete` tool's raw
    function (`FastMCP._tool_manager.get_tool("delete").fn`) concurrently, with
    `service.mcp_execute_delete` patched to sleep briefly so the first thread is guaranteed to
    still hold `mcp_delete_in_progress=True` when the second thread's entry check runs --
    deterministic, not dependent on real scheduling luck."""
    import threading
    import time as time_module

    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    service.run_scan(state, [root], time.time())
    scan_id = service.scan_id_for_state(state)

    selected = service.select_candidates_for_selector(
        state, tier="A", rule_id_or_category="dev_artifact_node_modules"
    )
    assert [c.path for c in selected] == [paths["node_modules_dir"]]
    selection_hash = compute_selection_hash(
        scan_id=scan_id,
        tier="A",
        rule_id_or_category="dev_artifact_node_modules",
        paths=[c.path.as_posix() for c in selected],
    )

    real_mcp_execute_delete = service.mcp_execute_delete

    def _slow_mcp_execute_delete(*args: object, **kwargs: object) -> object:
        # Runs while this thread already holds `mcp_delete_in_progress=True` (set by `delete()`
        # before it ever reaches `service.mcp_execute_delete`) -- sleeping here, not before the
        # flag is claimed, is what guarantees the second thread's entry check observes it.
        time_module.sleep(0.3)
        return real_mcp_execute_delete(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(service, "mcp_execute_delete", _slow_mcp_execute_delete)

    server = build_mcp_server(state)
    tool = server._tool_manager.get_tool("delete")
    assert tool is not None
    delete_fn = tool.fn

    class _FakeContext:
        """Minimal stand-in for `mcp.server.fastmcp.Context` -- only `client_id`/`request_id`
        are read (via `_client_id`/`_request_id`), and those are ordinary properties that
        require a live request context on a real `Context`, which doesn't exist when calling
        the raw function directly like this."""

        client_id = None
        request_id = "concurrency-test"

    results: list[object] = [None, None]
    errors: list[BaseException | None] = [None, None]

    def _call_delete(slot: int) -> None:
        try:
            results[slot] = delete_fn(
                scan_id=scan_id,
                rule_id_or_category="dev_artifact_node_modules",
                tier="A",
                selection_hash=selection_hash,
                ctx=_FakeContext(),
            )
        except BaseException as exc:
            errors[slot] = exc

    first_thread = threading.Thread(target=_call_delete, args=(0,))
    second_thread = threading.Thread(target=_call_delete, args=(1,))
    first_thread.start()
    # A small head start so the first thread deterministically wins the race to claim
    # `mcp_delete_in_progress` before the second thread's own entry check runs -- the outcome
    # being tested (exactly one success, one clear refusal) doesn't depend on WHICH thread wins,
    # only that they don't both proceed; a head start just removes any ambiguity about which
    # thread this test expects to be the "first" one below.
    time_module.sleep(0.05)
    second_thread.start()
    first_thread.join()
    second_thread.join()

    assert errors[0] is None, errors[0]
    succeeded = results[0]
    refusal = errors[1]

    # The first call actually executed the delete; the second was refused outright, with a
    # typed error naming exactly why -- never both racing into apply_batch, and never a refused
    # call disguised as a 0-success "success".
    assert results[1] is None, "the second call must not have returned a result at all"
    assert refusal is not None, "the second call must have been refused, not silently allowed"
    assert isinstance(refusal, ConcurrentDeleteError), refusal
    assert "in progress" in str(refusal).lower()

    assert succeeded is not None
    assert succeeded.files_succeeded == 1  # type: ignore[attr-defined]
    assert succeeded.files_failed == 0  # type: ignore[attr-defined]

    # Real, disk-mutating proof: quarantined exactly once, never attempted twice.
    assert not paths["node_modules_dir"].exists()
    assert len(list((tmp_path / "vault").rglob("index.js"))) == 1


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


# --- Q3 rebase re-verification: MCP-path-specific identity attacks (docs/AUDIT-2026-08.md) -----
#
# The three tests below reproduce, through the real MCP `delete` tool specifically (not
# `apply_batch` called directly, which `evals/test_apply_identity_reverify.py` already covers),
# the exact P0-K1a live-reproduced finding that fix closed for the CLI/HTTP paths: swapping the
# real filesystem content at a candidate's path between `preview_apply` and `delete` must never
# result in the swapped content being silently deleted/vaulted. These are the money-shot proofs
# that `mcp_execute_delete`'s Q3 rebase fix (passing `scan_index=` to `apply_batch`, and the
# top-level `(dev, ino)` check that runs regardless of it) genuinely closes the gap for the MCP
# surface specifically, not just CLI/HTTP.


async def test_delete_refuses_when_path_swapped_between_preview_and_delete_via_mcp(
    tmp_path: Path,
) -> None:
    """The money-shot P0-K1a reproduction through the MCP surface: `preview_apply`'s
    `selection_hash` is a commitment over PATHS (and the stale DB index), never live filesystem
    identity -- deleting the real `node_modules` directory and recreating a directory with
    different content at the exact same path between `preview_apply` and `delete` produces the
    IDENTICAL hash (same scan_id/tier/rule_id_or_category/paths), so `SelectionMismatchError`
    alone cannot catch this. The swapped content must survive: `apply_batch`'s
    `_top_level_identity_mismatch` preflight check (compared against the `(dev, ino)` baseline
    recorded at scan time) is the only thing standing between this attack and a real deletion of
    unrelated, unreviewed content -- and it runs unconditionally for every candidate, independent
    of whether `scan_index=` was passed."""
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

        # Swap the real directory at the exact same path -- new inode, same path string, so the
        # commitment hash (paths only) is unaffected but the live filesystem identity is not.
        node_modules_dir = paths["node_modules_dir"]
        shutil.rmtree(node_modules_dir)
        node_modules_dir.mkdir(parents=True)
        canary = node_modules_dir / "SWAPPED-UNRELATED-CONTENT.txt"
        canary.write_text("this directory was swapped in after preview_apply -- must survive")

        delete_result = await session.call_tool(
            "delete",
            {
                "scan_id": scan_id,
                "rule_id_or_category": "dev_artifact_node_modules",
                "tier": "A",
                "selection_hash": preview["selection_hash"],
            },
        )
        # Not a hard refusal (the hash genuinely matched) -- a per-item skip, surfaced as
        # files_succeeded=0/files_failed=1, same shape the identity-reverify eval suite asserts
        # for the CLI/HTTP paths.
        assert delete_result.isError is False, delete_result.content
        outcome = delete_result.structuredContent
        assert outcome["files_succeeded"] == 0
        assert outcome["files_failed"] == 1
        assert outcome["bytes_freed"] == 0

    # The real, disk-level proof: the swapped-in content is untouched, never vaulted.
    assert node_modules_dir.exists()
    assert canary.exists()
    assert canary.read_text() == "this directory was swapped in after preview_apply -- must survive"
    assert not any((tmp_path / "vault").rglob("SWAPPED-UNRELATED-CONTENT.txt"))


async def test_delete_refuses_when_junction_repointed_between_preview_and_delete_via_mcp(
    tmp_path: Path,
) -> None:
    """Same P0-K1a shape as the path-swap test above, but via a real NTFS junction repoint
    (`mklink /J`, deleted and recreated pointing somewhere else) between `preview_apply` and
    `delete` -- reproduces `evals/test_apply_identity_reverify.py::test_junction_repoint_is_
    skipped`'s exact attack, now through the MCP `delete` tool specifically.

    The scanner counts a reparse point itself but never recurses into it (see `scanner.py`'s own
    comment) -- so, unlike the plain-directory path-swap test above, the CANDIDATE here is the
    `node_modules` entry itself being a junction (not a real directory containing one), which the
    `dev_artifact_node_modules` detector rule can still see via the adjacent `package.json`
    manifest-adjacency check without ever needing to recurse into it."""
    root = tmp_path / "tree"
    root.mkdir()
    project_dir = root / "Project"
    project_dir.mkdir()
    (project_dir / "package.json").write_bytes(b'{"name": "demo"}')

    target_a = tmp_path / "target_a"
    target_a.mkdir()
    (target_a / "index.js").write_bytes(b"x" * 5_000)

    target_b = tmp_path / "target_b"
    target_b.mkdir()
    (target_b / "b.txt").write_text("from target B -- must survive", encoding="utf-8")

    link = project_dir / "node_modules"
    result = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["cmd", "/c", "mklink", "/J", str(link), str(target_a)],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create NTFS junction: {result.stderr or result.stdout}")

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
        assert preview["item_count"] == 1

        link.rmdir()  # removes only the reparse point -- target_a's own contents untouched
        repoint = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(target_b)],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        assert repoint.returncode == 0, repoint.stderr or repoint.stdout

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
        assert outcome["files_succeeded"] == 0
        assert outcome["files_failed"] == 1

    # Neither the original target nor the swapped-in one was ever touched.
    assert (target_a / "index.js").exists()
    assert (target_b / "b.txt").exists()


async def test_delete_refuses_a_selection_hash_reused_across_a_fresh_scan_of_identical_content(
    tmp_path: Path,
) -> None:
    """`compute_selection_hash` includes `scan_id` in its committed payload (not just paths/tier/
    rule_id_or_category) -- verified here live: a hash computed under `scan_id=A` must be refused
    when replayed against `scan_id=B`, even when B's rescan produces an IDENTICAL-looking
    selection (same rule/category/tier, same single node_modules path) with nothing on disk
    having changed at all. Without `scan_id` in the hash input, a hash captured from one scan
    generation could be replayed against a later one that happens to resolve to the same paths --
    this proves that specific bypass is closed, not merely that some staleness check exists."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("scan", {"path": str(root)})
        scan_id_a = await _poll_scan_status_until_completed(session)

        preview_a = (
            await session.call_tool(
                "preview_apply",
                {
                    "scan_id": scan_id_a,
                    "rule_id_or_category": "dev_artifact_node_modules",
                    "tier": "A",
                },
            )
        ).structuredContent
        hash_from_scan_a = preview_a["selection_hash"]

        # Rescan the IDENTICAL tree -- nothing on disk changed, but scan_generation (and so
        # scan_id) increments regardless, same as `test_delete_refuses_a_stale_scan_id` above.
        await session.call_tool("scan", {"path": str(root)})
        scan_id_b = await _poll_scan_status_until_completed(session)
        assert scan_id_b != scan_id_a

        preview_b = (
            await session.call_tool(
                "preview_apply",
                {
                    "scan_id": scan_id_b,
                    "rule_id_or_category": "dev_artifact_node_modules",
                    "tier": "A",
                },
            )
        ).structuredContent
        # Same logical selection (identical single path), different scan generation -> the
        # commitment hash must differ, proving scan_id is genuinely part of the hash input, not
        # just checked separately.
        assert preview_b["selection_hash"] != hash_from_scan_a
        assert preview_b["sample_paths"] == preview_a["sample_paths"]

        # Attempt to replay scan A's hash against the CURRENT scan_id (B) -- must be refused.
        delete_result = await session.call_tool(
            "delete",
            {
                "scan_id": scan_id_b,
                "rule_id_or_category": "dev_artifact_node_modules",
                "tier": "A",
                "selection_hash": hash_from_scan_a,
            },
        )
        assert delete_result.isError is True
        [content] = delete_result.content
        assert "selection_hash" in content.text or "match" in content.text.lower()

    assert paths["node_modules_dir"].exists()


# The schema-level "no registered tool accepts a raw path" proof (including a negative test that
# the check itself has teeth) lives in evals/test_mcp_safety_gate.py, alongside this package's
# other structural safety-gate assertions -- not duplicated here. `tests/` has no import path to
# `evals/` (no shared `pythonpath`/package root between the two directories, by this project's
# existing convention -- neither does tests/test_ai_safety_gate-adjacent code), so the schema
# check is self-contained there instead of split across two directories.
