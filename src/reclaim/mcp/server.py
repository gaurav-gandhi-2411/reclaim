from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from reclaim.api import service
from reclaim.api.state import AppState, ScanStatus
from reclaim.config import Config
from reclaim.first_run import DEFAULT_FIRST_RUN_STATE_PATH
from reclaim.logging_config import DEFAULT_LOG_PATH
from reclaim.mcp.audit import log_mcp_action
from reclaim.mcp.schemas import (
    CandidateSummary,
    DeleteResult,
    ListCandidatesResult,
    PreviewApplyResult,
    ScanStatusResult,
    ScanTriggerResult,
)
from reclaim.mcp.selection import (
    ConcurrentDeleteError,
    SelectionMismatchError,
    StaleScanError,
    compute_selection_hash,
)
from reclaim.mode import DEFAULT_MODE_LOG_PATH
from reclaim.safety import SafetyValidator

# reclaim.mcp.server — the ONLY module in this package that talks to the outside world (stdio,
# the MCP transport). Never imports `reclaim.executor` or `send2trash` (see this package's
# module docstring); every real read or mutation goes through `reclaim.api.service`'s
# choke-point functions, the same layer `reclaim.api.routes` (the HTTP surface) already uses.

_DEFAULT_VAULT_DIR = Path("data/quarantine")
_DEFAULT_MANIFEST_PATH = _DEFAULT_VAULT_DIR / "manifest.jsonl"

# `PreviewApplyResult.sample_paths`/an agent's own sanity-check need a preview, not a full
# enumeration -- see `PreviewApplyResult`'s docstring. Matches `cli.py`'s own `_REPORT_TOP_N`
# convention (this codebase's existing "cap a printed/returned sample at 10" number) rather than
# inventing a second one.
_SAMPLE_PATHS_LIMIT = 10

_TIER_CHOICES = ("A", "B", "both")


def build_state(
    *,
    db_path: Path,
    config: Config,
    vault_dir: Path | None = None,
    manifest_path: Path | None = None,
    mode_log_path: Path | None = None,
    first_run_state_path: Path | None = None,
    log_path: Path | None = None,
) -> AppState:
    """Builds one `AppState` for the MCP server process -- the exact same construction
    `reclaim.api.app.create_app` does for the HTTP dashboard, minus the FastAPI/HTTP-only
    fields (`csrf_token`/`host`/`port` exist solely for `reclaim.api.security`'s Origin/Host
    DNS-rebinding guard, which has no meaning for a stdio-transport MCP server -- placeholder
    values are used since nothing under `reclaim.mcp` ever reads them). `config` must be RAW
    (exactly `reclaim.config.load_config`'s output, no safe-mode override baked in) for the same
    reason `create_app` requires it raw -- see `AppState.effective_config`'s docstring."""
    resolved_log_path = log_path if log_path is not None else DEFAULT_LOG_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return AppState(
        db_path=db_path,
        config=config,
        vault_dir=vault_dir if vault_dir is not None else _DEFAULT_VAULT_DIR,
        manifest_path=manifest_path if manifest_path is not None else _DEFAULT_MANIFEST_PATH,
        safety=SafetyValidator(config),
        csrf_token="",  # unused outside the HTTP transport's Origin/Host guard
        host="127.0.0.1",  # unused outside the HTTP transport's Origin/Host guard
        port=0,  # unused outside the HTTP transport's Origin/Host guard
        mode_log_path=mode_log_path if mode_log_path is not None else DEFAULT_MODE_LOG_PATH,
        first_run_state_path=(
            first_run_state_path
            if first_run_state_path is not None
            else DEFAULT_FIRST_RUN_STATE_PATH
        ),
        log_path=resolved_log_path,
    )


def _client_id(ctx: Context[Any, Any, Any]) -> str | None:
    """Best-effort caller identity for the audit log -- `Context.client_id` is populated by the
    MCP session handshake for transports that carry one; `None` (never a fabricated placeholder)
    when the connected client didn't supply one, so a log reader can tell "no id given" apart
    from a real id that happened to be absent for some other reason."""
    return ctx.client_id


def _request_id(ctx: Context[Any, Any, Any]) -> str:
    return str(ctx.request_id)


def build_mcp_server(state: AppState) -> FastMCP:
    """Builds one Reclaim MCP server instance bound to `state`. A fresh `AppState` per call
    (never a module-level global) -- same isolation reasoning `AppState`'s own docstring gives
    for the HTTP dashboard: each test, and each real server process, gets its own instance."""
    mcp = FastMCP(
        name="reclaim",
        instructions=(
            "Reclaim disk-cleanup control surface. Workflow: scan(path) -> scan_status() until "
            "completed (note the returned scan_id) -> list_candidates(scan_id, tier, category) "
            "to see what's eligible -> preview_apply(scan_id, rule_id_or_category, tier) to get "
            "a selection_hash and a byte/item count -> delete(scan_id, rule_id_or_category, "
            "tier, selection_hash) to actually quarantine/delete those files. There is no way "
            "to name an arbitrary file path for deletion through this server -- every selection "
            "is by Reclaim's own detector rule id or category group, never a path. A stale "
            "scan_id or a selection_hash that no longer matches the live candidate set is "
            "refused, not silently substituted."
        ),
    )

    @mcp.tool()
    def scan(path: str, ctx: Context[Any, Any, Any]) -> ScanTriggerResult:
        """Start scanning a directory tree to build/update Reclaim's inventory index. Returns
        immediately (the scan runs in the background) -- call scan_status() to poll progress and
        learn the scan_id once it completes. Refuses if a scan is already running or `path`
        isn't a directory that exists on this machine."""
        root = Path(path)
        if not root.is_dir():
            raise ValueError(f"scan path does not exist or is not a directory: {root}")

        started_at = time.time()
        with state.lock:
            if state.scan_status.status == "running":
                raise RuntimeError(
                    f"a scan is already running for {state.scan_status.root} -- call "
                    "scan_status() and wait for it to finish before starting another."
                )
            # Mirrors `reclaim.api.routes.start_scan`'s exact ordering: the cancel event is
            # cleared and scan_status flipped to "running" synchronously, BEFORE the background
            # scan is ever started -- see `AppState.cancel_scan_event`'s docstring for the race
            # this ordering avoids.
            state.cancel_scan_event.clear()
            state.scan_status = ScanStatus(
                status="running",
                root=root,
                started_at=started_at,
                phase="estimating",
                current_drive=root.as_posix(),
                drives_total=1,
                drives_done=0,
            )

        log_mcp_action(
            "mcp.scan_started",
            client_id=_client_id(ctx),
            request_id=_request_id(ctx),
            path=root.as_posix(),
        )
        # No FastAPI BackgroundTasks in an MCP/stdio process -- a daemon thread is this
        # process's equivalent (never blocks the tool call itself; the thread is abandoned, not
        # joined, on process exit, same posture a killed `reclaim serve` process already has for
        # its own uvicorn worker threads).
        threading.Thread(
            target=service.run_scan, args=(state, [root], started_at), daemon=True
        ).start()
        return ScanTriggerResult(status="running", root=root.as_posix())

    @mcp.tool()
    def scan_status(ctx: Context[Any, Any, Any]) -> ScanStatusResult:
        """Poll the status of the most recently triggered scan. `scan_id` is populated only
        once `status == "completed"` -- use it with list_candidates/preview_apply/delete."""
        with state.lock:
            status = state.scan_status
            scan_id = service.scan_id_for_state(state) if status.status == "completed" else None
            result = ScanStatusResult(
                status=status.status,
                root=status.root.as_posix() if status.root is not None else None,
                scan_id=scan_id,
                entries_total=status.entries_total,
                files_written=status.files_written,
                error=status.error,
            )
        log_mcp_action(
            "mcp.scan_status_polled",
            client_id=_client_id(ctx),
            request_id=_request_id(ctx),
            status=result.status,
        )
        return result

    @mcp.tool()
    def list_candidates(
        scan_id: str, tier: str, category: str | None, ctx: Context[Any, Any, Any]
    ) -> ListCandidatesResult:
        """List cleanup candidates for a completed scan (wraps GET /api/candidates). `tier` is
        one of "A", "B", "both". `category` optionally narrows to one category_group (e.g.
        "dev_artifacts"); omit it for every category. Refuses with a stale-scan error if
        `scan_id` no longer matches the scan the live index reflects -- call scan_status() for
        the current one."""
        if not service.is_current_scan_id(state, scan_id):
            raise StaleScanError(
                f"scan_id {scan_id!r} does not match the current scan "
                f"({service.scan_id_for_state(state)}) -- the index has changed since this "
                "scan_id was issued. Call scan_status() for the current scan_id."
            )
        if tier not in _TIER_CHOICES:
            raise ValueError(f"tier must be one of {_TIER_CHOICES} (got {tier!r})")

        response = service.list_candidates(state, tier=tier, category_group=category)
        summaries = [
            CandidateSummary(
                path=c.path,
                category=c.category,
                category_group=c.category_group,
                tier=c.tier.value,
                size_bytes=c.size_bytes,
                rationale=c.rationale,
            )
            for c in response.candidates
        ]
        log_mcp_action(
            "mcp.list_candidates",
            client_id=_client_id(ctx),
            request_id=_request_id(ctx),
            scan_id=scan_id,
            tier=tier,
            category=category,
            count=len(summaries),
        )
        return ListCandidatesResult(
            scan_id=scan_id,
            count=len(summaries),
            total_bytes=response.total_bytes,
            candidates=summaries,
        )

    @mcp.tool()
    def preview_apply(
        scan_id: str, rule_id_or_category: str, tier: str, ctx: Context[Any, Any, Any]
    ) -> PreviewApplyResult:
        """Preview what a `delete` call for this exact `(scan_id, rule_id_or_category, tier)`
        selection would do, WITHOUT touching disk. `rule_id_or_category` is either a
        fine-grained detector rule id (Candidate.category, e.g. "windows_temp") or a coarse
        category group (Candidate.category_group, e.g. "temp_and_browser_caches") -- never a
        file path; there is no path parameter anywhere on this tool. Returns a
        `selection_hash` that `delete` must be called with to actually execute this selection
        -- the hash is a commitment over the exact candidate set resolved right now, and
        `delete` refuses if a fresh re-derivation no longer matches it (a stale scan, a changed
        candidate set, or a tampered hash)."""
        if not service.is_current_scan_id(state, scan_id):
            raise StaleScanError(
                f"scan_id {scan_id!r} does not match the current scan "
                f"({service.scan_id_for_state(state)}) -- the index has changed since this "
                "scan_id was issued. Call scan_status() for the current scan_id."
            )
        selected = service.select_candidates_for_selector(
            state, tier=tier, rule_id_or_category=rule_id_or_category
        )
        paths = [c.path.as_posix() for c in selected]
        selection_hash = compute_selection_hash(
            scan_id=scan_id, tier=tier, rule_id_or_category=rule_id_or_category, paths=paths
        )
        bytes_total = sum(c.size_bytes for c in selected)
        sample_paths = sorted(paths)[:_SAMPLE_PATHS_LIMIT]

        log_mcp_action(
            "mcp.preview_apply",
            client_id=_client_id(ctx),
            request_id=_request_id(ctx),
            scan_id=scan_id,
            tier=tier,
            rule_id_or_category=rule_id_or_category,
            item_count=len(selected),
            bytes_total=bytes_total,
        )
        return PreviewApplyResult(
            scan_id=scan_id,
            tier=tier,
            rule_id_or_category=rule_id_or_category,
            selection_hash=selection_hash,
            item_count=len(selected),
            bytes_total=bytes_total,
            sample_paths=sample_paths,
        )

    @mcp.tool()
    def delete(
        scan_id: str,
        rule_id_or_category: str,
        tier: str,
        selection_hash: str,
        ctx: Context[Any, Any, Any],
    ) -> DeleteResult:
        """Actually quarantine/delete the files selected by `(scan_id, rule_id_or_category,
        tier)` -- REQUIRES a `selection_hash` from a prior `preview_apply` call for this exact
        selection. There is no path parameter: this tool can only ever act on Reclaim's own
        deterministic detector output, selected by rule id or category group. Refuses (no
        partial execution) if `scan_id` is stale, if a fresh re-derivation of the selection no
        longer hashes to `selection_hash`, or if another `delete` call is already in flight on
        this server -- call preview_apply again for a current hash in the first two cases, or
        simply retry once the in-flight call finishes for the third."""
        client_id = _client_id(ctx)
        request_id = _request_id(ctx)

        # Concurrency fix (docs/AUDIT-2026-08.md, adversarial re-verification of PR #39):
        # check-and-set `mcp_delete_in_progress` atomically under `state.lock`, same idiom
        # `POST /api/apply` already uses for `apply_status` (routes.py) -- claims the
        # single-flight slot BEFORE any candidate re-derivation or hash work happens, so a
        # second concurrent `delete` call for the identical selection is refused immediately
        # rather than racing the first one to `apply_batch` (see `ConcurrentDeleteError`'s
        # docstring for the exact race this closes). The lock is held only briefly here, not
        # across the whole operation below -- `apply_batch` can take minutes on a large batch
        # (ADR-0026), and holding a process-wide lock for that long would block every other
        # AppState reader (scan status polls, mode checks) for no reason; the flag alone is
        # what needs to be atomic, matching `POST /api/apply`'s own lock-scoping precedent.
        with state.lock:
            if state.mcp_delete_in_progress:
                log_mcp_action(
                    "mcp.delete_refused",
                    client_id=client_id,
                    request_id=request_id,
                    reason="concurrent_delete_in_progress",
                    scan_id=scan_id,
                    tier=tier,
                    rule_id_or_category=rule_id_or_category,
                )
                raise ConcurrentDeleteError(
                    "another delete() call is already in progress on this server -- refusing "
                    "this one rather than risk two concurrent deletes racing the same "
                    "selection. Wait for the in-flight call to finish and try again."
                )
            state.mcp_delete_in_progress = True

        try:
            if not service.is_current_scan_id(state, scan_id):
                log_mcp_action(
                    "mcp.delete_refused",
                    client_id=client_id,
                    request_id=request_id,
                    reason="stale_scan_id",
                    scan_id=scan_id,
                    tier=tier,
                    rule_id_or_category=rule_id_or_category,
                )
                raise StaleScanError(
                    f"scan_id {scan_id!r} does not match the current scan "
                    f"({service.scan_id_for_state(state)}) -- the index has changed since this "
                    "selection was previewed. Refusing to delete anything. Call scan_status() "
                    "for the current scan_id, then preview_apply() again for a fresh "
                    "selection_hash."
                )

            selected = service.select_candidates_for_selector(
                state, tier=tier, rule_id_or_category=rule_id_or_category
            )
            paths = [c.path.as_posix() for c in selected]
            recomputed_hash = compute_selection_hash(
                scan_id=scan_id, tier=tier, rule_id_or_category=rule_id_or_category, paths=paths
            )
            if recomputed_hash != selection_hash:
                log_mcp_action(
                    "mcp.delete_refused",
                    client_id=client_id,
                    request_id=request_id,
                    reason="selection_hash_mismatch",
                    scan_id=scan_id,
                    tier=tier,
                    rule_id_or_category=rule_id_or_category,
                )
                raise SelectionMismatchError(
                    "selection_hash does not match a fresh re-derivation of this "
                    "(scan_id, rule_id_or_category, tier) selection -- the candidate set "
                    "changed since preview_apply ran (a manual apply/restore, a background "
                    "purge, or files changing on disk), or the hash was tampered with. Refusing "
                    "to delete anything. Call preview_apply() again for a current "
                    "selection_hash."
                )

            log_mcp_action(
                "mcp.delete_executing",
                client_id=client_id,
                request_id=request_id,
                scan_id=scan_id,
                tier=tier,
                rule_id_or_category=rule_id_or_category,
                item_count=len(selected),
            )
            response = service.mcp_execute_delete(state, selected)
            log_mcp_action(
                "mcp.delete_executed",
                client_id=client_id,
                request_id=request_id,
                batch_id=response.batch_id,
                files_succeeded=response.files_succeeded,
                files_failed=response.files_failed,
                bytes_freed=response.bytes_freed,
            )
            return DeleteResult(
                batch_id=response.batch_id,
                files_processed=response.files_processed,
                files_succeeded=response.files_succeeded,
                files_failed=response.files_failed,
                bytes_freed=response.bytes_freed,
            )
        finally:
            # Released unconditionally -- a refusal (stale scan, hash mismatch) or a real
            # exception from mcp_execute_delete must never leave this slot permanently claimed,
            # which would wedge every future delete() call on this process.
            with state.lock:
                state.mcp_delete_in_progress = False

    return mcp


def run_mcp_server(state: AppState) -> None:
    """Runs the MCP server over stdio -- the ONLY transport this entry point ever uses. No
    `--transport`/`--host`/`--port` flags exist anywhere on `reclaim mcp-serve` (see `cli.py`):
    stdio means the server is spawned as a subprocess by whatever MCP client invokes it and
    talks over its own stdin/stdout, with nothing to bind to a network interface at all --
    strictly narrower than `reclaim serve`'s already-hard-enforced loopback-only HTTP bind
    (`cli._loopback_host`). A network-reachable transport for a deletion-capable control surface
    would need its own, deliberately separate, security review before ever being added; this
    function does not leave that door ajar."""
    build_mcp_server(state).run(transport="stdio")
