"""The frozen smoke suite's HTTP probe follows the candidates cold-cache contract (ADR-0040).

Release-gate finding on build 13 (1 Nov): check 7 (scan -> apply -> undo) failed with
"GET /api/candidates failed" on a healthy build, because the server answers a cold cache with
409 `candidates_not_warm` and the probe treated that as a failure. These tests drive the probe's
`Client.get_candidates` against a fake loopback server, never a real Reclaim process.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

_PROBE_PATH = Path(__file__).resolve().parents[1] / "packaging" / "smoke" / "http_probe.py"
_NOT_WARM = {"detail": "cold", "code": "candidates_not_warm", "stale_reason": "cold"}


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("frozen_http_probe", _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Script:
    """Scripted answers: `candidates` is consumed one per GET /api/candidates, `warm` one per
    GET /api/candidates/warm-status (the last entry repeats)."""

    def __init__(self, candidates: list[tuple[int, str]], warm: list[str]) -> None:
        self.candidates = candidates
        self.warm = warm
        self.calls: list[str] = []


def _serve(script: _Script) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            script.calls.append(self.path)
            if self.path.startswith("/api/candidates/warm-status"):
                state = script.warm.pop(0) if len(script.warm) > 1 else script.warm[0]
                code, body = (
                    200,
                    json.dumps({"status": state, "error": "boom" if state == "failed" else None}),
                )
            elif self.path.startswith("/api/candidates"):
                code, body = script.candidates.pop(0)
            else:
                code, body = 404, "{}"
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:  # keep pytest output clean
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load_probe()
    monkeypatch.setattr(module, "_POLL_INTERVAL_SECONDS", 0.01)
    return module


def _run(probe: ModuleType, script: _Script, timeout: float = 5.0) -> tuple[int, str]:
    gen = _serve(script)
    base = next(gen)
    try:
        return probe.Client(base).get_candidates("/api/candidates?tier=both", timeout=timeout)
    finally:
        gen.close()


def test_cold_cache_waits_for_ready_then_reads_once_more(probe: ModuleType) -> None:
    ok = json.dumps({"candidates": [{"category": "large_log", "path": "x"}]})
    script = _Script([(409, json.dumps(_NOT_WARM)), (200, ok)], ["computing", "computing", "ready"])
    status, body = _run(probe, script)
    assert status == 200
    assert json.loads(body)["candidates"][0]["category"] == "large_log"
    assert script.calls.count("/api/candidates?tier=both") == 2
    assert script.calls.count("/api/candidates/warm-status") == 3


def test_warm_cache_reads_once_and_never_polls(probe: ModuleType) -> None:
    script = _Script([(200, json.dumps({"candidates": []}))], ["ready"])
    status, _ = _run(probe, script)
    assert status == 200
    assert script.calls == ["/api/candidates?tier=both"]


def test_failed_warm_up_is_reported_not_retried(probe: ModuleType) -> None:
    script = _Script([(409, json.dumps(_NOT_WARM))], ["computing", "failed"])
    status, body = _run(probe, script)
    assert status == 409
    assert json.loads(body)["error"] == "boom"
    assert script.calls.count("/api/candidates?tier=both") == 1


@pytest.mark.parametrize(
    "body",
    [json.dumps({"detail": "a warm-up is already running"}), "not json", json.dumps(["list"])],
)
def test_other_409_bodies_are_returned_as_is(probe: ModuleType, body: str) -> None:
    script = _Script([(409, body)], ["ready"])
    status, returned = _run(probe, script)
    assert (status, returned) == (409, body)
    assert script.calls == ["/api/candidates?tier=both"]


def test_warm_up_that_never_finishes_times_out(probe: ModuleType) -> None:
    script = _Script([(409, json.dumps(_NOT_WARM))], ["computing"])
    with pytest.raises(TimeoutError):
        _run(probe, script, timeout=0.2)
