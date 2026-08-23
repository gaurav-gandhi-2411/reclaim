"""Real HTTP round trips against an already-running frozen `reclaim.exe serve` process.

Two independently-invokable subcommands, both talking to the same live dashboard API a real
browser session would (CSRF token scraped from the served `index.html`, same as
`reclaim.api.security` requires of any mutating request):

  dpapi-roundtrip   -- store/read-status/test/delete a disposable Anthropic key through
                        `/api/settings/anthropic-key*`, proving the DPAPI CryptProtectData/
                        CryptUnprotectData round trip works under Nuitka (never uses or logs a
                        real API key -- see reclaim.anthropic_key_store's module docstring for
                        why this module never logs at all; this probe mirrors that discipline).
  scan-apply-undo   -- a real scan -> candidate list -> power-mode apply (vault) -> restore
                        cycle against a caller-provided scratch directory, ending by switching
                        the server's mode back to safe (this probe's own cleanup, not the
                        caller's responsibility).

Stdlib-only (`urllib.request`) -- deliberately no dependency on the `mcp`/`requests` packages or
this repo's own dev venv, since this exercises the FROZEN artifact, not the source tree.

Every exit path prints exactly one JSON result line (`result`: PASS/FAIL/BLOCKED) for the
calling PowerShell script to parse -- never a silent skip (house rule 98a).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

_CSRF_META_RE = re.compile(r'name="reclaim-csrf-token"\s+content="([^"]+)"')
_POLL_INTERVAL_SECONDS = 0.5
_POLL_TIMEOUT_SECONDS = 60.0
_POWER_MODE_CONFIRMATION = "I understand this can permanently delete files"
_DISPOSABLE_TEST_KEY = "sk-smoke-test-disposable-value-never-a-real-key-000000"
# AR5 (2026-08-24, real-machine finding): both get() and mutate() used to hardcode 15s/30s --
# same class of bug docs/AUDIT-2026-08.md already found and fixed once in this repo's OTHER
# trip script (ac3_login_diagnostic.ps1's apply-call timeout, "10s vs. an observed 30-50s"),
# just never checked for a sibling here. `GET /api/candidates` and `POST /api/apply` (an
# explicit-paths request, which re-derives candidates from the persisted index rather than a
# warm cache) both scale with the persisted index's row count, not the caller's tiny scratch
# fixture -- live-measured on a real dev machine, GET /api/candidates took 138s against a
# ~985MB/36,844-row index (accumulated from unrelated earlier work, not this suite's own
# fixture). 15s/30s reads as "this endpoint is slow/broken" on exactly the machine shape (a
# real, long-used dev box) this suite exists to catch regressions on.
#
# 180s raised the ceiling but did NOT fully close the gap: because `reclaim.app_paths.
# data_root()` for a frozen build anchors to the installed exe's own directory regardless of
# this suite's own scratch WorkingDirectory (by design -- see PR #51/#52/#53's CWD-independence
# fixes), every run of this suite on the SAME machine adds its own scan to the SAME shared,
# persisted index, not an isolated one -- so the real cost keeps growing across repeated runs
# and no fixed timeout is a permanent fix. Re-measured moments after the 138s figure above (one
# additional scan/apply/restore cycle in between): 180s+ and still climbing. A durable fix needs
# an isolated `--db`/data-root override for this suite specifically, not a bigger number here --
# out of scope for this pass; disclosed rather than silently left as "should be enough now."
_HTTP_TIMEOUT_SECONDS = 180.0


def _emit(result: str, detail: str, **extra: Any) -> None:
    payload = {"result": result, "detail": detail, **extra}
    print(json.dumps(payload))  # noqa: T201 -- structured output contract, see module docstring
    sys.exit(0)


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.csrf: str | None = None

    def get(self, path: str) -> tuple[int, str]:
        # self.base is always the http://127.0.0.1:<port> this suite itself launched (see
        # run_frozen_smoke_suite.ps1) -- never user/network-controlled input, so the scheme is
        # fixed and known-safe despite ruff's generic "audit URL open" warning.
        req = urllib.request.Request(self.base + path)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def mutate(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, str]:
        if self.csrf is None:
            raise RuntimeError("fetch_csrf() must be called before any mutating request")
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "x-reclaim-csrf-token": self.csrf}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310 -- see get() above
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def fetch_csrf(self) -> bool:
        status, html = self.get("/")
        if status != 200:
            return False
        match = _CSRF_META_RE.search(html)
        if match is None:
            return False
        self.csrf = match.group(1)
        return True

    def poll_until_done(self, status_path: str, timeout: float = _POLL_TIMEOUT_SECONDS) -> dict:
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            status, body = self.get(status_path)
            if status != 200:
                raise RuntimeError(f"GET {status_path} -> {status}: {body}")
            last = json.loads(body)
            if last.get("status") in ("completed", "failed", "cancelled"):
                return last
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"{status_path} did not reach a terminal status within {timeout}s (last seen: {last})"
        )


def cmd_dpapi_roundtrip(client: Client) -> None:
    if not client.fetch_csrf():
        _emit("BLOCKED", "could not fetch CSRF token from GET / -- is the server actually up?")
        return

    status, body = client.get("/api/settings/anthropic-key")
    if status != 200 or json.loads(body).get("configured") is not False:
        _emit(
            "FAIL",
            "expected configured=false before the round trip (a prior key was left behind, or "
            "the endpoint is broken)",
            status=status,
            body=body,
        )
        return

    status, body = client.mutate(
        "POST", "/api/settings/anthropic-key", {"api_key": _DISPOSABLE_TEST_KEY}
    )
    if status != 200 or json.loads(body).get("configured") is not True:
        _emit(
            "FAIL",
            "POST /api/settings/anthropic-key (DPAPI protect+write) failed",
            status=status,
            body=body,
        )
        return

    # /test decrypts the stored blob (DPAPI unprotect) and sends the plaintext to Anthropic's
    # real API -- a network-reachable "unauthorized" response PROVES the round trip worked (the
    # plaintext we stored came back out correctly); a DPAPI failure surfaces as a structurally
    # different 500, not a clean "Anthropic rejected this key" message.
    status, body = client.mutate("POST", "/api/settings/anthropic-key/test", {})
    if status != 200:
        _emit(
            "FAIL",
            "POST /api/settings/anthropic-key/test returned non-200 -- likely a DPAPI "
            "CryptUnprotectData failure, not a network problem",
            status=status,
            body=body,
        )
        return
    test_result = json.loads(body)

    status, body = client.mutate("DELETE", "/api/settings/anthropic-key", {})
    if status != 200 or json.loads(body).get("configured") is not False:
        _emit(
            "FAIL",
            "DELETE /api/settings/anthropic-key failed to clear the stored key",
            status=status,
            body=body,
        )
        return

    _emit(
        "PASS",
        "DPAPI store -> has_key -> decrypt-and-use (via /test) -> delete round trip completed; "
        "no key or key fragment ever appeared in this probe's own logging",
        test_endpoint_response=test_result,
    )


def cmd_scan_apply_undo(client: Client, seed_dir: str) -> None:
    if not client.fetch_csrf():
        _emit("BLOCKED", "could not fetch CSRF token from GET / -- is the server actually up?")
        return

    status, body = client.mutate(
        "POST", "/api/mode/power", {"confirmation_text": _POWER_MODE_CONFIRMATION}
    )
    if status != 200:
        _emit(
            "FAIL",
            "POST /api/mode/power failed -- cannot exercise a restorable vault apply",
            status=status,
            body=body,
        )
        return

    try:
        status, body = client.mutate("POST", "/api/scan", {"path": seed_dir})
        if status != 202:
            _emit("FAIL", "POST /api/scan did not return 202", status=status, body=body)
            return
        scan_final = client.poll_until_done("/api/scan/status")
        if scan_final.get("status") != "completed":
            _emit("FAIL", "scan did not complete", scan_status=scan_final)
            return

        status, body = client.get("/api/candidates?tier=both")
        if status != 200:
            _emit("FAIL", "GET /api/candidates failed", status=status, body=body)
            return
        candidates = json.loads(body)["candidates"]
        matching = [c for c in candidates if c["category"] == "large_log"]
        if not matching:
            _emit(
                "FAIL",
                "seeded large_log candidate was not proposed -- the scan/detector pipeline is "
                "not producing the expected result under the frozen build",
                all_candidates=candidates,
            )
            return
        target_path = matching[0]["path"]

        status, body = client.mutate(
            "POST",
            "/api/apply",
            {"tier": "both", "paths": [target_path], "method": "vault", "dry_run": False},
        )
        if status != 202:
            _emit("FAIL", "POST /api/apply did not return 202", status=status, body=body)
            return
        apply_final = client.poll_until_done("/api/apply/status")
        if apply_final.get("status") != "completed" or not apply_final.get("result"):
            _emit("FAIL", "apply did not complete successfully", apply_status=apply_final)
            return
        result = apply_final["result"]
        if result["files_succeeded"] != 1 or result["method"] != "vault":
            _emit(
                "FAIL",
                "apply completed but did not vault exactly the one seeded file as expected",
                apply_result=result,
            )
            return
        batch_id = result["batch_id"]

        status, body = client.mutate("POST", f"/api/restore/{batch_id}", {})
        if status != 202:
            _emit(
                "FAIL", "POST /api/restore/{batch_id} did not return 202", status=status, body=body
            )
            return
        restore_final = client.poll_until_done("/api/restore/status")
        if restore_final.get("status") != "completed" or not restore_final.get("result"):
            _emit(
                "FAIL", "restore (undo) did not complete successfully", restore_status=restore_final
            )
            return
        restore_result = restore_final["result"]
        if restore_result["files_succeeded"] != 1:
            _emit(
                "FAIL",
                "restore completed but did not restore the expected file",
                restore_result=restore_result,
            )
            return

        _emit(
            "PASS",
            "scan -> candidate list -> vault apply -> restore (undo) all completed against "
            "the frozen build, scoped to the caller-provided scratch seed file",
            batch_id=batch_id,
            apply_result=result,
            restore_result=restore_result,
        )
    finally:
        # Always attempt to leave the server back in safe mode, even on a failure path above --
        # this probe's own cleanup responsibility (see module docstring), independent of whether
        # the cycle it was testing succeeded.
        client.mutate("POST", "/api/mode/safe", {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subcommand", choices=("dpapi-roundtrip", "scan-apply-undo"))
    parser.add_argument("--base", required=True, help="Base URL, e.g. http://127.0.0.1:8421")
    parser.add_argument("--seed-dir", help="Scratch directory to scan (scan-apply-undo only)")
    args = parser.parse_args()

    client = Client(args.base)
    if args.subcommand == "dpapi-roundtrip":
        cmd_dpapi_roundtrip(client)
    else:
        if not args.seed_dir:
            _emit("BLOCKED", "--seed-dir is required for scan-apply-undo")
            return 0
        cmd_scan_apply_undo(client, args.seed_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
