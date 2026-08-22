"""Real MCP stdio round trip against a frozen `reclaim.exe mcp-serve` subprocess.

Launches the frozen exe as a real subprocess, speaks newline-delimited JSON-RPC 2.0 over its
stdin/stdout exactly as the MCP stdio transport spec defines (no `mcp` client package
dependency, deliberately -- this script must run on a bare Python install with no project venv,
since it's exercising the FROZEN artifact independent of source), sends a real `initialize`
handshake followed by one real tool call (`scan_status`, chosen because it needs no arguments and
has no side effects), and reports a single JSON result line on stdout for the calling PowerShell
script to parse.

Never treats a timeout or a malformed response as a pass (house rule 98a: a probe that can't
verify must not be read as a positive result) -- every exit path prints exactly one JSON object
with a `result` field of `PASS`, `FAIL`, or `BLOCKED`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_INITIALIZE_TIMEOUT_SECONDS = 20.0
_TOOL_CALL_TIMEOUT_SECONDS = 15.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _emit(result: str, detail: str, **extra: Any) -> None:
    """Prints the one JSON result line this script ever produces, then exits 0 -- the calling
    PowerShell script distinguishes PASS/FAIL/BLOCKED from the `result` field, never from this
    script's own exit code (which is always 0 once a result was determined at all; a non-zero
    exit with no JSON line means the harness itself crashed, a distinct failure the caller
    reports separately)."""
    payload = {"result": result, "detail": detail, **extra}
    print(json.dumps(payload))  # noqa: T201 -- this IS this script's structured output contract
    sys.exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path, help="Path to the frozen reclaim.exe")
    parser.add_argument("--db", required=True, type=Path, help="Scratch --db path for mcp-serve")
    parser.add_argument(
        "--config", required=True, type=Path, help="Scratch --config path for mcp-serve"
    )
    args = parser.parse_args()

    if not args.exe.is_file():
        _emit("BLOCKED", f"reclaim.exe not found at {args.exe}")
        return 0

    # Explicit cwd, always -- `reclaim mcp-serve` still writes relative `data/logs/reclaim.log`
    # (see logging_config.DEFAULT_LOG_PATH) even though --db/--config are given explicitly.
    # Without this, the subprocess inherits THIS script's own cwd (whatever directory the
    # caller happened to invoke it from), which can leak a stray `data/` directory into an
    # unrelated location -- confirmed directly while building this suite (a `data/logs/
    # reclaim.log` leaked into this very repo's working tree on a first pass). --db's parent is
    # already a scratch directory the caller controls, so it doubles as the working directory.
    scratch_dir = args.db.parent
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        # This subprocess call IS the check -- launching --exe (a caller-supplied, --exe-flagged
        # path this harness was explicitly pointed at, never arbitrary/untrusted input) is the
        # entire point of a frozen-artifact smoke test.
        proc = subprocess.Popen(  # noqa: S603
            [str(args.exe), "mcp-serve", "--db", str(args.db), "--config", str(args.config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            cwd=str(scratch_dir),
        )
    except OSError as exc:
        _emit("FAIL", f"failed to launch mcp-serve subprocess: {exc}")
        return 0

    responses: dict[int, dict[str, Any]] = {}
    stdout_lines: list[str] = []
    reader_error: list[str] = []

    def reader() -> None:
        if proc.stdout is None:
            raise RuntimeError("subprocess.PIPE guarantees proc.stdout is not None")
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                stdout_lines.append(line)
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg and isinstance(msg["id"], int):
                    responses[msg["id"]] = msg
        except Exception as exc:  # pragma: no cover -- defensive, reported via reader_error
            reader_error.append(str(exc))

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def send(message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("subprocess.PIPE guarantees proc.stdin is not None")
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def wait_for(msg_id: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if msg_id in responses:
                return responses[msg_id]
            if proc.poll() is not None:
                # Process exited before answering -- never treat this as "still waiting."
                return None
            time.sleep(0.1)
        return None

    def cleanup() -> str:
        stderr_tail = ""
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        if proc.stderr:
            with contextlib.suppress(OSError):
                stderr_tail = proc.stderr.read()[-2000:]
        return stderr_tail

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "reclaim-frozen-smoke-suite", "version": "0.1"},
                },
            }
        )
    except (OSError, ValueError) as exc:
        stderr_tail = cleanup()
        _emit("FAIL", f"failed to write initialize request to stdin: {exc}", stderr=stderr_tail)
        return 0

    init_resp = wait_for(1, _INITIALIZE_TIMEOUT_SECONDS)
    if init_resp is None:
        stderr_tail = cleanup()
        _emit(
            "FAIL",
            "no well-formed initialize response within timeout (process may have crashed on "
            "startup -- see stderr)",
            stderr=stderr_tail,
            reader_error=reader_error,
            stdout_lines=stdout_lines[-10:],
        )
        return 0
    if "error" in init_resp:
        stderr_tail = cleanup()
        _emit("FAIL", f"initialize returned a JSON-RPC error: {init_resp['error']}")
        return 0

    try:
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "scan_status", "arguments": {}},
            }
        )
    except (OSError, ValueError) as exc:
        stderr_tail = cleanup()
        _emit("FAIL", f"failed to write tool-call request to stdin: {exc}", stderr=stderr_tail)
        return 0

    tool_resp = wait_for(2, _TOOL_CALL_TIMEOUT_SECONDS)
    stderr_tail = cleanup()

    if tool_resp is None:
        _emit(
            "FAIL",
            "no well-formed tools/call response within timeout",
            stderr=stderr_tail,
            reader_error=reader_error,
        )
        return 0
    if "error" in tool_resp:
        _emit("FAIL", f"tools/call returned a JSON-RPC error: {tool_resp['error']}")
        return 0
    if tool_resp.get("result", {}).get("isError"):
        _emit("FAIL", f"scan_status tool call reported isError: {tool_resp['result']}")
        return 0

    _emit(
        "PASS",
        "initialize handshake + scan_status tool call both returned well-formed JSON-RPC "
        "responses over stdio",
        server_info=init_resp.get("result", {}).get("serverInfo"),
        tool_result=tool_resp.get("result"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
