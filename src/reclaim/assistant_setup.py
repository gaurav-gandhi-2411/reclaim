"""The setup text for using Reclaim from Claude Desktop, Claude Code or Codex CLI (run spec
Phase C, docs/specs/assistant-mcp.md). Each client runs `reclaim.exe mcp-serve` over a local
pipe. Reclaim only shows the text with a copy button: it never edits a client's config file.

The client formats are from each client's documentation as known when this was written
(2026-10). They were not checked against a live install on the build machine (BELIEVED);
docs/HOWTO.md section 8 says so too.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from reclaim.app_paths import compiled_exe_dir

SERVER_NAME = "reclaim"


@dataclass(frozen=True, slots=True)
class AssistantSetup:
    command: str
    args: tuple[str, ...]
    installed: bool  # False: a source checkout, where the command is this Python interpreter
    claude_desktop_json: str
    claude_code_command: str
    codex_toml: str


def _toml_string(value: str) -> str:
    """A TOML literal string when it can be one (no escaping of backslashes needed), else a
    basic string with JSON-compatible escapes."""
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return json.dumps(value)


def _shell_quote(value: str) -> str:
    return f'"{value}"' if any(c in value for c in ' &()^%!"') or not value else value


def assistant_setup(exe_dir: Path | None = None, python: str | None = None) -> AssistantSetup:
    exe_dir = exe_dir if exe_dir is not None else compiled_exe_dir()
    args: tuple[str, ...]
    if exe_dir is not None:
        command, args, installed = str(exe_dir / "reclaim.exe"), ("mcp-serve",), True
    else:
        command = python or sys.executable
        args, installed = ("-m", "reclaim.cli", "mcp-serve"), False
    desktop = json.dumps(
        {"mcpServers": {SERVER_NAME: {"command": command, "args": list(args)}}}, indent=2
    )
    code = " ".join(
        ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, "--", _shell_quote(command)]
        + [_shell_quote(a) for a in args]
    )
    codex = "\n".join(
        [
            f"[mcp_servers.{SERVER_NAME}]",
            f"command = {_toml_string(command)}",
            "args = [" + ", ".join(json.dumps(a) for a in args) + "]",
        ]
    )
    return AssistantSetup(command, args, installed, desktop, code, codex)
