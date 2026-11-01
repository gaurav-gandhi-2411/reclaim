from __future__ import annotations

import json
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from reclaim.api import security
from reclaim.api.app import create_app
from reclaim.assistant_setup import assistant_setup
from reclaim.config import Config


def test_installed_snippets_name_the_exe_and_parse(tmp_path: Path) -> None:
    exe_dir = Path(r"C:\Users\some one\AppData\Local\Programs\Reclaim")
    s = assistant_setup(exe_dir=exe_dir)
    exe = str(exe_dir / "reclaim.exe")
    assert s.installed and s.command == exe and s.args == ("mcp-serve",)
    desktop = json.loads(s.claude_desktop_json)
    assert desktop == {"mcpServers": {"reclaim": {"command": exe, "args": ["mcp-serve"]}}}
    codex = tomllib.loads(s.codex_toml)
    assert codex == {"mcp_servers": {"reclaim": {"command": exe, "args": ["mcp-serve"]}}}
    assert s.claude_code_command == f'claude mcp add --scope user reclaim -- "{exe}" mcp-serve'


def test_a_quote_in_the_path_still_gives_valid_toml() -> None:
    s = assistant_setup(exe_dir=Path(r"C:\Users\o'neil\Reclaim"))
    assert tomllib.loads(s.codex_toml)["mcp_servers"]["reclaim"]["command"].endswith("reclaim.exe")


def test_a_source_checkout_uses_the_interpreter() -> None:
    s = assistant_setup(exe_dir=None, python=r"C:\py\python.exe")
    assert not s.installed
    assert s.args == ("-m", "reclaim.cli", "mcp-serve")
    assert (
        json.loads(s.claude_desktop_json)["mcpServers"]["reclaim"]["command"] == r"C:\py\python.exe"
    )


def test_the_route_is_read_only(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=Config(),
        config_path=tmp_path / "config.toml",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=tmp_path / "mode_log.jsonl",
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host="127.0.0.1",
        port=8420,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1:8420",
        headers={security.CSRF_HEADER_NAME: app.state.reclaim.csrf_token},
    )
    data = client.get("/api/assistant/setup").json()
    assert set(data) == {
        "command",
        "args",
        "installed",
        "claude_desktop_json",
        "claude_code_command",
        "codex_toml",
    }
    assert client.post("/api/assistant/setup").status_code == 405
