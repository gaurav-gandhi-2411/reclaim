"""Writes the fixture profile and the MCP config for the Claude Code end-to-end smoke (run spec
Phase C; results in reports/assistant-mcp/). Run it on a scratch folder, then:
  claude -p "$(cat prompt.txt)" --mcp-config <dir>/mcp.json --strict-mcp-config --allowedTools
  <the reclaim read and plan tools> --disallowedTools mcp__reclaim__delete --output-format json
Scratch only: the MCP server's every profile root points into the fixture, so it can only ever see
that fixture."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

base = Path(sys.argv[1])
home = base / "Users" / "sam"
local = home / "AppData" / "Local"
for rel, size in {
    "Projects/webapp/package.json": 20,
    "Projects/webapp/node_modules/pkg/index.js": 2_000_000,
    "Projects/webapp/node_modules/pkg2/lib.js": 1_000_000,
    "Documents/keep.txt": 100,
    "AppData/Local/Temp/old.tmp": 500_000,
}.items():
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'{"name": "webapp"}' if rel.endswith("package.json") else os.urandom(size))
    os.utime(p, (1_600_000_000, 1_600_000_000))  # old: past every age threshold
(home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
data = base / "data"
data.mkdir(exist_ok=True)
env = {
    "USERPROFILE": str(home),
    "HOME": str(home),
    "LOCALAPPDATA": str(local),
    "APPDATA": str(home / "AppData" / "Roaming"),
    "TEMP": str(local / "Temp"),
    "TMP": str(local / "Temp"),
    "PYTHONPATH": r"C:\Users\gaura\ml-projects\reclaim-wt-work\src",
    "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
}
config = {
    "mcpServers": {
        "reclaim": {
            "command": r"C:\Users\gaura\ml-projects\reclaim\.venv\Scripts\python.exe",
            "args": [
                "-m",
                "reclaim.cli",
                "mcp-serve",
                "--db",
                str(data / "index.sqlite3"),
                "--config",
                str(data / "config.toml"),
                "--vault-dir",
                str(data / "quarantine"),
                "--manifest",
                str(data / "quarantine" / "manifest.jsonl"),
                "--mode-log",
                str(data / "mode_log.jsonl"),
                "--first-run-state",
                str(data / "first_run.json"),
                "--log-path",
                str(data / "reclaim.log"),
            ],
            "env": env,
        }
    }
}
(base / "mcp.json").write_text(json.dumps(config, indent=1), encoding="utf-8")
print(home)
