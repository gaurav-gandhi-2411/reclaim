from __future__ import annotations

import os
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp_gates import auto_approve
from test_mcp import _build_power_mode_state, _poll_scan_status_until_completed, _write

from reclaim.api.relocate_routes import relocation_candidates
from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, ExclusionsConfig
from reclaim.mcp.server import build_mcp_server

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")


def _config() -> Config:
    return Config(
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30)
        ),
        exclusions=ExclusionsConfig(project_names=["zzexclproj"]),
    )


def _tree(root: Path) -> None:
    for project in ("Project", "zzexclproj"):
        _write(root / project / "package.json", b'{"name": "demo"}')
        _write(root / project / "node_modules" / "pkg" / "index.js", b"x" * 5_000)


async def test_a_plan_hands_out_exactly_the_hash_preview_and_delete_use(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state, approval_gate=auto_approve())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("scan", {"path": str(root)})
        scan_id = await _poll_scan_status_until_completed(session)

        plan = (
            await session.call_tool("plan_free_space", {"scan_id": scan_id, "target_bytes": 1000})
        ).structuredContent
        assert plan["reaches_target"] is True
        assert plan["frees_now_bytes"] == 0
        (selection,) = plan["selections"]
        assert selection["rule_id_or_category"] == "dev_artifact_node_modules"
        assert selection["reversible"] is True

        preview = (
            await session.call_tool(
                "preview_apply",
                {
                    "scan_id": scan_id,
                    "rule_id_or_category": selection["rule_id_or_category"],
                    "tier": "A",
                },
            )
        ).structuredContent
        assert preview["selection_hash"] == selection["selection_hash"]
        # A3: the excluded project's node_modules never appears in what the plan covers.
        assert all("zzexclproj" not in p for p in preview["sample_paths"])
        assert preview["item_count"] == selection["item_count"] == 1

        deleted = await session.call_tool(
            "delete",
            {
                "scan_id": scan_id,
                "rule_id_or_category": selection["rule_id_or_category"],
                "tier": "A",
                "selection_hash": selection["selection_hash"],
            },
        )
        assert deleted.isError is False, deleted.content
    assert not (root / "Project" / "node_modules").exists()
    assert (root / "zzexclproj" / "node_modules" / "pkg" / "index.js").exists()


async def test_an_unreachable_target_reports_its_shortfall_and_relaxes_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    _tree(root)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state, approval_gate=auto_approve())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("scan", {"path": str(root)})
        scan_id = await _poll_scan_status_until_completed(session)
        plan = (
            await session.call_tool(
                "plan_free_space",
                {
                    "scan_id": scan_id,
                    "target_bytes": 10**12,
                    "exclude_categories": ["dev_artifacts"],
                },
            )
        ).structuredContent
    assert plan["reaches_target"] is False
    assert plan["selections"] == []
    assert plan["shortfall_bytes"] == 10**12
    assert any("exclude_categories" in line for line in plan["left_out"])


async def test_a_stale_scan_id_and_bad_arguments_are_refused(tmp_path: Path) -> None:
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state, approval_gate=auto_approve())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        stale = await session.call_tool(
            "plan_free_space", {"scan_id": "scan-999", "target_bytes": 1}
        )
        assert stale.isError is True
        current = "scan-0"
        for bad in ({"max_tier": "B"}, {"prefer": "random"}, {"target_bytes": 0}):
            args = {"scan_id": current, "target_bytes": 1, **bad}
            result = await session.call_tool("plan_free_space", args)
            assert result.isError is True, bad


async def test_the_read_only_tools_answer_without_a_scan(tmp_path: Path) -> None:
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state, approval_gate=auto_approve())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        disks = (await session.call_tool("get_disk_status", {})).structuredContent
        assert disks["drives"] and all(
            d["total_bytes"] >= d["free_bytes"] >= 0 for d in disks["drives"]
        )
        exclusions = (await session.call_tool("list_exclusions", {})).structuredContent
        assert exclusions["project_names"] == ["zzexclproj"]


async def test_find_unused_models_never_reads_an_excluded_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(os.environ["USERPROFILE"])
    hub = tmp_path / "hub"
    for repo in ("org--used", "org--unused", "org--zzexclproj-model"):
        _write(hub / f"models--{repo}" / "blobs" / "b", b"w" * 100)
    _write(home / f"p-{tmp_path.name}" / "train.py", b'load("org/used")')
    secret = home / f"zzexclproj-{tmp_path.name}" / "train.py"
    _write(secret, b'load("org/unused")')
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    opened: list[str] = []
    real_read = Path.read_text

    def spy(self: Path, *a: object, **k: object) -> str:
        opened.append(str(self))
        return real_read(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)
    state = _build_power_mode_state(tmp_path, config=_config())
    server = build_mcp_server(state, approval_gate=auto_approve())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        report = (await session.call_tool("find_unused_models", {})).structuredContent
    found = {m["repo_id"]: m for m in report["models"]}
    assert set(found) == {"org/used", "org/unused"}  # the excluded-named model is not reported
    assert found["org/used"]["referenced_by"]
    assert found["org/unused"]["referenced_by"] == []  # its only user is excluded, never read
    assert report["excluded_projects_not_read"] is True
    assert not any("zzexclproj" in p.casefold() for p in opened)


def _snapshot(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p), p.stat().st_size if p.is_file() else -1) for p in root.rglob("*"))


async def test_list_relocation_options_is_the_pages_listing_and_changes_nothing(
    tmp_path: Path,
) -> None:
    home = Path(os.environ["USERPROFILE"])  # redirected under tmp by the root conftest
    hub = home / ".cache" / "huggingface" / "hub"
    _write(hub / "models--org--big" / "blobs" / "b", b"w" * 4_096)
    state = _build_power_mode_state(tmp_path, config=_config())
    before = _snapshot(home)
    server = build_mcp_server(state, approval_gate=auto_approve())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert tools["list_relocation_options"].inputSchema.get("properties", {}) == {}
        result = (await session.call_tool("list_relocation_options", {})).structuredContent
    assert _snapshot(home) == before  # read-only: nothing renamed, written or journaled
    options = {o["path"]: o for o in result["options"]}
    assert options[str(hub)]["verdict"] == "movable"
    assert options[str(hub)]["size_bytes"] >= 4_096
    page = relocation_candidates(state)  # exactly what the dashboard page lists
    assert [o["path"] for o in result["options"]] == [c.path for c in page.candidates]
    assert [o["verdict"] for o in result["options"]] == [c.verdict for c in page.candidates]
